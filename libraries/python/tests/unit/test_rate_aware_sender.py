"""Rate-aware outbound sender — pitch / duration / passthrough.

Authentic tests: they drive the REAL ``TwilioAudioSender`` (and the real
``audioop.ratecv`` resampler + real mu-law codec) and assert on the actual
wire bytes. The only "mock" is a trivial fake WebSocket that captures the
base64 payloads the sender emits — the carrier boundary, per the authentic-
tests rule. No DSP is faked.

The bug under test: the sender used to assume every TTS source was 16 kHz and
ran a fixed 16 kHz -> 8 kHz decimator. A provider emitting 8 kHz PCM was
decimated AGAIN -> ~2x pitch (chipmunk). Now the sender reads the declared
source rate via ``set_source_format`` and resamples from the REAL rate.
"""

from __future__ import annotations

import base64
import json
import math
import struct

import pytest

from getpatter.audio.transcoding import mulaw_to_pcm16
from getpatter.telephony.twilio import TwilioAudioSender


def _sine_pcm16le(freq_hz: float, rate: int, seconds: float, amp: int = 12000) -> bytes:
    n = int(round(rate * seconds))
    out = bytearray()
    for i in range(n):
        v = int(round(amp * math.sin(2 * math.pi * freq_hz * i / rate)))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


def _goertzel(pcm: bytes, freq_hz: float, rate: int) -> float:
    n = len(pcm) // 2
    w = 2 * math.pi * freq_hz / rate
    coeff = 2 * math.cos(w)
    s1 = 0.0
    s2 = 0.0
    for i in range(n):
        (x,) = struct.unpack_from("<h", pcm, i * 2)
        s0 = x + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def _dominant_freq(pcm: bytes, rate: int, step: int = 2) -> float:
    best_f = 0.0
    best_p = -1.0
    f = float(step)
    while f < rate / 2 - step:
        p = _goertzel(pcm, f, rate)
        if p > best_p:
            best_p = p
            best_f = f
        f += step
    return best_f


class _FakeWS:
    """Captures the JSON frames the sender writes — the carrier WS boundary."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))

    def mulaw_bytes(self) -> bytes:
        out = bytearray()
        for fr in self.frames:
            payload = fr.get("media", {}).get("payload")
            if payload:
                out += base64.b64decode(payload)
        return bytes(out)


@pytest.mark.unit
class TestRateAwareTwilioSender:
    @pytest.mark.parametrize("src_rate", [8000, 16000, 22050, 24000, 44100])
    async def test_pitch_preserved_across_source_rates(self, src_rate: int) -> None:
        ws = _FakeWS()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")
        sender.set_source_format("pcm_s16le", src_rate)

        pcm = _sine_pcm16le(440, src_rate, 0.5)
        # Stream in awkward, non-frame-aligned chunks like a real HTTP TTS.
        for i in range(0, len(pcm), 517):
            await sender.send_audio(pcm[i : i + 517])
        await sender.flush()

        wire_pcm = mulaw_to_pcm16(ws.mulaw_bytes())  # decode mu-law back to PCM16
        f = _dominant_freq(wire_pcm, 8000)
        assert 430 < f < 450, f"expected ~440 Hz at 8 kHz wire, got {f} Hz"

    def test_old_fixed_path_chipmunks_an_8k_source(self) -> None:
        # Regression guard: the legacy default (no set_source_format → 16k→8k)
        # treats an 8 kHz tone as 16 kHz, decimating to ~880 Hz.
        from getpatter.audio.transcoding import (
            create_resampler_16k_to_8k,
            pcm16_to_mulaw,
        )

        pcm = _sine_pcm16le(440, 8000, 0.5)
        r = create_resampler_16k_to_8k()
        wire = mulaw_to_pcm16(pcm16_to_mulaw(r.process(pcm) + r.flush()))
        f = _dominant_freq(wire, 8000)
        assert f > 820, f"expected chipmunk (~2x) from the old path, got {f} Hz"

    @pytest.mark.parametrize("src_rate", [8000, 16000, 24000])
    async def test_duration_preserved(self, src_rate: int) -> None:
        ws = _FakeWS()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")
        sender.set_source_format("pcm_s16le", src_rate)
        pcm = _sine_pcm16le(300, src_rate, 1.0)
        await sender.send_audio(pcm)
        await sender.flush()
        # mu-law @ 8 kHz = 1 byte/sample → ~8000 bytes for 1.0 s.
        n_wire = len(ws.mulaw_bytes())
        assert abs(n_wire - 8000) < 80, f"{src_rate} Hz → {n_wire} mu-law bytes"

    async def test_mulaw_passthrough_is_bit_clean(self) -> None:
        # When the TTS declares mu-law 8 kHz (e.g. Cartesia.for_twilio), the
        # sender must forward bytes UNCHANGED — zero resample, zero re-encode.
        ws = _FakeWS()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")
        sender.set_source_format("mulaw", 8000)
        # Arbitrary mu-law payload (already wire-format).
        mulaw_in = bytes((i * 7 + 3) & 0xFF for i in range(160))
        await sender.send_audio(mulaw_in)
        assert ws.mulaw_bytes() == mulaw_in

"""Unit tests for ``StatefulFirLowpass`` — the windowed-sinc anti-alias
low-pass that runs on the 24 kHz Realtime-2 output before the 24->16->8
decimation to mulaw.

These exercise the REAL filter (no mocks): they feed real sinusoids through
``process()`` and assert on the real output bytes. A low-frequency tone passes
near unity; a tone above the cutoff is strongly attenuated; and feeding a
signal in two chunks yields byte-for-byte the same result as one chunk
(stateful cross-chunk continuity).

Signal synthesis + RMS use only the stdlib (``struct`` / ``math``) — numpy is
NOT a base dependency, so importing it here would break the base
``Python SDK Tests`` CI job at collection time. The filter itself is pure
stdlib too (``array``), so this keeps the whole path dependency-clean.
"""

from __future__ import annotations

import math
import struct

import pytest

from getpatter.audio.transcoding import StatefulFirLowpass

_SR = 24000
_AMP = 10000


def _sine_pcm16le(
    freq_hz: float, n: int = 4800, sr: int = _SR, amp: int = _AMP
) -> bytes:
    """A pure sine wave as little-endian PCM16 bytes (``struct`` is always LE)."""
    out = bytearray()
    for i in range(n):
        v = int(round(amp * math.sin(2 * math.pi * freq_hz * i / sr)))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    total = 0.0
    for i in range(n):
        (s,) = struct.unpack_from("<h", pcm, i * 2)
        total += s * s
    return math.sqrt(total / n)


def _lowpass() -> StatefulFirLowpass:
    return StatefulFirLowpass(num_taps=63, cutoff_hz=3700, sample_rate_hz=_SR)


def test_low_frequency_tone_passes_near_unity() -> None:
    # 500 Hz is well below the 3.7 kHz cutoff — it should survive intact.
    inp = _sine_pcm16le(500)
    out = _lowpass().process(inp)
    ratio = _rms(out) / _rms(inp)
    assert 0.9 < ratio < 1.1, f"expected near-unity passband, got {ratio:.3f}"


def test_high_frequency_tone_strongly_attenuated() -> None:
    # 7 kHz is almost a full octave above the cutoff — it is exactly the energy
    # that would alias into the telephony band on decimation, so it must be
    # heavily suppressed.
    inp = _sine_pcm16le(7000)
    out = _lowpass().process(inp)
    ratio = _rms(out) / _rms(inp)
    assert ratio < 0.25, f"expected strong stopband attenuation, got {ratio:.3f}"


def test_output_length_matches_input_length() -> None:
    inp = _sine_pcm16le(1000)
    out = _lowpass().process(inp)
    assert len(out) == len(inp)


def test_stateful_continuity_across_two_chunks() -> None:
    # Feeding the same signal in two consecutive chunks must reconstruct the
    # single-chunk output exactly (no per-chunk transient at the boundary) —
    # this is what makes the filter safe to run on streamed audio deltas.
    inp = _sine_pcm16le(800)
    single = _lowpass().process(inp)

    chunked_filter = _lowpass()
    half = (len(inp) // 4) * 2  # split on an even-byte (whole-sample) boundary
    first = chunked_filter.process(inp[:half])
    second = chunked_filter.process(inp[half:])
    chunked = first + second

    # Exact byte-for-byte — the chunk split changes nothing in the math.
    assert chunked == single


def test_unity_dc_gain() -> None:
    # A constant (DC) input must pass through unchanged once the filter's
    # history has filled — the coefficients are DC-normalised to unity gain.
    const = struct.pack("<h", 5000) * 4800
    out = _lowpass().process(const)
    n = len(out) // 2
    # Skip the start-up transient: the FIR only sees a full window of constant
    # input once num_taps-1 (=62) samples have flowed in.
    for i in range(63, n):
        (s,) = struct.unpack_from("<h", out, i * 2)
        assert abs(s - 5000) <= 1, f"sample {i} settled to {s}, expected ~5000"


def test_odd_trailing_byte_carried_across_calls() -> None:
    # An odd-length buffer leaves a single byte buffered; combined with the
    # next call it completes the sample. Mirrors StatefulResampler semantics.
    lp = _lowpass()
    out1 = lp.process(b"\x01\x02\x03")  # 1 whole sample + 1 carried byte
    assert len(out1) == 2
    out2 = lp.process(b"\x04")  # completes the carried sample
    assert len(out2) == 2


def test_reset_clears_history() -> None:
    lp = _lowpass()
    lp.process(_sine_pcm16le(7000))
    lp.reset()
    # After reset, filtering a fresh low tone behaves like a cold-start filter.
    inp = _sine_pcm16le(500)
    out_after_reset = lp.process(inp)
    cold = _lowpass().process(inp)
    assert out_after_reset == cold


def test_rejects_even_num_taps() -> None:
    with pytest.raises(ValueError):
        StatefulFirLowpass(num_taps=64, cutoff_hz=3700, sample_rate_hz=_SR)


def test_rejects_cutoff_above_nyquist() -> None:
    with pytest.raises(ValueError):
        StatefulFirLowpass(num_taps=63, cutoff_hz=13000, sample_rate_hz=_SR)

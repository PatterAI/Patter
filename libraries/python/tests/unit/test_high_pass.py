"""Unit tests for :class:`getpatter.audio.high_pass.BiquadHighPass`.

Real DSP: a tone synthesised here is filtered by the real biquad and the
output energy is measured. No mocks — if the filter coefficients are wrong
the attenuation assertions fail.
"""

from __future__ import annotations

import array
import math

import pytest

from getpatter.audio.high_pass import BiquadHighPass

_SR = 16000


def _sine(freq_hz: float, n: int, amplitude: float = 10000.0, sr: int = _SR) -> bytes:
    buf = array.array("h", bytes(n * 2))
    for i in range(n):
        buf[i] = int(round(amplitude * math.sin(2.0 * math.pi * freq_hz * i / sr)))
    return buf.tobytes()


def _rms(pcm: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0.0
    acc = sum(float(s) * float(s) for s in samples)
    return math.sqrt(acc / len(samples))


def _tail_rms(pcm: bytes, skip_samples: int) -> float:
    """RMS over the steady-state tail (skip the filter settling transient)."""
    return _rms(pcm[skip_samples * 2 :])


@pytest.mark.unit
class TestBiquadHighPass:
    def test_low_frequency_band_is_attenuated(self) -> None:
        hpf = BiquadHighPass(cutoff_hz=100.0, sample_rate=_SR)
        n = _SR  # 1 s
        sig = _sine(50.0, n)
        out = hpf.process(sig)
        # 50 Hz is one octave below a 100 Hz cutoff → ~12 dB for a 2nd-order
        # section. Skip the first 0.1 s of settling.
        atten_db = 20.0 * math.log10(
            _tail_rms(out, _SR // 10) / _tail_rms(sig, _SR // 10)
        )
        assert atten_db < -10.0, (
            f"expected >10 dB attenuation at 50 Hz, got {atten_db:.1f} dB"
        )

    def test_speech_band_tone_passes_through_almost_unchanged(self) -> None:
        hpf = BiquadHighPass(cutoff_hz=100.0, sample_rate=_SR)
        n = _SR
        sig = _sine(440.0, n)
        out = hpf.process(sig)
        atten_db = 20.0 * math.log10(
            _tail_rms(out, _SR // 10) / _tail_rms(sig, _SR // 10)
        )
        assert abs(atten_db) < 1.0, f"expected ~0 dB at 440 Hz, got {atten_db:.2f} dB"

    def test_dc_offset_is_removed(self) -> None:
        hpf = BiquadHighPass(cutoff_hz=100.0, sample_rate=_SR)
        n = _SR
        sig = array.array("h", [8000] * n).tobytes()  # constant DC
        out = hpf.process(sig)
        tail = array.array("h")
        tail.frombytes(out[(_SR // 5) * 2 :])  # skip 0.2 s settling
        mean = sum(tail) / len(tail)
        assert abs(mean) < 50.0, f"DC not removed: residual mean {mean:.1f}"

    def test_chunked_equals_one_shot(self) -> None:
        n = 4000
        sig = _sine(300.0, n)
        one_shot = BiquadHighPass(cutoff_hz=120.0, sample_rate=_SR).process(sig)
        chunked = BiquadHighPass(cutoff_hz=120.0, sample_rate=_SR)
        parts = []
        step = 320 * 2  # 20 ms chunks
        for off in range(0, len(sig), step):
            parts.append(chunked.process(sig[off : off + step]))
        assert b"".join(parts) == one_shot

    def test_reset_clears_state(self) -> None:
        hpf = BiquadHighPass(cutoff_hz=100.0, sample_rate=_SR)
        sig = _sine(300.0, 640)
        first = hpf.process(sig)
        hpf.reset()
        again = hpf.process(sig)
        assert again == first

    def test_invalid_cutoff_rejected(self) -> None:
        with pytest.raises(ValueError):
            BiquadHighPass(cutoff_hz=0.0, sample_rate=_SR)
        with pytest.raises(ValueError):
            BiquadHighPass(cutoff_hz=9000.0, sample_rate=_SR)  # > Nyquist

    def test_odd_length_chunk_is_carried(self) -> None:
        hpf = BiquadHighPass(cutoff_hz=100.0, sample_rate=_SR)
        # 3 bytes = one sample + a trailing odd byte carried to the next call.
        out = hpf.process(b"\x10\x00\x20")
        assert len(out) == 2  # one sample emitted, odd byte buffered
        rest = hpf.process(b"\x00")  # completes the carried sample
        assert len(rest) == 2

"""Unit tests for :class:`getpatter.audio.agc.Agc`.

Real DSP: tones at known dBFS are pushed through the real AGC and the
output level / peak are measured. No mocks.
"""

from __future__ import annotations

import array
import math

import pytest

from getpatter.audio.agc import Agc

_SR = 16000
_FRAME = 320  # 20 ms @ 16 kHz
_FULL_SCALE = 32768.0


def _sine_frame(dbfs: float, freq_hz: float = 220.0, n: int = _FRAME) -> bytes:
    rms_lin = _FULL_SCALE * (10.0 ** (dbfs / 20.0))
    amp = rms_lin * math.sqrt(2.0)
    buf = array.array("h", bytes(n * 2))
    for i in range(n):
        buf[i] = int(round(amp * math.sin(2.0 * math.pi * freq_hz * i / _SR)))
    return buf.tobytes()


def _rms_dbfs(pcm: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return -120.0
    acc = sum(float(s) * float(s) for s in samples)
    rms = math.sqrt(acc / len(samples))
    if rms <= 0:
        return -120.0
    return 20.0 * math.log10(rms / _FULL_SCALE)


def _peak(pcm: bytes) -> int:
    samples = array.array("h")
    samples.frombytes(pcm)
    return max(abs(s) for s in samples)


@pytest.mark.unit
class TestAgc:
    def test_quiet_input_is_normalised_toward_target(self) -> None:
        agc = Agc()  # target -18 dBFS
        frame = _sine_frame(-30.0)
        out = b""
        for _ in range(200):
            out = agc.process(frame)
        assert abs(_rms_dbfs(out) - (-18.0)) < 2.0

    def test_input_already_at_target_stays_stable(self) -> None:
        agc = Agc()
        frame = _sine_frame(-18.0)
        for _ in range(60):
            out = agc.process(frame)
            assert abs(_rms_dbfs(out) - (-18.0)) < 1.5

    def test_no_clipping_under_aggressive_boost(self) -> None:
        # High target + a -10 dBFS sine forces gain*peak past full scale, so
        # the limiter must engage.
        agc = Agc(target_rms_dbfs=-3.0)
        frame = _sine_frame(-10.0, freq_hz=300.0)
        out = b""
        for _ in range(200):
            out = agc.process(frame)
        ceiling = int(0.99 * _FULL_SCALE)
        peak = _peak(out)
        assert peak <= ceiling, f"limiter failed: peak {peak} > ceiling {ceiling}"
        assert peak >= 30000, f"limiter never engaged (peak {peak})"
        # No wraparound to the opposite rail.
        samples = array.array("h")
        samples.frombytes(out)
        assert all(-32768 <= s <= 32767 for s in samples)

    def test_noise_floor_not_pumped_in_silence_gaps(self) -> None:
        agc = Agc()
        speech = _sine_frame(-30.0)
        for _ in range(120):  # drive the gain up on speech
            agc.process(speech)
        noise = _sine_frame(-55.0)  # below the -45 dBFS speech floor
        out = b""
        for _ in range(60):
            out = agc.process(noise)
        # If the gain had stayed at the speech value (~+12 dB) the -55 dBFS
        # noise would be pumped to ~-43 dBFS. Speech-selective release keeps
        # it near its original level.
        assert _rms_dbfs(out) < -48.0, f"noise pumped to {_rms_dbfs(out):.1f} dBFS"

    def test_invalid_config_rejected(self) -> None:
        with pytest.raises(ValueError):
            Agc(target_rms_dbfs=0.0)
        with pytest.raises(ValueError):
            Agc(limiter_ceiling=1.5)
        with pytest.raises(ValueError):
            Agc(max_gain_db=0.0)

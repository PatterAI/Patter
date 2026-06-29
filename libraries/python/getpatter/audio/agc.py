"""Speech-selective automatic gain control (AGC) for the inbound chain.

Cheap STT models and small TTS pipelines are sensitive to input level:
a caller far from the mic (-35 dBFS) and one speaking right into it
(-10 dBFS) hit the recogniser at wildly different energies, and energy
normalisation toward a consistent target is a well-established way to cut
word-error rate on quiet/variable input. This AGC normalises the inbound
PCM16 stream toward a target RMS just before VAD/STT.

Two properties keep it safe on a phone line:

* **Speech-selective.** Gain is only driven *up* on frames whose RMS sits
  above a speech floor. On silence / line-noise frames the gain is
  released back toward unity, so the quiet gaps between words are never
  amplified into a hiss (the classic "AGC pumping the noise floor"
  artefact).
* **Limited.** After gain is applied, a per-frame peak limiter scales the
  frame so its peak never exceeds a ceiling (default 99 % of full scale).
  That prevents the hard digital clipping that an aggressive boost would
  otherwise produce on a loud transient.

Gain changes are smoothed with separate attack (gain decreasing — fast,
to catch a sudden loud talker before it clips) and release (gain
increasing — slow, so a quiet talker is brought up smoothly without
audible breathing) time constants.

Pure-Python (no numpy) so the opt-in AGC runs without the optional numpy
extra. Mirrors TypeScript ``src/audio/agc.ts``.
"""

from __future__ import annotations

import array
import logging
import math
from typing import Final

logger = logging.getLogger("getpatter")

# int16 full-scale reference (0 dBFS) — a full-scale sine peaks at this.
_FULL_SCALE: Final[float] = 32768.0


def _dbfs_to_linear(dbfs: float) -> float:
    """Convert a dBFS level to a linear int16 amplitude (0 dBFS = 32768)."""
    return _FULL_SCALE * (10.0 ** (dbfs / 20.0))


def _clamp_int16(value: int) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


class Agc:
    """Stateful speech-selective AGC for PCM16-LE mono frames.

    Args:
        sample_rate: Stream sample rate, Hz (drives the attack/release
            per-frame smoothing coefficients).
        target_rms_dbfs: Desired output RMS level in dBFS. Default -18.
        max_gain_db: Symmetric gain bound — gain is clamped to
            ``[10**(-max_gain_db/20), 10**(max_gain_db/20)]`` so the noise
            floor is never amplified by more than ``max_gain_db``. Default 30.
        speech_floor_dbfs: Frames with RMS below this are treated as
            non-speech; their gain target is unity (release toward 1.0).
            Default -45.
        attack_ms: Time constant when the gain is *decreasing* (signal got
            louder). Fast. Default 10 ms.
        release_ms: Time constant when the gain is *increasing* (signal got
            quieter). Slow. Default 200 ms.
        limiter_ceiling: Peak ceiling as a fraction of full scale. Default
            0.99.

    Thread-safety: NOT thread-safe. One instance per call session.
    """

    __slots__ = (
        "_sample_rate",
        "_target_rms",
        "_max_gain",
        "_min_gain",
        "_speech_floor",
        "_attack_ms",
        "_release_ms",
        "_ceiling",
        "_gain",
    )

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        target_rms_dbfs: float = -18.0,
        max_gain_db: float = 30.0,
        speech_floor_dbfs: float = -45.0,
        attack_ms: float = 10.0,
        release_ms: float = 200.0,
        limiter_ceiling: float = 0.99,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0; got {sample_rate}.")
        if target_rms_dbfs >= 0:
            raise ValueError(
                f"target_rms_dbfs must be < 0 dBFS; got {target_rms_dbfs}."
            )
        if max_gain_db <= 0:
            raise ValueError(f"max_gain_db must be > 0; got {max_gain_db}.")
        if attack_ms <= 0 or release_ms <= 0:
            raise ValueError(
                f"attack_ms/release_ms must be > 0; got {attack_ms}/{release_ms}."
            )
        if not 0 < limiter_ceiling <= 1.0:
            raise ValueError(
                f"limiter_ceiling must be in (0, 1]; got {limiter_ceiling}."
            )

        self._sample_rate = int(sample_rate)
        self._target_rms = _dbfs_to_linear(target_rms_dbfs)
        self._max_gain = 10.0 ** (max_gain_db / 20.0)
        self._min_gain = 10.0 ** (-max_gain_db / 20.0)
        self._speech_floor = _dbfs_to_linear(speech_floor_dbfs)
        self._attack_ms = float(attack_ms)
        self._release_ms = float(release_ms)
        self._ceiling = limiter_ceiling * _FULL_SCALE
        # Current smoothed gain (linear). Starts at unity so the very first
        # frame is untouched and the gain ramps in over the next frames.
        self._gain = 1.0

    def process(self, pcm: bytes) -> bytes:
        """Normalise one PCM16-LE frame toward the target RMS.

        Returns the gain-adjusted, peak-limited frame (same sample count).
        """
        if not pcm:
            return pcm
        usable = pcm if len(pcm) % 2 == 0 else pcm[:-1]
        if not usable:
            return pcm

        samples = array.array("h")
        samples.frombytes(usable)
        n = len(samples)

        # Frame RMS.
        acc = 0.0
        for s in samples:
            acc += float(s) * float(s)
        rms = math.sqrt(acc / n) if n else 0.0

        # Desired instantaneous gain. On non-speech frames release toward
        # unity so the noise floor is never pumped up.
        if rms >= self._speech_floor and rms > 0.0:
            desired = self._target_rms / rms
            if desired > self._max_gain:
                desired = self._max_gain
            elif desired < self._min_gain:
                desired = self._min_gain
        else:
            desired = 1.0

        # Smooth toward the target: fast attack when gain decreases (signal
        # got louder), slow release when gain increases (signal got quieter).
        frame_ms = 1000.0 * n / self._sample_rate
        tau = self._release_ms if desired > self._gain else self._attack_ms
        coef = 1.0 - math.exp(-frame_ms / tau)
        self._gain += (desired - self._gain) * coef
        gain = self._gain

        # Peak limiter: never let the gained frame exceed the ceiling.
        peak = 0.0
        for s in samples:
            mag = abs(float(s)) * gain
            if mag > peak:
                peak = mag
        limit_scale = self._ceiling / peak if peak > self._ceiling else 1.0
        applied = gain * limit_scale

        out = array.array("h", bytes(n * 2))
        for i in range(n):
            out[i] = _clamp_int16(int(round(float(samples[i]) * applied)))
        return out.tobytes()

    def reset(self) -> None:
        """Reset the smoothed gain to unity (e.g. at a call boundary)."""
        self._gain = 1.0

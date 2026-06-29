"""High-pass / DC-block biquad for the inbound audio-processing chain.

The first canonical stage of a WebRTC-style audio-processing module (APM)
is a high-pass filter: it strips DC offset, mains hum (50/60 Hz), and
sub-100 Hz handling rumble *before* echo cancellation, noise suppression,
VAD, and STT run. Those low-frequency components carry no speech
information yet bias the AEC's adaptive filter, inflate the VAD's energy
estimate, and waste STT compute — removing them up front improves every
downstream stage at negligible cost.

This module implements a 2nd-order RBJ (Robert Bristow-Johnson "Audio EQ
Cookbook") high-pass biquad in Direct-Form I, stateful across chunk
boundaries so a streamed signal filters identically to a one-shot pass.
A 2nd-order section rolls off at 12 dB/octave below the cutoff; with the
default Butterworth ``Q`` (0.707) the pass-band is maximally flat, so a
440 Hz tone above a 100 Hz cutoff passes essentially unchanged while a
50 Hz component is attenuated ~12 dB.

The filter is pure-Python (no numpy): an IIR recursion cannot be
vectorised (each output depends on the previous one), and a 2nd-order
section is only ~5 multiply-adds per sample — ~80k ops/s for a 16 kHz
stream, far below 1 % of one core. Keeping it numpy-free lets the opt-in
high-pass run without the optional ``silero``/numpy extra.

Mirrors TypeScript ``src/audio/high-pass.ts``.
"""

from __future__ import annotations

import array
import logging
import math
from typing import Final

logger = logging.getLogger("getpatter")

# Butterworth quality factor — 1/sqrt(2). Maximally flat pass-band.
_DEFAULT_Q: Final[float] = 0.7071067811865476


def _clamp_int16(value: int) -> int:
    """Clamp an int to the signed 16-bit range (no wraparound)."""
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


class BiquadHighPass:
    """Stateful 2nd-order high-pass / DC-block biquad (RBJ cookbook).

    Args:
        cutoff_hz: -3 dB corner frequency. Must be in ``(0, sample_rate/2)``.
            Typical telephony value is 80-120 Hz.
        sample_rate: Sample rate of the stream being filtered, in Hz.
        q: Filter quality factor. Defaults to Butterworth (``0.707``) for a
            maximally flat pass-band.

    Thread-safety: NOT thread-safe. Each call session owns its own instance.
    """

    __slots__ = (
        "_b0",
        "_b1",
        "_b2",
        "_a1",
        "_a2",
        "_x1",
        "_x2",
        "_y1",
        "_y2",
        "_cutoff_hz",
        "_sample_rate",
        "_carry",
    )

    def __init__(
        self,
        cutoff_hz: float,
        sample_rate: int,
        q: float = _DEFAULT_Q,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0; got {sample_rate}.")
        nyquist = sample_rate / 2.0
        if cutoff_hz <= 0 or cutoff_hz >= nyquist:
            raise ValueError(f"cutoff_hz must be in (0, {nyquist}); got {cutoff_hz}.")
        if q <= 0:
            raise ValueError(f"q must be > 0; got {q}.")

        self._cutoff_hz = float(cutoff_hz)
        self._sample_rate = int(sample_rate)

        w0 = 2.0 * math.pi * cutoff_hz / sample_rate
        cos_w0 = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * q)

        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha

        # Normalise by a0 so the recursion needs no per-sample division.
        self._b0 = b0 / a0
        self._b1 = b1 / a0
        self._b2 = b2 / a0
        self._a1 = a1 / a0
        self._a2 = a2 / a0

        # Direct-Form I delay line (previous two inputs / outputs).
        self._x1 = 0.0
        self._x2 = 0.0
        self._y1 = 0.0
        self._y2 = 0.0
        # Carry one trailing byte if a chunk arrives odd-length (decoded PCM16
        # is always even, but stay robust to upstream mis-framing).
        self._carry = b""

    def process(self, pcm: bytes) -> bytes:
        """Filter a chunk of PCM16-LE mono samples.

        Returns the same number of samples it consumed (minus any trailing
        odd byte, which is carried to the next call). State persists across
        calls so chunked processing equals one-shot processing.
        """
        if not pcm:
            return pcm
        if self._carry:
            pcm = self._carry + pcm
            self._carry = b""
        if len(pcm) % 2:
            self._carry = pcm[-1:]
            pcm = pcm[:-1]
        if not pcm:
            return b""

        samples = array.array("h")
        samples.frombytes(pcm)
        n = len(samples)
        out = array.array("h", bytes(n * 2))

        b0 = self._b0
        b1 = self._b1
        b2 = self._b2
        a1 = self._a1
        a2 = self._a2
        x1 = self._x1
        x2 = self._x2
        y1 = self._y1
        y2 = self._y2

        for i in range(n):
            x0 = float(samples[i])
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2 = x1
            x1 = x0
            y2 = y1
            y1 = y0
            out[i] = _clamp_int16(int(round(y0)))

        self._x1 = x1
        self._x2 = x2
        self._y1 = y1
        self._y2 = y2
        return out.tobytes()

    def reset(self) -> None:
        """Clear the delay line and byte carry (e.g. at a call boundary)."""
        self._x1 = 0.0
        self._x2 = 0.0
        self._y1 = 0.0
        self._y2 = 0.0
        self._carry = b""

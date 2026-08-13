"""PCM transcoding utilities — mu-law conversion, resampling, byte-alignment.

Public API
----------
- ``mulaw_to_pcm16`` / ``pcm16_to_mulaw``  — mu-law codec conversion
- ``alaw_to_pcm16`` / ``alaw_to_mulaw``  — A-law decode/normalization
- ``resample_8k_to_16k`` / ``resample_16k_to_8k``  — **deprecated** one-shot helpers
- ``PcmCarry``  — odd-byte alignment helper
- ``StatefulResampler``  — stateful chunk-by-chunk resampler (preferred)
- ``create_resampler_8k_to_16k`` / ``create_resampler_16k_to_8k``
  / ``create_resampler_24k_to_16k`` / ``create_resampler_24k_to_8k``
  — convenience factories
"""

from __future__ import annotations

import array
import logging
import math
import sys
import warnings
from typing import Optional, Tuple

logger = logging.getLogger("getpatter")

try:
    import audioop  # type: ignore[import]
except ImportError:
    try:
        import audioop_lts as audioop  # type: ignore[import,no-redef]
    except ImportError:
        audioop = None  # type: ignore[assignment]


__all__ = [
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
    "alaw_to_pcm16",
    "alaw_to_mulaw",
    "resample_8k_to_16k",
    "resample_16k_to_8k",
    "resample_24k_to_16k",
    "PcmCarry",
    "StatefulResampler",
    "StatefulFirLowpass",
    "create_resampler_8k_to_16k",
    "create_resampler_16k_to_8k",
    "create_resampler_24k_to_16k",
    "create_resampler_24k_to_8k",
]

_AUDIOOP_MISSING_MSG = "audioop required: pip install getpatter[local]"


# ---------------------------------------------------------------------------
# Codec conversion helpers
# ---------------------------------------------------------------------------


def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    """Decode mu-law (G.711) bytes to signed 16-bit linear PCM."""
    if audioop is None:
        raise ImportError(_AUDIOOP_MISSING_MSG)
    return audioop.ulaw2lin(mulaw_data, 2)


def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
    """Encode signed 16-bit linear PCM bytes to mu-law (G.711)."""
    if audioop is None:
        raise ImportError(_AUDIOOP_MISSING_MSG)
    return audioop.lin2ulaw(pcm_data, 2)


def alaw_to_pcm16(alaw_data: bytes) -> bytes:
    """Decode A-law (G.711) bytes to signed 16-bit linear PCM."""
    if audioop is None:
        raise ImportError(_AUDIOOP_MISSING_MSG)
    return audioop.alaw2lin(alaw_data, 2)


def alaw_to_mulaw(alaw_data: bytes) -> bytes:
    """Normalize A-law (PCMA) bytes to the SDK-wide mu-law contract."""
    if audioop is None:
        raise ImportError(_AUDIOOP_MISSING_MSG)
    return audioop.lin2ulaw(audioop.alaw2lin(alaw_data, 2), 2)


# ---------------------------------------------------------------------------
# PcmCarry — odd-byte alignment buffer
# ---------------------------------------------------------------------------


class PcmCarry:
    """Odd-byte carry buffer for PCM streams.

    HTTP streaming TTS providers (ElevenLabs, Cartesia, LMNT, Rime,
    Telnyx) yield chunks of arbitrary byte length, including odd
    counts. Passing an odd-length buffer to ``audioop.ratecv`` raises
    ``audioop.error: not a whole number of frames``, crashing the TTS
    mid-sentence. Prepend any leftover byte from the previous chunk,
    return the even-length portion, and stash the trailing odd byte for
    the next call. Mirrors TS ``StreamHandler.alignPcm16``.

    Parameters
    ----------
    sample_width:
        Bytes per sample (default 2 for PCM16). Alignment ensures
        ``len(result) % sample_width == 0``.
    """

    __slots__ = ("_carry", "_sample_width")

    def __init__(self, sample_width: int = 2) -> None:
        if sample_width < 1:
            raise ValueError(f"sample_width must be >= 1, got {sample_width}")
        self._sample_width = sample_width
        self._carry: bytes = b""

    # ------------------------------------------------------------------
    # Public contract (used by StatefulResampler and callers directly)
    # ------------------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        """Buffer odd-length tail; return aligned bytes ready for ratecv.

        Parameters
        ----------
        data:
            Arbitrary-length PCM bytes.

        Returns
        -------
        bytes
            ``data`` (prefixed with any carry from the previous call) trimmed
            to a whole number of ``sample_width``-byte frames.  The remaining
            trailing bytes are buffered internally for the next call.
        """
        combined = self._carry + data if self._carry else data
        remainder = len(combined) % self._sample_width
        if remainder:
            self._carry = combined[-remainder:]
            return combined[:-remainder]
        self._carry = b""
        return combined

    def flush(self) -> bytes:
        """Return any remaining carry bytes and clear the buffer."""
        out = self._carry
        self._carry = b""
        return out

    def reset(self) -> None:
        """Drop any buffered carry bytes. Call at each stream boundary."""
        self._carry = b""

    # ------------------------------------------------------------------
    # Legacy alias kept for callers that use .align()
    # ------------------------------------------------------------------

    def align(self, chunk: bytes) -> bytes:
        """Alias for :meth:`feed` retained for backward compatibility."""
        return self.feed(chunk)


# ---------------------------------------------------------------------------
# StatefulResampler
# ---------------------------------------------------------------------------


class StatefulResampler:
    """Chunk-by-chunk PCM resampler that preserves ``audioop.ratecv`` state.

    Each :meth:`process` call feeds PCM bytes through ``audioop.ratecv``
    while carrying the filter state forward, preventing the "pop" artefacts
    that occur when stateless calls reset the FIR filter at every chunk
    boundary.  Odd-byte alignment is handled internally via :class:`PcmCarry`.

    Parameters
    ----------
    src_rate:
        Input sample rate in Hz (e.g. 8000).
    dst_rate:
        Output sample rate in Hz (e.g. 16000).
    channels:
        Number of interleaved channels (default 1 = mono).
    sample_width:
        Bytes per sample (default 2 = PCM16 / signed 16-bit LE).

    Raises
    ------
    RuntimeError
        If neither ``audioop`` (Python ≤ 3.12) nor ``audioop_lts``
        (Python 3.13+) is importable.

    Example
    -------
    ::

        resampler = StatefulResampler(8000, 16000)
        for chunk in stream:
            output.extend(resampler.process(chunk))
        output.extend(resampler.flush())
    """

    def __init__(
        self,
        src_rate: int,
        dst_rate: int,
        channels: int = 1,
        sample_width: int = 2,
    ) -> None:
        if audioop is None:
            raise RuntimeError(
                "StatefulResampler requires 'audioop' (Python ≤3.12) or "
                "'audioop-lts' (Python 3.13+). "
                "Install via: pip install getpatter[local]"
            )
        self._src_rate = src_rate
        self._dst_rate = dst_rate
        self._channels = channels
        self._sample_width = sample_width
        self._carry = PcmCarry(sample_width)
        # audioop.ratecv state: None means "first call / cold start"
        self._state: Optional[Tuple] = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, pcm_bytes: bytes) -> bytes:
        """Resample a chunk of PCM audio.

        Handles odd-byte alignment internally. Returns resampled bytes;
        may return ``b""`` if the chunk is too small to form a full sample
        frame (the bytes are buffered for the next call).

        Parameters
        ----------
        pcm_bytes:
            Raw PCM bytes at ``src_rate``.

        Returns
        -------
        bytes
            Resampled PCM bytes at ``dst_rate``.
        """
        aligned = self._carry.feed(pcm_bytes)
        if not aligned:
            return b""
        resampled, self._state = audioop.ratecv(
            aligned,
            self._sample_width,
            self._channels,
            self._src_rate,
            self._dst_rate,
            self._state,
        )
        return resampled

    def flush(self) -> bytes:
        """Drain any buffered carry byte and return final resampled output.

        Call once after the last :meth:`process` call to ensure no trailing
        audio is lost.  Resets internal state so the instance is ready to
        begin a new stream.
        """
        carry = self._carry.flush()
        out = b""
        if carry:
            # ``PcmCarry`` only ever buffers a PARTIAL frame (len % width),
            # so a non-empty carry can never be a whole number of frames —
            # feeding it to ratecv raised ``audioop.error`` on every odd-
            # length input (including the deprecated helper wrappers and the
            # OpenAI-TTS tail flush). Drop the sub-sample remainder with a
            # debug log, matching the TS resampler.
            logger.debug(
                "StatefulResampler.flush: dropping %d sub-frame carry byte(s)",
                len(carry),
            )
        self.reset()
        return out

    def reset(self) -> None:
        """Reset internal state for a new stream without recreating the object."""
        self._carry.reset()
        self._state = None


# ---------------------------------------------------------------------------
# StatefulFirLowpass — windowed-sinc anti-alias low-pass
# ---------------------------------------------------------------------------


def _pcm16le_to_int_list(data: bytes) -> array.array:
    """Decode little-endian PCM16 bytes to a signed-int ``array('h')``."""
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        # ``array('h')`` parses in native order; the wire format is LE.
        samples.byteswap()
    return samples


def _int_list_to_pcm16le(samples: array.array) -> bytes:
    """Encode a signed-int ``array('h')`` to little-endian PCM16 bytes."""
    if sys.byteorder == "big":
        swapped = array.array("h", samples)
        swapped.byteswap()
        return swapped.tobytes()
    return samples.tobytes()


class StatefulFirLowpass:
    """Linear-phase FIR low-pass with cross-chunk history — an anti-alias
    pre-filter to run BEFORE decimation.

    Why this exists: decimating ``gpt-realtime-2``'s 24 kHz output straight
    down to 8 kHz aliases any energy above 4 kHz into the telephony band,
    heard as raspy / crackly speech. The 24→16 (linear) + 16→8 resampler
    chain under-attenuates that band. Running this low-pass on the 24 kHz
    signal FIRST removes everything above the cutoff, so the downstream
    decimation has nothing left to alias.

    Coefficients are a Hamming-windowed sinc (~53 dB stopband), DC-normalised
    to unity gain. State is the previous ``num_taps - 1`` input samples,
    carried across :meth:`process` calls so the output is continuous (no
    per-chunk transient). Group delay is a constant ``(num_taps - 1) / 2``
    samples — inaudible at these tap counts. Odd trailing bytes are buffered
    across calls via :class:`PcmCarry`, mirroring :class:`StatefulResampler`.

    Parameters
    ----------
    num_taps:
        Number of FIR taps. Must be a positive ODD integer (linear phase).
    cutoff_hz:
        -6 dB cutoff frequency in Hz.
    sample_rate_hz:
        Sample rate of the stream being filtered, in Hz.
    """

    __slots__ = ("_coeffs", "_num_taps", "_history", "_carry")

    def __init__(self, num_taps: int, cutoff_hz: float, sample_rate_hz: float) -> None:
        if num_taps < 1 or num_taps % 2 == 0:
            raise ValueError(
                f"StatefulFirLowpass: num_taps must be a positive odd integer, "
                f"got {num_taps}"
            )
        if cutoff_hz <= 0 or cutoff_hz >= sample_rate_hz / 2:
            raise ValueError(
                f"StatefulFirLowpass: cutoff_hz must be in (0, "
                f"{sample_rate_hz / 2}), got {cutoff_hz}"
            )
        self._num_taps = num_taps
        self._coeffs = self._design(num_taps, cutoff_hz, sample_rate_hz)
        # Last ``num_taps - 1`` input samples: history[k] = x[k - (num_taps-1)].
        self._history: list[float] = [0.0] * (num_taps - 1)
        self._carry = PcmCarry(2)

    @staticmethod
    def _design(num_taps: int, cutoff_hz: float, sample_rate_hz: float) -> list[float]:
        """Hamming-windowed sinc low-pass, DC-normalised to unity gain."""
        fc = cutoff_hz / sample_rate_hz  # normalised cutoff (cycles/sample)
        m = num_taps - 1
        coeffs = [0.0] * num_taps
        total = 0.0
        for n in range(num_taps):
            k = n - m / 2
            if k == 0:
                sinc = 2 * fc
            else:
                sinc = math.sin(2 * math.pi * fc * k) / (math.pi * k)
            window = 0.54 - 0.46 * math.cos((2 * math.pi * n) / m)  # Hamming
            coeffs[n] = sinc * window
            total += coeffs[n]
        return [c / total for c in coeffs]  # unity DC gain

    def process(self, pcm: bytes) -> bytes:
        """Filter a chunk of PCM16-LE samples, returning the same number of
        samples. Odd trailing bytes are buffered across calls.
        """
        aligned = self._carry.feed(pcm)
        if not aligned:
            return b""
        x = _pcm16le_to_int_list(aligned)
        n = len(x)

        taps = self._num_taps
        keep = taps - 1
        c = self._coeffs
        hist = self._history
        out = array.array("h", bytes(n * 2))

        for i in range(n):
            acc = 0.0
            # y[i] = sum_{j=0..taps-1} c[j] * x[i - (taps-1) + j]
            for j in range(taps):
                idx = i - keep + j
                s = x[idx] if idx >= 0 else hist[keep + idx]
                acc += c[j] * s
            out[i] = _clamp_int16(int(round(acc)))

        # New history = last ``keep`` input samples of the virtual stream.
        # Build into a temp first so small chunks (n < keep) don't read
        # overwritten slots.
        nxt = [0.0] * keep
        for k in range(keep):
            idx = n - keep + k
            nxt[k] = float(x[idx]) if idx >= 0 else hist[keep + idx]
        self._history = nxt

        return _int_list_to_pcm16le(out)

    def reset(self) -> None:
        """Clear filter history (e.g. at call boundaries)."""
        self._history = [0.0] * (self._num_taps - 1)
        self._carry.reset()


def _clamp_int16(value: int) -> int:
    """Clamp an int to the signed 16-bit range."""
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


# ---------------------------------------------------------------------------
# Convenience factories
# ---------------------------------------------------------------------------


def create_resampler_8k_to_16k() -> StatefulResampler:
    """Return a :class:`StatefulResampler` configured for 8 kHz → 16 kHz."""
    return StatefulResampler(src_rate=8000, dst_rate=16000)


def create_resampler_16k_to_8k() -> StatefulResampler:
    """Return a :class:`StatefulResampler` configured for 16 kHz → 8 kHz."""
    return StatefulResampler(src_rate=16000, dst_rate=8000)


def create_resampler_24k_to_16k() -> StatefulResampler:
    """Return a :class:`StatefulResampler` configured for 24 kHz → 16 kHz."""
    return StatefulResampler(src_rate=24000, dst_rate=16000)


def create_resampler_24k_to_8k() -> StatefulResampler:
    """Return a :class:`StatefulResampler` configured for 24 kHz → 8 kHz."""
    # Single ratecv state collapses the 24k→16k→8k chain into one step.
    return StatefulResampler(src_rate=24000, dst_rate=8000)


# ---------------------------------------------------------------------------
# Deprecated one-shot helpers (stateless — lose ratecv state across calls)
# ---------------------------------------------------------------------------

# Per-function once-per-process flags so the DeprecationWarning fires only once
# rather than on every call — avoids spam in hot audio paths.
_warned_resample_8k_16k: bool = False
_warned_resample_16k_8k: bool = False
_warned_resample_24k_16k: bool = False


def resample_8k_to_16k(audio_data: bytes) -> bytes:
    """Resample 8kHz PCM16 to 16kHz using audioop.ratecv.

    .. deprecated::
        Stateless: filter state is discarded between calls.  Use
        :class:`StatefulResampler` or :func:`create_resampler_8k_to_16k`.
    """
    global _warned_resample_8k_16k
    if not _warned_resample_8k_16k:
        warnings.warn(
            "resample_8k_to_16k() is a deprecated stateless helper that loses "
            "audioop.ratecv filter state across chunks. Use StatefulResampler or "
            "create_resampler_8k_to_16k() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _warned_resample_8k_16k = True
    if audioop is None:
        raise ImportError(_AUDIOOP_MISSING_MSG)
    if not audio_data:
        return audio_data
    resampler = StatefulResampler(8000, 16000)
    return resampler.process(audio_data) + resampler.flush()


def resample_16k_to_8k(audio_data: bytes) -> bytes:
    """Resample 16kHz PCM16 to 8kHz using audioop.ratecv with anti-aliasing.

    .. deprecated::
        Stateless: filter state is discarded between calls.  Use
        :class:`StatefulResampler` or :func:`create_resampler_16k_to_8k`.
    """
    global _warned_resample_16k_8k
    if not _warned_resample_16k_8k:
        warnings.warn(
            "resample_16k_to_8k() is a deprecated stateless helper that loses "
            "audioop.ratecv filter state across chunks. Use StatefulResampler or "
            "create_resampler_16k_to_8k() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _warned_resample_16k_8k = True
    if audioop is None:
        raise ImportError(_AUDIOOP_MISSING_MSG)
    if not audio_data:
        return audio_data
    resampler = StatefulResampler(16000, 8000)
    return resampler.process(audio_data) + resampler.flush()


def resample_24k_to_16k(audio_data: bytes) -> bytes:
    """Resample 24kHz PCM16 to 16kHz using audioop.ratecv (3:2 ratio).

    Parity with TypeScript ``resample24kTo16k`` in
    ``libraries/typescript/src/audio/transcoding.ts``.

    .. deprecated::
        Stateless: filter state is discarded between calls. Use
        :class:`StatefulResampler` or :func:`create_resampler_24k_to_16k`
        for streaming pipelines where chunk-boundary continuity matters.
    """
    global _warned_resample_24k_16k
    if not _warned_resample_24k_16k:
        warnings.warn(
            "resample_24k_to_16k() is a deprecated stateless helper that loses "
            "audioop.ratecv filter state across chunks. Use StatefulResampler or "
            "create_resampler_24k_to_16k() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _warned_resample_24k_16k = True
    if audioop is None:
        raise ImportError(_AUDIOOP_MISSING_MSG)
    if not audio_data:
        return audio_data
    resampler = StatefulResampler(24000, 16000)
    return resampler.process(audio_data) + resampler.flush()

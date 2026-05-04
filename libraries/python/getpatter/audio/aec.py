"""Acoustic echo cancellation for speakerphone telephony.

The pipeline-mode VAD runs on the inbound mic stream. On a speakerphone
or laptop-mic deployment, the agent's outbound TTS leaks back into the
mic — VAD then sees continuous voice-like energy from the bleed and
cannot detect when the caller starts speaking. The barge-in only fires
during natural pauses in the TTS, which produces the intermittent
"interrupt sometimes works, other times the agent keeps talking" symptom.

This module implements an NLMS (normalised least-mean-squares) adaptive
filter that subtracts the estimated echo from the inbound signal. It is
NOT a drop-in replacement for production-grade echo cancellation
(WebRTC's AEC3, Speex AEC). For tight integration with battle-tested
DSP, wrap a binding to ``libwebrtc-audio-processing`` externally.

Wiring::

    aec = NlmsEchoCanceller(sample_rate=16000)
    # In TTS path: every chunk we ship to the carrier is also fed to AEC.
    aec.push_far_end(tts_pcm_bytes)
    # In mic path: subtract estimated echo before VAD/STT.
    cleaned = aec.process_near_end(mic_pcm_bytes)
    vad.process_frame(cleaned, 16000)
"""

from __future__ import annotations

import logging
from typing import Final

import numpy as np

logger = logging.getLogger("getpatter")


_DEFAULT_FILTER_TAPS: Final[int] = 2048
"""Length of the adaptive filter in samples. 2048 taps @ 16 kHz = 128 ms,
which covers the typical end-to-end loop latency on telephony links
(carrier + cloudflare tunnel + speakerphone path)."""

_DEFAULT_STEP_SIZE: Final[float] = 0.1
"""NLMS step size. Larger = faster convergence but less stable; lower =
more stable but slower to track changes. 0.1 is the standard textbook
value for narrowband voice."""

_DEFAULT_LEAKAGE: Final[float] = 0.9999
"""Per-iteration leakage on the filter weights. Slightly less than 1 so
the filter slowly forgets stale tap estimates if the echo path changes
mid-call (e.g. caller moves the phone)."""

_DOUBLE_TALK_RHO: Final[float] = 0.6
"""Geigel double-talk threshold. When ``max(|near|) > rho * max(|far|)``
the near-end signal contains energy that the far-end alone cannot
explain → freeze adaptation to avoid the filter mistakenly learning the
caller's voice as part of the echo path."""

_FAR_END_BUFFER_SECONDS: Final[float] = 0.5
"""How much past far-end (TTS) audio we retain. The echo arrives at the
mic 50–200 ms after we played it; the filter needs at least that much
look-back to find the right alignment. 500 ms is generous and covers
even satellite-routed calls."""


class NlmsEchoCanceller:
    """Time-domain NLMS adaptive filter with Geigel double-talk detection.

    Designed for narrowband mono 16 kHz PCM (the format Patter's pipeline
    pushes between transcoding and STT). 8 kHz is also accepted but the
    default tap count translates to a 256 ms history at that rate which
    costs more CPU per frame for diminishing return — for 8 kHz callers
    pass ``filter_taps=1024`` explicitly.

    Thread-safety: NOT thread-safe. Each call session must own its own
    instance. The stream handler creates one per ``StreamHandler``.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        *,
        filter_taps: int = _DEFAULT_FILTER_TAPS,
        step_size: float = _DEFAULT_STEP_SIZE,
        leakage: float = _DEFAULT_LEAKAGE,
        double_talk_rho: float = _DOUBLE_TALK_RHO,
    ) -> None:
        if sample_rate not in (8000, 16000):
            raise ValueError(
                "NlmsEchoCanceller supports 8000 Hz or 16000 Hz only; "
                f"got {sample_rate}."
            )
        if filter_taps < 64:
            raise ValueError(
                f"filter_taps must be >= 64 to model a meaningful echo path; got {filter_taps}."
            )
        if not 0 < step_size <= 1:
            raise ValueError(f"step_size must be in (0, 1]; got {step_size}.")
        if not 0 < leakage <= 1:
            raise ValueError(f"leakage must be in (0, 1]; got {leakage}.")

        self._sample_rate = sample_rate
        self._taps = filter_taps
        self._step = float(step_size)
        self._leakage = float(leakage)
        self._rho = float(double_talk_rho)

        # Filter coefficients (init to zeros — the filter will adapt to
        # match the channel impulse response within 0.5–2 s of TTS).
        self._w = np.zeros(filter_taps, dtype=np.float32)

        # Far-end ring buffer: stores at least filter_taps samples of TTS
        # history so the filter can convolve against past samples. Add
        # extra headroom so push/process can be called out-of-step.
        max_buf_samples = max(
            filter_taps * 2,
            int(sample_rate * _FAR_END_BUFFER_SECONDS),
        )
        self._far_buf = np.zeros(max_buf_samples, dtype=np.float32)
        self._far_write_idx = 0  # next write position (head)
        self._far_filled = 0  # samples written so far (capped at len(far_buf))

        # Snapshot of frame-level stats for diagnostics (slow_callback duration
        # debugging). Only updated, never read inside the hot path.
        self.frames_processed: int = 0
        self.double_talk_frames: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push_far_end(self, pcm_bytes: bytes) -> None:
        """Append far-end (TTS) audio to the reference ring buffer.

        Accepts raw int16 little-endian mono PCM at the configured sample
        rate — same shape as what we hand off to ``audio_sender`` before
        the carrier-specific transcode.
        """
        if not pcm_bytes:
            return
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        n = samples.shape[0]
        buf_len = self._far_buf.shape[0]
        if n >= buf_len:
            # Caller pushed more than we can hold — keep only the most
            # recent ``buf_len`` samples and reset the head.
            self._far_buf[:] = samples[-buf_len:]
            self._far_write_idx = 0
            self._far_filled = buf_len
            return
        end = self._far_write_idx + n
        if end <= buf_len:
            self._far_buf[self._far_write_idx : end] = samples
        else:
            head = buf_len - self._far_write_idx
            self._far_buf[self._far_write_idx :] = samples[:head]
            self._far_buf[: n - head] = samples[head:]
        self._far_write_idx = (self._far_write_idx + n) % buf_len
        self._far_filled = min(self._far_filled + n, buf_len)

    def process_near_end(self, pcm_bytes: bytes) -> bytes:
        """Subtract estimated echo from the near-end (mic) signal.

        Returns int16 little-endian mono PCM with the estimated echo
        removed. When the far-end buffer hasn't been primed yet (no TTS
        has played) the call is a pass-through — there is nothing to
        cancel.
        """
        if not pcm_bytes:
            return pcm_bytes

        # Pass-through when we don't have enough far-end history to fill
        # the filter window. This avoids the filter producing garbage
        # during the very first speech frame of the call.
        if self._far_filled < self._taps:
            return pcm_bytes

        near = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        cleaned = self._block_nlms(near)
        # Clip to int16 range, then convert.
        out = np.clip(cleaned * 32768.0, -32768.0, 32767.0).astype(np.int16)
        self.frames_processed += 1
        return out.tobytes()

    def reset(self) -> None:
        """Clear filter coefficients and far-end history.

        Useful between two unrelated turns when the echo path may have
        changed (e.g. caller switched from speakerphone to handset).
        """
        self._w.fill(0)
        self._far_buf.fill(0)
        self._far_write_idx = 0
        self._far_filled = 0
        self.frames_processed = 0
        self.double_talk_frames = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _far_window(self, length: int) -> np.ndarray:
        """Return the most recent ``length`` far-end samples in
        chronological order (oldest first, newest last).
        """
        buf_len = self._far_buf.shape[0]
        if length > self._far_filled:
            length = self._far_filled
        # The newest sample lives at index (write_idx - 1) mod buf_len.
        end = self._far_write_idx
        if end >= length:
            return self._far_buf[end - length : end]
        head = self._far_buf[buf_len - (length - end) :]
        tail = self._far_buf[:end]
        return np.concatenate((head, tail))

    def _block_nlms(self, near: np.ndarray) -> np.ndarray:
        """Sample-by-sample NLMS over a frame of near-end samples.

        Vectorised would be ideal, but classical NLMS depends on the
        adapted weights from the previous sample, so the inner loop must
        be sequential. Each sample is O(taps) work; on a 320-sample frame
        with 2048 taps this is ~650K mul-adds (numpy keeps it under a
        millisecond on commodity CPUs).
        """
        taps = self._taps
        far_window = self._far_window(taps + near.shape[0] - 1)
        if far_window.shape[0] < taps + near.shape[0] - 1:
            # Edge case: still warming up. Pad with zeros at the head so
            # the indices line up.
            pad = np.zeros(
                taps + near.shape[0] - 1 - far_window.shape[0], dtype=np.float32
            )
            far_window = np.concatenate((pad, far_window))

        # Geigel double-talk detector — operates frame-wise. See module
        # docstring + Hänsler/Schmidt for derivation.
        far_max = float(np.max(np.abs(far_window))) if far_window.size else 0.0
        near_max = float(np.max(np.abs(near)))
        double_talk = near_max > self._rho * far_max if far_max > 1e-6 else False
        if double_talk:
            self.double_talk_frames += 1

        out = np.empty_like(near)
        w = self._w
        leakage = self._leakage
        step = self._step
        # Iterate sample-by-sample. ``x`` is the most recent ``taps`` samples
        # ending at the current sample's emission time (slid one position
        # per output sample).
        for i in range(near.shape[0]):
            x = far_window[i : i + taps]
            y_est = float(np.dot(w, x))
            e = float(near[i] - y_est)
            out[i] = e
            if not double_talk:
                # NLMS update with leakage. The +1e-6 prevents a divide-by-
                # zero when the far-end is silent.
                norm = float(np.dot(x, x)) + 1e-6
                w *= leakage
                w += (step * e / norm) * x
        return out

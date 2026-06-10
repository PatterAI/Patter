"""Krisp VIVA noise-reduction :class:`AudioFilter` for Patter.

Implements :class:`getpatter.providers.base.AudioFilter` directly, exposing
an ``async process(pcm_chunk, sample_rate) -> bytes`` API that integrates
with ``PipelineStreamHandler``.

Requires the proprietary Krisp Audio SDK; see
:mod:`getpatter.providers.krisp_instance` for setup instructions.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Union

from getpatter.providers.base import AudioFilter
from getpatter.providers.krisp_instance import (
    KRISP_AUDIO_AVAILABLE,
    KRISP_FRAME_DURATIONS,
    KRISP_NOT_INSTALLED_MESSAGE,
    KrispFrameDuration,
    KrispSampleRate,
    KrispSDKManager,
    int_to_krisp_frame_duration,
    int_to_krisp_sample_rate,
    krisp_audio,
)

logger = logging.getLogger(__name__)


class KrispVivaFilter(AudioFilter):
    """:class:`AudioFilter` backed by the Krisp VIVA noise-reduction SDK.

    Parameters
    ----------
    model_path:
        Path to the Krisp ``.kef`` model file.  If ``None`` the value of
        ``KRISP_VIVA_FILTER_MODEL_PATH`` is used.
    noise_suppression_level:
        Suppression strength in ``[0, 100]``.  Defaults to ``100``.
    frame_duration_ms:
        Frame duration in milliseconds — one of ``10``, ``15``, ``20``, ``30``
        or ``32``.  Defaults to ``10`` ms.
    sample_rate:
        Initial sample rate in Hz.  Defaults to ``16000``.  The internal Krisp
        session is lazily recreated if the runtime sample rate differs.

    Raises
    ------
    RuntimeError
        If ``krisp-audio`` is not installed.
    ValueError
        If ``model_path`` is missing or ``frame_duration_ms`` is unsupported.
    FileNotFoundError
        If ``model_path`` does not point to an existing file.
    """

    def __init__(
        self,
        model_path: str | None = None,
        noise_suppression_level: int = 100,
        frame_duration_ms: Union[KrispFrameDuration, int] = KrispFrameDuration.MS_10,
        sample_rate: Union[KrispSampleRate, int, None] = None,
    ) -> None:
        if not KRISP_AUDIO_AVAILABLE or krisp_audio is None:
            raise RuntimeError(KRISP_NOT_INSTALLED_MESSAGE)

        self._sdk_acquired: bool = False
        self._filtering_enabled: bool = True
        self._session: Any | None = None
        self._noise_suppression_level: int = noise_suppression_level
        self._sample_rate: int | None = None
        self._frame_duration_ms: int = frame_duration_ms
        # Internal re-framing buffer: Krisp sessions process EXACTLY
        # ``frame_duration_ms`` of audio per call, but the pipeline pushes
        # whatever the carrier delivers (typically 20 ms frames). Input is
        # accumulated here and processed in ``frame_duration_ms`` sub-frames;
        # any remainder is carried into the next call instead of raising a
        # frame-size mismatch. Cleared on sample-rate change and ``close()``.
        self._pending: bytes = b""

        try:
            KrispSDKManager.acquire()
            self._sdk_acquired = True
        except Exception as e:
            logger.error("Failed to acquire Krisp SDK: %s", e)
            raise RuntimeError(f"Failed to acquire Krisp SDK: {e}") from e

        try:
            self._model_path = model_path or os.getenv("KRISP_VIVA_FILTER_MODEL_PATH")
            if not self._model_path:
                logger.error(
                    "Model path is not provided and "
                    "KRISP_VIVA_FILTER_MODEL_PATH is not set."
                )
                raise ValueError("Model path for KrispVivaFilter must be provided.")

            if not self._model_path.endswith(".kef"):
                raise ValueError("Model is expected with .kef extension")

            if not os.path.isfile(self._model_path):
                raise FileNotFoundError(f"Model file not found: {self._model_path}")

            if frame_duration_ms not in KRISP_FRAME_DURATIONS:
                raise ValueError(
                    f"Unsupported frame duration: {frame_duration_ms}ms. "
                    f"Supported durations: "
                    f"{sorted(KRISP_FRAME_DURATIONS.keys())}"
                )

            init_sample_rate = (
                sample_rate if sample_rate is not None else KrispSampleRate.HZ_16000
            )
            self._create_session(init_sample_rate)
            logger.info(
                "Krisp filter initialised (model=%s, sr=%s Hz)",
                self._model_path,
                init_sample_rate,
            )
        except Exception:
            if self._sdk_acquired:
                KrispSDKManager.release()
                self._sdk_acquired = False
            raise

    def _create_session(self, sample_rate: int) -> None:
        """Create or recreate a Krisp session for the given sample rate."""
        if self._session is not None and self._sample_rate == sample_rate:
            return

        logger.info("Creating Krisp session for sample rate: %sHz", sample_rate)

        assert krisp_audio is not None  # narrowed by constructor check

        model_info = krisp_audio.ModelInfo()
        model_info.path = self._model_path

        nc_cfg = krisp_audio.NcSessionConfig()
        nc_cfg.inputSampleRate = int_to_krisp_sample_rate(sample_rate)
        nc_cfg.inputFrameDuration = int_to_krisp_frame_duration(self._frame_duration_ms)
        nc_cfg.outputSampleRate = nc_cfg.inputSampleRate
        nc_cfg.modelInfo = model_info

        try:
            self._session = krisp_audio.NcInt16.create(nc_cfg)
            self._sample_rate = sample_rate
            logger.info("Krisp session created successfully")
        except Exception as e:
            logger.error("Failed to create Krisp session: %s", e)
            raise

    def _process_sync(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        """Run the blocking Krisp C-extension work synchronously.

        Intended to be called via ``asyncio.to_thread`` from :meth:`process`
        so the event loop is never blocked by the native DSP pipeline.

        Input of any size is accepted: it is re-framed internally into the
        ``frame_duration_ms`` sub-frames the Krisp session requires. When the
        accumulated audio is not an exact multiple of the Krisp frame size,
        the remainder is buffered for the next call and the returned bytes
        are correspondingly shorter (possibly ``b""`` for a sub-frame input)
        — with the pipeline's standard 20 ms telephony frames and the default
        10 ms Krisp frame this never happens and output length equals input
        length.
        """
        try:
            import numpy as np  # local import keeps numpy optional
        except ImportError as e:  # pragma: no cover - numpy ships with krisp
            raise RuntimeError(
                "numpy is required for KrispVivaFilter: pip install numpy"
            ) from e

        # Lazy session re-creation if sample rate changed mid-call. Buffered
        # audio from the previous rate is meaningless at the new one — drop it.
        if self._session is None or self._sample_rate != sample_rate:
            self._create_session(sample_rate)
            self._pending = b""

        expected_samples = int((sample_rate * self._frame_duration_ms) / 1000)
        frame_bytes = expected_samples * 2  # int16 mono

        data = self._pending + pcm_chunk if self._pending else pcm_chunk
        if len(data) < frame_bytes:
            self._pending = data
            return b""
        emit_len = (len(data) // frame_bytes) * frame_bytes
        self._pending = data[emit_len:]

        out = bytearray()
        for offset in range(0, emit_len, frame_bytes):
            frame = data[offset : offset + frame_bytes]
            out += self._filter_frame(
                np.frombuffer(frame, dtype=np.int16), frame
            )
        return bytes(out)

    def _filter_frame(self, audio_samples: Any, frame: bytes) -> bytes:
        """Run ONE exact-size frame through the Krisp session.

        Falls back to the original ``frame`` bytes on empty / mismatched /
        failing SDK output so the call audio path is never broken.
        """
        try:
            filtered_samples = self._session.process(
                audio_samples, self._noise_suppression_level
            )

            if filtered_samples is None or len(filtered_samples) == 0:
                logger.warning("Krisp returned empty output, using original audio")
                return frame
            if len(filtered_samples) != len(audio_samples):
                logger.warning(
                    "Krisp output size mismatch: expected %s, got %s, using original",
                    len(audio_samples),
                    len(filtered_samples),
                )
                return frame

            return bytes(filtered_samples.tobytes())
        except Exception as e:
            logger.error("Error processing Krisp frame: %s", e)
            return frame

    async def process(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        """Run the PCM chunk through Krisp and return the filtered bytes.

        When filtering is disabled the input is returned unchanged.  Errors in
        the underlying SDK are logged and the original PCM is returned to
        avoid breaking the call audio path.

        The blocking C-extension DSP work is offloaded to a thread via
        ``asyncio.to_thread`` to avoid stalling the event loop.
        """
        if not self._filtering_enabled:
            return pcm_chunk

        return await asyncio.to_thread(self._process_sync, pcm_chunk, sample_rate)

    def enable(self) -> None:
        """Enable noise filtering."""
        self._filtering_enabled = True

    def disable(self) -> None:
        """Disable noise filtering (audio passes through unmodified)."""
        self._filtering_enabled = False

    @property
    def enabled(self) -> bool:
        """Return True when filtering is active."""
        return self._filtering_enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._filtering_enabled = value

    async def close(self) -> None:
        """Release the Krisp session and decrement the SDK refcount."""
        self._pending = b""
        if self._session is not None:
            self._session = None
        if self._sdk_acquired:
            try:
                KrispSDKManager.release()
            except Exception as e:
                logger.error("Error releasing Krisp SDK: %s", e)
            finally:
                self._sdk_acquired = False
        logger.debug("Krisp filter closed")

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown path
        # Avoid calling C extensions during Python shutdown to prevent GIL
        # errors.  Callers should invoke ``await close()`` explicitly.
        if KrispSDKManager is None:
            return
        if getattr(self, "_sdk_acquired", False):
            try:
                if getattr(self, "_session", None) is not None:
                    self._session = None
                KrispSDKManager.release()
                self._sdk_acquired = False
            except Exception:
                pass

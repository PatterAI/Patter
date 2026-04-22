# Copyright 2023 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Krisp VIVA noise-reduction :class:`AudioFilter` for Patter.

Adapted from LiveKit Agents (Apache 2.0):
https://github.com/livekit/agents/blob/main/livekit-plugins/livekit-plugins-krisp/livekit/plugins/krisp/viva_filter.py

The original LiveKit implementation subclasses ``rtc.FrameProcessor`` and
operates on :class:`livekit.rtc.AudioFrame`.  Patter does not depend on
livekit-rtc, so this port implements :class:`getpatter.providers.base.AudioFilter`
directly, exposing an ``async process(pcm_chunk, sample_rate) -> bytes`` API
that integrates with ``PipelineStreamHandler``.

Requires the proprietary Krisp Audio SDK; see
:mod:`getpatter.providers.krisp_instance` for setup instructions.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from getpatter.providers.base import AudioFilter
from getpatter.providers.krisp_instance import (
    KRISP_AUDIO_AVAILABLE,
    KRISP_FRAME_DURATIONS,
    KRISP_NOT_INSTALLED_MESSAGE,
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
        frame_duration_ms: int = 10,
        sample_rate: int | None = None,
    ) -> None:
        if not KRISP_AUDIO_AVAILABLE or krisp_audio is None:
            raise RuntimeError(KRISP_NOT_INSTALLED_MESSAGE)

        self._sdk_acquired: bool = False
        self._filtering_enabled: bool = True
        self._session: Any | None = None
        self._noise_suppression_level: int = noise_suppression_level
        self._sample_rate: int | None = None
        self._frame_duration_ms: int = frame_duration_ms

        try:
            KrispSDKManager.acquire()
            self._sdk_acquired = True
        except Exception as e:
            logger.error("Failed to acquire Krisp SDK: %s", e)
            raise RuntimeError(f"Failed to acquire Krisp SDK: {e}") from e

        try:
            self._model_path = model_path or os.getenv(
                "KRISP_VIVA_FILTER_MODEL_PATH"
            )
            if not self._model_path:
                logger.error(
                    "Model path is not provided and "
                    "KRISP_VIVA_FILTER_MODEL_PATH is not set."
                )
                raise ValueError(
                    "Model path for KrispVivaFilter must be provided."
                )

            if not self._model_path.endswith(".kef"):
                raise ValueError("Model is expected with .kef extension")

            if not os.path.isfile(self._model_path):
                raise FileNotFoundError(
                    f"Model file not found: {self._model_path}"
                )

            if frame_duration_ms not in KRISP_FRAME_DURATIONS:
                raise ValueError(
                    f"Unsupported frame duration: {frame_duration_ms}ms. "
                    f"Supported durations: "
                    f"{sorted(KRISP_FRAME_DURATIONS.keys())}"
                )

            init_sample_rate = sample_rate if sample_rate is not None else 16000
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
        nc_cfg.inputFrameDuration = int_to_krisp_frame_duration(
            self._frame_duration_ms
        )
        nc_cfg.outputSampleRate = nc_cfg.inputSampleRate
        nc_cfg.modelInfo = model_info

        try:
            self._session = krisp_audio.NcInt16.create(nc_cfg)
            self._sample_rate = sample_rate
            logger.info("Krisp session created successfully")
        except Exception as e:
            logger.error("Failed to create Krisp session: %s", e)
            raise

    async def process(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        """Run the PCM chunk through Krisp and return the filtered bytes.

        When filtering is disabled the input is returned unchanged.  Errors in
        the underlying SDK are logged and the original PCM is returned to
        avoid breaking the call audio path.
        """
        if not self._filtering_enabled:
            return pcm_chunk

        try:
            import numpy as np  # local import keeps numpy optional
        except ImportError as e:  # pragma: no cover - numpy ships with krisp
            raise RuntimeError(
                "numpy is required for KrispVivaFilter: pip install numpy"
            ) from e

        # Lazy session re-creation if sample rate changed mid-call.
        if self._session is None or self._sample_rate != sample_rate:
            self._create_session(sample_rate)

        expected_samples = int(
            (sample_rate * self._frame_duration_ms) / 1000
        )
        audio_samples = np.frombuffer(pcm_chunk, dtype=np.int16)
        if len(audio_samples) != expected_samples:
            raise ValueError(
                f"Frame size mismatch: expected {expected_samples} samples "
                f"({self._frame_duration_ms}ms @ {sample_rate}Hz), "
                f"got {len(audio_samples)} samples"
            )

        try:
            filtered_samples = self._session.process(
                audio_samples, self._noise_suppression_level
            )

            if filtered_samples is None or len(filtered_samples) == 0:
                logger.warning("Krisp returned empty output, using original audio")
                return pcm_chunk
            if len(filtered_samples) != len(audio_samples):
                logger.warning(
                    "Krisp output size mismatch: expected %s, got %s, using original",
                    len(audio_samples),
                    len(filtered_samples),
                )
                return pcm_chunk

            return bytes(filtered_samples.tobytes())
        except Exception as e:
            logger.error("Error processing Krisp frame: %s", e)
            return pcm_chunk

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

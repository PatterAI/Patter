"""
Silero VAD provider.

Acoustic voice activity detection backed by the Silero ONNX model. Wraps
:class:`~patter.providers.silero_onnx.OnnxModel` with a streaming state
machine that buffers incoming PCM frames, runs inference on fixed-size
windows (256 samples at 8 kHz, 512 at 16 kHz), applies an exponential
probability filter, and emits :class:`~patter.providers.base.VADEvent`
transitions (``speech_start`` / ``speech_end`` / ``silence``).

Ported from LiveKit Agents (Apache 2.0):
https://github.com/livekit/agents
Source: livekit-plugins/livekit-plugins-silero/livekit/plugins/silero/vad.py

Adaptations for Patter:
  - Input is raw PCM ``bytes`` (int16, little-endian, mono) via
    ``process_frame(pcm_chunk, sample_rate)``, not ``livekit.rtc.AudioFrame``.
  - No IPC / inference runner: onnxruntime runs inline in a
    ``loop.run_in_executor`` worker thread.
  - Emits ``VADEvent`` (Patter protocol) instead of LiveKit event types.
"""

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

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np

from patter.providers.base import VADEvent, VADProvider
from patter.providers.silero_onnx import (
    SUPPORTED_SAMPLE_RATES,
    OnnxModel,
    new_inference_session,
)

if TYPE_CHECKING:
    import onnxruntime  # type: ignore

logger = logging.getLogger(__name__)

SLOW_INFERENCE_THRESHOLD = 0.2  # late by 200ms


@dataclass
class _VADOptions:
    min_speech_duration: float
    min_silence_duration: float
    prefix_padding_duration: float
    activation_threshold: float
    deactivation_threshold: float
    sample_rate: int


class _ExpFilter:
    """Exponential smoothing filter.

    Ported from ``livekit.agents.utils.ExpFilter`` (Apache 2.0). Returns a
    smoothed value using ``a = alpha**exp`` as the smoothing coefficient so
    that callers can adapt per-sample weight based on how stale the filter
    state is relative to the incoming sample.
    """

    def __init__(self, alpha: float) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1].")
        self._alpha = alpha
        self._filtered: float | None = None

    def apply(self, exp: float, sample: float) -> float:
        if self._filtered is None:
            self._filtered = sample
        else:
            a = self._alpha ** exp
            self._filtered = a * self._filtered + (1 - a) * sample
        return self._filtered

    def reset(self) -> None:
        self._filtered = None


class SileroVAD(VADProvider):
    """Silero-based :class:`~patter.providers.base.VADProvider`.

    Construct via :meth:`load` to initialise the ONNX session and options::

        vad = SileroVAD.load(activation_threshold=0.5, sample_rate=16000)
        event = await vad.process_frame(pcm_bytes, sample_rate=16000)
        if event and event.type == "speech_start":
            ...
        await vad.close()

    ``process_frame`` accepts raw mono int16 little-endian PCM and returns an
    event only when the internal state transitions; it returns ``None`` while
    buffering or emitting continuous silence/speech frames that do not change
    state. Inference runs in an executor to avoid blocking the event loop.
    """

    @classmethod
    def load(
        cls,
        *,
        min_speech_duration: float = 0.05,
        min_silence_duration: float = 0.55,
        prefix_padding_duration: float = 0.5,
        activation_threshold: float = 0.5,
        sample_rate: Literal[8000, 16000] = 16000,
        force_cpu: bool = True,
        onnx_file_path: Path | str | None = None,
        deactivation_threshold: float | None = None,
    ) -> "SileroVAD":
        """Load the Silero VAD model and return a ready-to-use provider.

        Args:
            min_speech_duration: Minimum continuous speech duration (seconds)
                before a ``speech_start`` event is emitted.
            min_silence_duration: Minimum continuous silence duration (seconds)
                before a ``speech_end`` event is emitted after speech.
            prefix_padding_duration: Padding added to the start of detected
                speech. Preserved for parity with LiveKit; not currently used
                for audio re-emission in the Patter port since callers already
                own the upstream PCM buffer.
            activation_threshold: Probability threshold above which a frame is
                considered speech.
            sample_rate: Inference sample rate (8000 or 16000 Hz).
            force_cpu: Restrict ONNX Runtime to the CPU execution provider.
            onnx_file_path: Optional override for the bundled model file.
            deactivation_threshold: Exit threshold while speaking. Defaults to
                ``max(activation_threshold - 0.15, 0.01)``.
        """
        if sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError("Silero VAD only supports 8KHz and 16KHz sample rates")

        if deactivation_threshold is not None and deactivation_threshold <= 0:
            raise ValueError("deactivation_threshold must be greater than 0")

        session = new_inference_session(force_cpu, onnx_file_path=onnx_file_path)
        opts = _VADOptions(
            min_speech_duration=min_speech_duration,
            min_silence_duration=min_silence_duration,
            prefix_padding_duration=prefix_padding_duration,
            activation_threshold=activation_threshold,
            deactivation_threshold=(
                deactivation_threshold
                if deactivation_threshold is not None
                else max(activation_threshold - 0.15, 0.01)
            ),
            sample_rate=sample_rate,
        )
        return cls(session=session, opts=opts)

    def __init__(
        self,
        *,
        session: "onnxruntime.InferenceSession",
        opts: _VADOptions,
    ) -> None:
        self._opts = opts
        self._onnx_session = session
        self._model = OnnxModel(onnx_session=session, sample_rate=opts.sample_rate)
        self._exp_filter = _ExpFilter(alpha=0.35)

        # Streaming state
        self._pending = np.zeros(0, dtype=np.float32)
        self._pub_speaking = False
        self._speech_threshold_duration = 0.0
        self._silence_threshold_duration = 0.0
        self._pub_current_sample = 0
        self._pub_timestamp = 0.0
        self._closed = False

    @property
    def model(self) -> str:
        return "silero"

    @property
    def provider(self) -> str:
        return "ONNX"

    @property
    def sample_rate(self) -> int:
        return self._opts.sample_rate

    async def process_frame(
        self, pcm_chunk: bytes, sample_rate: int
    ) -> VADEvent | None:
        """Process a raw mono int16 little-endian PCM chunk.

        Returns a :class:`~patter.providers.base.VADEvent` only on state
        transitions (``speech_start`` / ``speech_end``). Between transitions
        this method returns ``None`` while buffering audio and running
        inference; callers that need per-frame probabilities should subclass
        and observe the internal state.
        """
        if self._closed:
            raise RuntimeError("SileroVAD is closed")

        if sample_rate != self._opts.sample_rate:
            raise ValueError(
                f"input sample_rate {sample_rate} does not match model "
                f"sample_rate {self._opts.sample_rate}; resampling is not "
                "implemented in the Patter port"
            )

        if not pcm_chunk:
            return None

        # int16 LE PCM -> float32 in [-1.0, 1.0]
        samples_i16 = np.frombuffer(pcm_chunk, dtype=np.int16)
        if samples_i16.size == 0:
            return None
        samples_f32 = samples_i16.astype(np.float32) / float(np.iinfo(np.int16).max)

        # Append to pending buffer
        self._pending = np.concatenate([self._pending, samples_f32])

        window_size = self._model.window_size_samples
        loop = asyncio.get_event_loop()
        event: VADEvent | None = None

        while self._pending.shape[0] >= window_size:
            window = self._pending[:window_size].copy()
            self._pending = self._pending[window_size:]

            start_time = time.perf_counter()
            raw_p = await loop.run_in_executor(
                None, _run_inference, self._model, window
            )
            p = self._exp_filter.apply(exp=1.0, sample=raw_p)

            inference_duration = time.perf_counter() - start_time
            window_duration = window_size / self._opts.sample_rate
            if inference_duration > SLOW_INFERENCE_THRESHOLD:
                logger.warning(
                    "Silero VAD inference slower than realtime",
                    extra={"inference_duration": inference_duration},
                )

            self._pub_current_sample += window_size
            self._pub_timestamp += window_duration

            transition = self._advance_state(p, window_duration)
            if transition is not None and event is None:
                # Keep the first transition in this batch; subsequent windows
                # in the same call continue to update internal state but we
                # only surface one event per process_frame call so callers do
                # not lose transitions to a later overwrite.
                event = transition

        return event

    def _advance_state(
        self, p: float, window_duration: float
    ) -> VADEvent | None:
        """Update internal speaking state based on filtered probability.

        Returns a VADEvent on transitions, None otherwise.
        """
        opts = self._opts
        if p >= opts.activation_threshold or (
            self._pub_speaking and p > opts.deactivation_threshold
        ):
            self._speech_threshold_duration += window_duration
            self._silence_threshold_duration = 0.0

            if not self._pub_speaking:
                if self._speech_threshold_duration >= opts.min_speech_duration:
                    self._pub_speaking = True
                    return VADEvent(
                        type="speech_start",
                        confidence=float(p),
                        duration_ms=self._speech_threshold_duration * 1000.0,
                    )
        else:
            self._silence_threshold_duration += window_duration
            self._speech_threshold_duration = 0.0

            if (
                self._pub_speaking
                and self._silence_threshold_duration >= opts.min_silence_duration
            ):
                self._pub_speaking = False
                return VADEvent(
                    type="speech_end",
                    confidence=float(p),
                    duration_ms=self._silence_threshold_duration * 1000.0,
                )

        return None

    async def close(self) -> None:
        """Release the ONNX session. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # onnxruntime sessions don't expose an explicit close; drop refs
        # so the session can be garbage collected.
        self._onnx_session = None  # type: ignore[assignment]
        self._model = None  # type: ignore[assignment]


def _run_inference(model: OnnxModel, window: np.ndarray) -> float:
    """Blocking inference entrypoint executed in an executor thread."""
    return model(window)

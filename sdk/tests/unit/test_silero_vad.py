"""Unit tests for patter.providers.silero_vad — SileroVAD streaming state machine.

All tests mock ``onnxruntime.InferenceSession`` so they run without the
bundled ONNX model and without pulling onnxruntime as a hard dependency.
Integration tests that exercise the real model are marked with the
``integration`` marker and skipped by default (see ``test_real_model`` below).
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pytest

from patter.providers.base import VADEvent
from patter.providers.silero_onnx import OnnxModel
from patter.providers.silero_vad import SileroVAD, _VADOptions


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeOnnxSession:
    """Minimal stand-in for ``onnxruntime.InferenceSession`` used in unit tests.

    Returns a programmable sequence of probabilities so tests can script the
    VAD state machine deterministically. The second return value is the new
    RNN state; we keep it shaped like Silero's (2, 1, 128) float32 tensor.
    """

    def __init__(self, probs: Iterable[float]) -> None:
        self._probs = list(probs)
        self._i = 0
        self.calls: list[dict[str, np.ndarray]] = []

    def run(self, _output_names, ort_inputs: dict[str, np.ndarray]):
        self.calls.append({k: v.copy() for k, v in ort_inputs.items()})
        if self._i >= len(self._probs):
            p = self._probs[-1] if self._probs else 0.0
        else:
            p = self._probs[self._i]
            self._i += 1
        out = np.array([[p]], dtype=np.float32)
        state = np.zeros((2, 1, 128), dtype=np.float32)
        return out, state


def _build_vad(
    *,
    probs: Iterable[float] = (),
    sample_rate: int = 16000,
    min_speech_duration: float = 0.032,
    min_silence_duration: float = 0.064,
    activation_threshold: float = 0.5,
) -> tuple[SileroVAD, _FakeOnnxSession]:
    """Construct a SileroVAD wired to a scripted fake ONNX session."""
    session = _FakeOnnxSession(probs)
    opts = _VADOptions(
        min_speech_duration=min_speech_duration,
        min_silence_duration=min_silence_duration,
        prefix_padding_duration=0.0,
        activation_threshold=activation_threshold,
        deactivation_threshold=max(activation_threshold - 0.15, 0.01),
        sample_rate=sample_rate,
    )
    vad = SileroVAD(session=session, opts=opts)  # type: ignore[arg-type]
    # Replace the internal OnnxModel with one bound to our fake session.
    vad._model = OnnxModel(onnx_session=session, sample_rate=sample_rate)  # type: ignore[arg-type]
    return vad, session


def _silence_pcm(num_samples: int) -> bytes:
    """Generate `num_samples` of pure silence as int16 LE PCM bytes."""
    return np.zeros(num_samples, dtype=np.int16).tobytes()


def _sine_pcm(num_samples: int, sample_rate: int, freq_hz: float = 440.0) -> bytes:
    """Generate a 440 Hz sine-wave int16 LE PCM buffer for `num_samples` samples."""
    t = np.arange(num_samples, dtype=np.float32) / sample_rate
    wave = np.sin(2 * math.pi * freq_hz * t) * 0.5  # half amplitude
    samples = (wave * np.iinfo(np.int16).max).astype(np.int16)
    return samples.tobytes()


# ---------------------------------------------------------------------------
# init / basic shape checks
# ---------------------------------------------------------------------------


def test_init_defaults_16k() -> None:
    vad, _ = _build_vad(sample_rate=16000)
    assert vad.sample_rate == 16000
    assert vad.model == "silero"
    assert vad.provider == "ONNX"
    assert vad._model.window_size_samples == 512
    assert vad._model.context_size == 64


def test_init_defaults_8k() -> None:
    vad, _ = _build_vad(sample_rate=8000)
    assert vad.sample_rate == 8000
    assert vad._model.window_size_samples == 256
    assert vad._model.context_size == 32


def test_init_invalid_sample_rate_raises() -> None:
    session = _FakeOnnxSession([0.0])
    with pytest.raises(ValueError, match="8KHz and 16KHz"):
        OnnxModel(onnx_session=session, sample_rate=44100)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# process_frame — silence path (mocked onnxruntime)
# ---------------------------------------------------------------------------


async def test_process_frame_silence_buffers_and_returns_none() -> None:
    """Silence PCM shorter than a window yields no event and no inference."""
    vad, session = _build_vad(probs=[0.0, 0.0, 0.0])
    pcm = _silence_pcm(256)  # < 512 window @ 16 kHz -> no inference yet
    event = await vad.process_frame(pcm, sample_rate=16000)
    assert event is None
    assert session.calls == []


async def test_process_frame_silence_full_window_returns_none() -> None:
    """A full window of silence runs inference but should not transition state."""
    vad, session = _build_vad(probs=[0.02, 0.02, 0.02])
    pcm = _silence_pcm(512)  # exactly one window @ 16 kHz
    event = await vad.process_frame(pcm, sample_rate=16000)
    assert event is None
    assert len(session.calls) == 1
    # Input tensor should be float32, shape (1, context_size + window_size_samples)
    buf = session.calls[0]["input"]
    assert buf.dtype == np.float32
    assert buf.shape == (1, 64 + 512)


# ---------------------------------------------------------------------------
# process_frame — speech activation (mocked onnxruntime)
# ---------------------------------------------------------------------------


async def test_process_frame_emits_speech_start_above_threshold() -> None:
    """High probability for >= min_speech_duration yields a speech_start event.

    This test uses a MOCKED onnxruntime session that returns fixed
    probabilities (>= activation_threshold) so the state machine crosses the
    min_speech_duration gate after a window.
    """
    vad, _ = _build_vad(
        probs=[0.95] * 4,
        min_speech_duration=0.032,  # one window @ 16 kHz = 32 ms
        activation_threshold=0.5,
    )
    pcm = _sine_pcm(512, sample_rate=16000)  # exactly one window
    event = await vad.process_frame(pcm, sample_rate=16000)
    assert isinstance(event, VADEvent)
    assert event.type == "speech_start"
    assert event.confidence > 0.5
    assert event.duration_ms >= 32.0


async def test_process_frame_emits_speech_end_after_silence_gate() -> None:
    """After speech_start, prolonged low prob yields speech_end."""
    vad, _ = _build_vad(
        probs=[0.9, 0.9, 0.02, 0.02, 0.02],
        min_speech_duration=0.032,
        min_silence_duration=0.064,  # two windows @ 16 kHz = 64 ms
        activation_threshold=0.5,
    )
    pcm_speech = _sine_pcm(512 * 2, sample_rate=16000)
    pcm_silence = _silence_pcm(512 * 3)
    start_event = await vad.process_frame(pcm_speech, sample_rate=16000)
    end_event = await vad.process_frame(pcm_silence, sample_rate=16000)
    assert start_event is not None and start_event.type == "speech_start"
    assert end_event is not None and end_event.type == "speech_end"


async def test_process_frame_empty_chunk_returns_none() -> None:
    vad, session = _build_vad(probs=[0.0])
    assert await vad.process_frame(b"", sample_rate=16000) is None
    assert session.calls == []


async def test_process_frame_rejects_mismatched_sample_rate() -> None:
    vad, _ = _build_vad(sample_rate=16000)
    with pytest.raises(ValueError, match="sample_rate"):
        await vad.process_frame(_silence_pcm(512), sample_rate=8000)


# ---------------------------------------------------------------------------
# close() is idempotent
# ---------------------------------------------------------------------------


async def test_close_is_idempotent() -> None:
    vad, _ = _build_vad(probs=[0.0])
    await vad.close()
    await vad.close()  # must not raise


async def test_process_frame_after_close_raises() -> None:
    vad, _ = _build_vad(probs=[0.0])
    await vad.close()
    with pytest.raises(RuntimeError, match="closed"):
        await vad.process_frame(_silence_pcm(512), sample_rate=16000)


# ---------------------------------------------------------------------------
# Integration (skipped by default — requires bundled model and onnxruntime)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_model_load() -> None:
    """Smoke test against the real ONNX model. Requires onnxruntime installed
    and the bundled ``patter/resources/silero_vad.onnx`` file present.
    """
    pytest.importorskip("onnxruntime")
    vad = SileroVAD.load(sample_rate=16000)
    assert vad.sample_rate == 16000
    assert vad.model == "silero"

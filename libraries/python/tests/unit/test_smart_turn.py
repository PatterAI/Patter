"""Unit tests for getpatter.providers.smart_turn — SmartTurnDetector.

All tests stub ``onnxruntime.InferenceSession`` (mirroring
``test_silero_vad.py``) so they run without the smart-turn model file, the
onnxruntime native dependency, or any network access. The Whisper log-mel
preprocessing is exercised directly against its documented invariants —
the implementation was verified offline against
``transformers.WhisperFeatureExtractor(chunk_length=8)`` to ~1e-6 (float32
rounding).
"""

from __future__ import annotations

import logging
import math

import pytest

# Smart-turn is an optional extra (`pip install getpatter[turn-detector]`);
# skip the whole module gracefully when numpy is not installed on CI
# runners that only install base deps.
np = pytest.importorskip("numpy")

from getpatter.providers import smart_turn as smart_turn_module  # noqa: E402
from getpatter.providers.base import TurnDetectorProvider  # noqa: E402
from getpatter.providers.smart_turn import (  # noqa: E402
    SMART_TURN_MAX_SAMPLES,
    SMART_TURN_MODEL_ENV_VAR,
    SmartTurnDetector,
    _SmartTurnOptions,
    compute_whisper_log_mel_features,
    prepare_input_window,
    resolve_smart_turn_model_path,
)

# The ONNX session/runtime — the only external boundary — is stubbed.
pytestmark = pytest.mark.mocked


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeOnnxSession:
    """Minimal stand-in for ``onnxruntime.InferenceSession``.

    Returns a programmable sequence of end-of-turn probabilities (the
    last value repeats once exhausted) and records every feed so tests can
    assert the exact ``input_features`` tensor the model receives.
    """

    def __init__(self, probs) -> None:
        self._probs = list(probs)
        self._i = 0
        self.calls: list[dict] = []

    def run(self, _output_names, feeds: dict):
        self.calls.append({k: np.array(v, copy=True) for k, v in feeds.items()})
        if self._i >= len(self._probs):
            p = self._probs[-1] if self._probs else 0.0
        else:
            p = self._probs[self._i]
            self._i += 1
        return [np.array([[p]], dtype=np.float32)]


def _build_detector(
    *, probs=(0.9,), threshold: float = 0.5
) -> tuple[SmartTurnDetector, _FakeOnnxSession]:
    session = _FakeOnnxSession(probs)
    detector = SmartTurnDetector(
        session=session,  # type: ignore[arg-type]
        opts=_SmartTurnOptions(threshold=threshold),
    )
    return detector, session


def _sine_pcm(num_samples: int, sample_rate: int = 16000, freq_hz: float = 440.0) -> bytes:
    t = np.arange(num_samples, dtype=np.float32) / sample_rate
    wave = np.sin(2 * math.pi * freq_hz * t) * 0.5
    return (wave * np.iinfo(np.int16).max).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# Model-path resolution (no onnxruntime needed — resolution happens first)
# ---------------------------------------------------------------------------


def test_load_without_model_path_or_env_raises_with_download_hint(monkeypatch) -> None:
    monkeypatch.delenv(SMART_TURN_MODEL_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="huggingface.co/pipecat-ai/smart-turn-v3"):
        SmartTurnDetector.load()


def test_load_with_missing_file_raises_file_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(SMART_TURN_MODEL_ENV_VAR, raising=False)
    missing = tmp_path / "smart-turn-v3.0.onnx"
    with pytest.raises(FileNotFoundError, match="huggingface.co"):
        SmartTurnDetector.load(model_path=missing)


def test_env_var_path_is_resolved(tmp_path, monkeypatch) -> None:
    model_file = tmp_path / "smart-turn-v3.0.onnx"
    model_file.write_bytes(b"\x00")  # existence is all resolution checks
    monkeypatch.setenv(SMART_TURN_MODEL_ENV_VAR, str(model_file))
    assert resolve_smart_turn_model_path(None) == model_file


def test_explicit_path_wins_over_env(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "env.onnx"
    env_file.write_bytes(b"\x00")
    arg_file = tmp_path / "arg.onnx"
    arg_file.write_bytes(b"\x00")
    monkeypatch.setenv(SMART_TURN_MODEL_ENV_VAR, str(env_file))
    assert resolve_smart_turn_model_path(arg_file) == arg_file


def test_env_var_pointing_at_directory_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SMART_TURN_MODEL_ENV_VAR, str(tmp_path))
    with pytest.raises(FileNotFoundError, match="not a file"):
        resolve_smart_turn_model_path(None)


def test_load_rejects_out_of_range_threshold(tmp_path) -> None:
    with pytest.raises(ValueError, match="threshold"):
        SmartTurnDetector.load(threshold=1.5, model_path=tmp_path / "x.onnx")


def test_load_without_numpy_raises_with_install_hint(tmp_path, monkeypatch) -> None:
    """Missing ``turn-detector`` extra → a descriptive ImportError, not an
    AttributeError from dereferencing the absent module."""
    model_file = tmp_path / "smart-turn-v3.0.onnx"
    model_file.write_bytes(b"\x00")
    monkeypatch.setattr(smart_turn_module, "np", None)  # simulate missing extra
    with pytest.raises(ImportError, match="turn-detector"):
        SmartTurnDetector.load(model_path=model_file)


# ---------------------------------------------------------------------------
# maybe_load — degrade (warn once + None) instead of raising
# ---------------------------------------------------------------------------


def test_maybe_load_unconfigured_returns_none_with_single_warning(
    monkeypatch, caplog
) -> None:
    """No model path / env var → None + ONE warning with the download hint,
    so ``Agent(turn_detector=SmartTurnDetector.maybe_load())`` falls back to
    plain VAD-silence endpointing instead of crashing the app."""
    monkeypatch.delenv(SMART_TURN_MODEL_ENV_VAR, raising=False)
    with caplog.at_level(logging.WARNING, logger="getpatter.providers.smart_turn"):
        assert SmartTurnDetector.maybe_load() is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "falling back" in warnings[0].getMessage()
    assert "huggingface.co/pipecat-ai/smart-turn-v3" in warnings[0].getMessage()


def test_maybe_load_missing_file_returns_none(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.delenv(SMART_TURN_MODEL_ENV_VAR, raising=False)
    with caplog.at_level(logging.WARNING, logger="getpatter.providers.smart_turn"):
        result = SmartTurnDetector.maybe_load(model_path=tmp_path / "missing.onnx")
    assert result is None
    assert any("falling back" in r.getMessage() for r in caplog.records)


def test_maybe_load_missing_runtime_returns_none(
    tmp_path, monkeypatch, caplog
) -> None:
    """Model file present but the optional runtime deps are missing → None
    (+ one warning naming the extra), never an exception."""
    model_file = tmp_path / "smart-turn-v3.0.onnx"
    model_file.write_bytes(b"\x00")
    monkeypatch.setattr(smart_turn_module, "np", None)  # simulate missing extra
    with caplog.at_level(logging.WARNING, logger="getpatter.providers.smart_turn"):
        assert SmartTurnDetector.maybe_load(model_path=model_file) is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "getpatter[turn-detector]" in warnings[0].getMessage()


def test_maybe_load_still_rejects_out_of_range_threshold(monkeypatch) -> None:
    """An invalid threshold is a configuration bug, not a provisioning gap —
    it must raise even on the degrade-gracefully path."""
    monkeypatch.delenv(SMART_TURN_MODEL_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="threshold"):
        SmartTurnDetector.maybe_load(threshold=-0.1)


# ---------------------------------------------------------------------------
# Whisper log-mel preprocessing invariants (smart-turn v3 input contract)
# ---------------------------------------------------------------------------


def test_features_shape_and_dtype() -> None:
    feats = compute_whisper_log_mel_features(prepare_input_window(np.zeros(16000)))
    assert feats.shape == (80, 800)
    assert feats.dtype == np.float32


def test_silence_features_are_uniform_floor() -> None:
    """All-zero audio → every cell is the clamped floor (max-8 → (x+4)/4 = -1.5)."""
    feats = compute_whisper_log_mel_features(prepare_input_window(np.zeros(8000)))
    assert np.allclose(feats, -1.5)


def test_short_audio_is_left_padded_so_speech_sits_at_the_end() -> None:
    """1 s of tone in an 8 s window → energy concentrated in the LAST frames.

    Smart-turn pads at the BEGINNING (``truncate_audio_to_last_n_seconds``)
    so the caller's most recent audio aligns with the end of the model's
    context window.
    """
    t = np.arange(16000) / 16000.0
    tone = np.sin(2 * np.pi * 440.0 * t) * 0.5
    feats = compute_whisper_log_mel_features(prepare_input_window(tone))
    tail = float(feats[:, -100:].mean())  # last second of frames
    head = float(feats[:, :100].mean())  # first second (padding)
    assert tail > head


def test_long_audio_keeps_only_the_most_recent_8_seconds() -> None:
    """A 10 s buffer whose only tone lives in the FIRST 2 s is truncated to
    the trailing 8 s of silence → features collapse to the uniform floor."""
    audio = np.zeros(160000)
    t = np.arange(32000) / 16000.0
    audio[:32000] = np.sin(2 * np.pi * 440.0 * t) * 0.5
    feats = compute_whisper_log_mel_features(prepare_input_window(audio))
    assert np.allclose(feats, -1.5)


def test_prepare_input_window_normalizes_to_zero_mean_unit_variance() -> None:
    rng = np.random.default_rng(7)
    window = prepare_input_window(rng.standard_normal(SMART_TURN_MAX_SAMPLES) * 3 + 5)
    assert abs(float(window.mean())) < 1e-9
    assert abs(float(window.var()) - 1.0) < 1e-3  # 1e-7 eps shifts it slightly


def test_compute_features_rejects_unprepared_window() -> None:
    with pytest.raises(ValueError, match="prepare_input_window"):
        compute_whisper_log_mel_features(np.zeros(1000))


# ---------------------------------------------------------------------------
# predict() — stubbed ONNX session
# ---------------------------------------------------------------------------


async def test_predict_returns_session_probability() -> None:
    detector, session = _build_detector(probs=[0.83])
    p = await detector.predict(_sine_pcm(16000))
    assert p == pytest.approx(0.83, abs=1e-6)
    assert len(session.calls) == 1


async def test_predict_feeds_input_features_1x80x800_float32() -> None:
    detector, session = _build_detector(probs=[0.4])
    await detector.predict(_sine_pcm(32000))
    feeds = session.calls[0]
    assert set(feeds.keys()) == {"input_features"}
    assert feeds["input_features"].shape == (1, 80, 800)
    assert feeds["input_features"].dtype == np.float32


async def test_predict_empty_window_short_circuits_to_zero() -> None:
    detector, session = _build_detector(probs=[0.99])
    assert await detector.predict(b"") == 0.0
    assert session.calls == []


async def test_predict_clamps_out_of_range_outputs() -> None:
    detector, _ = _build_detector(probs=[1.7])
    assert await detector.predict(_sine_pcm(1600)) == 1.0


async def test_predict_accepts_more_than_8s_of_audio() -> None:
    """Windows longer than the model context are truncated, not rejected."""
    detector, session = _build_detector(probs=[0.6])
    p = await detector.predict(_sine_pcm(16000 * 10))
    assert p == pytest.approx(0.6, abs=1e-6)
    assert session.calls[0]["input_features"].shape == (1, 80, 800)


# ---------------------------------------------------------------------------
# Surface / lifecycle
# ---------------------------------------------------------------------------


def test_detector_is_a_turn_detector_provider() -> None:
    detector, _ = _build_detector()
    assert isinstance(detector, TurnDetectorProvider)


def test_properties() -> None:
    detector, _ = _build_detector(threshold=0.65)
    assert detector.model == "smart-turn-v3"
    assert detector.provider == "ONNX"
    assert detector.sample_rate == 16000
    assert detector.max_window_seconds == 8
    assert detector.threshold == 0.65


def test_default_threshold_is_smart_turn_reference_value() -> None:
    detector, _ = _build_detector(probs=[0.0], threshold=0.5)
    assert detector.threshold == 0.5


async def test_close_is_idempotent() -> None:
    detector, _ = _build_detector()
    await detector.close()
    await detector.close()  # must not raise


async def test_predict_after_close_raises() -> None:
    detector, _ = _build_detector()
    await detector.close()
    with pytest.raises(RuntimeError, match="closed"):
        await detector.predict(_sine_pcm(1600))


# ---------------------------------------------------------------------------
# Integration (skipped by default — requires a downloaded model + onnxruntime)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_real_model_load() -> None:
    """Smoke test against a real smart-turn-v3 ONNX file. Requires
    onnxruntime installed and PATTER_SMART_TURN_MODEL pointing at the
    downloaded model.
    """
    import os

    pytest.importorskip("onnxruntime")
    if not os.environ.get(SMART_TURN_MODEL_ENV_VAR):
        pytest.skip(f"{SMART_TURN_MODEL_ENV_VAR} not set")
    detector = SmartTurnDetector.load()
    p = await detector.predict(_sine_pcm(16000))
    assert 0.0 <= p <= 1.0

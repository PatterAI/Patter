"""Unit tests for getpatter.providers.namo_turn_detector — NamoTurnDetector.

The NAMO Turn Detector v1 (VideoSDK, Apache-2.0) is a TEXT model: it scores
the rolling conversation transcript, not the audio window. All tests inject a
fake onnxruntime session + fake Hugging Face tokenizer (via
``NamoTurnDetector.from_session``) so they run without the model file, the
onnxruntime / transformers native deps, or any network access — the same
mocking discipline as ``test_smart_turn.py``.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

# NAMO's optional extra shares numpy with smart-turn; skip gracefully on CI
# runners that install only the base deps.
np = pytest.importorskip("numpy")

from getpatter.providers import namo_turn_detector as namo_module  # noqa: E402
from getpatter.providers.base import TurnDetectorProvider  # noqa: E402
from getpatter.providers.namo_turn_detector import (  # noqa: E402
    DEFAULT_NAMO_THRESHOLD,
    NAMO_MODEL_ENV_VAR,
    NAMO_REVISION_ENV_VAR,
    NAMO_TOKENIZER_ENV_VAR,
    NamoTurnDetector,
    completion_probability,
    load_namo_tokenizer,
    new_namo_session,
    resolve_namo_model_path,
    resolve_namo_revision,
    resolve_namo_tokenizer_path,
)

# The ONNX session + tokenizer — the only external boundaries — are stubbed.
pytestmark = pytest.mark.mocked


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTokenizer:
    """Minimal stand-in for a Hugging Face ``AutoTokenizer``.

    Deterministically maps text → token ids (id per character, clamped to a
    tiny vocab) and honours ``truncation`` / ``max_length`` so tests can
    assert the token budget is applied. Records every call.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, text, *, truncation=False, max_length=None, return_tensors=None):
        self.calls.append(
            {
                "text": text,
                "truncation": truncation,
                "max_length": max_length,
                "return_tensors": return_tensors,
            }
        )
        ids = [101] + [(ord(c) % 900) + 1 for c in text] + [102]
        if truncation and max_length is not None and len(ids) > max_length:
            ids = ids[:max_length]
        arr = np.array([ids], dtype=np.int64)
        return {"input_ids": arr, "attention_mask": np.ones_like(arr)}


class _FakeOnnxSession:
    """Minimal stand-in for ``onnxruntime.InferenceSession``.

    Returns a programmable sequence of raw logit rows (the last row repeats
    once exhausted) and records every feed so tests can assert the exact
    ``input_ids`` / ``attention_mask`` tensors the model receives.
    """

    def __init__(self, logits_rows) -> None:
        self._rows = [np.asarray(r, dtype=np.float32) for r in logits_rows]
        self._i = 0
        self.calls: list[dict] = []

    def run(self, _output_names, feeds: dict):
        self.calls.append({k: np.array(v, copy=True) for k, v in feeds.items()})
        if self._i >= len(self._rows):
            row = self._rows[-1] if self._rows else np.zeros(2, dtype=np.float32)
        else:
            row = self._rows[self._i]
            self._i += 1
        return [row.reshape(1, -1)]


def _build_detector(
    *, logits_rows=((0.0, 5.0),), threshold: float = 0.5, max_tokens: int = 128
) -> tuple[NamoTurnDetector, _FakeOnnxSession, _FakeTokenizer]:
    session = _FakeOnnxSession(logits_rows)
    tokenizer = _FakeTokenizer()
    detector = NamoTurnDetector.from_session(
        session=session,  # type: ignore[arg-type]
        tokenizer=tokenizer,
        threshold=threshold,
        max_tokens=max_tokens,
    )
    return detector, session, tokenizer


# ---------------------------------------------------------------------------
# Model-path resolution (no runtime needed — resolution happens first)
# ---------------------------------------------------------------------------


def test_load_without_model_path_or_env_raises(monkeypatch) -> None:
    monkeypatch.delenv(NAMO_MODEL_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="no model file configured"):
        resolve_namo_model_path(None)


def test_load_with_missing_file_raises_file_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(NAMO_MODEL_ENV_VAR, raising=False)
    with pytest.raises(FileNotFoundError, match="not found"):
        resolve_namo_model_path(tmp_path / "model.onnx")


def test_env_var_path_is_resolved(tmp_path, monkeypatch) -> None:
    model_file = tmp_path / "model.onnx"
    model_file.write_bytes(b"\x00")
    monkeypatch.setenv(NAMO_MODEL_ENV_VAR, str(model_file))
    assert resolve_namo_model_path(None) == model_file


def test_explicit_path_wins_over_env(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "env.onnx"
    env_file.write_bytes(b"\x00")
    arg_file = tmp_path / "arg.onnx"
    arg_file.write_bytes(b"\x00")
    monkeypatch.setenv(NAMO_MODEL_ENV_VAR, str(env_file))
    assert resolve_namo_model_path(arg_file) == arg_file


def test_model_path_pointing_at_directory_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(NAMO_MODEL_ENV_VAR, str(tmp_path))
    with pytest.raises(FileNotFoundError, match="not a file"):
        resolve_namo_model_path(None)


def test_tokenizer_path_defaults_to_model_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(NAMO_TOKENIZER_ENV_VAR, raising=False)
    model_file = tmp_path / "model.onnx"
    assert resolve_namo_tokenizer_path(None, model_file) == str(tmp_path)


def test_tokenizer_env_overrides_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(NAMO_TOKENIZER_ENV_VAR, "/some/tokenizer/dir")
    model_file = tmp_path / "model.onnx"
    assert resolve_namo_tokenizer_path(None, model_file) == "/some/tokenizer/dir"
    # An explicit argument still wins over the env var.
    assert resolve_namo_tokenizer_path("/explicit", model_file) == "/explicit"


# ---------------------------------------------------------------------------
# Revision resolution — Hugging Face Hub download pinning (Bandit B615)
# ---------------------------------------------------------------------------


def test_resolve_revision_defaults_to_none_without_arg_or_env(monkeypatch) -> None:
    monkeypatch.delenv(NAMO_REVISION_ENV_VAR, raising=False)
    assert resolve_namo_revision(None) is None


def test_resolve_revision_reads_env_var(monkeypatch) -> None:
    monkeypatch.setenv(NAMO_REVISION_ENV_VAR, "abc1234")
    assert resolve_namo_revision(None) == "abc1234"


def test_resolve_revision_explicit_arg_wins_over_env(monkeypatch) -> None:
    monkeypatch.setenv(NAMO_REVISION_ENV_VAR, "env-revision")
    assert resolve_namo_revision("explicit-revision") == "explicit-revision"


def test_load_namo_tokenizer_passes_revision_through(monkeypatch) -> None:
    """The Hugging Face ``from_pretrained`` call must receive whatever
    revision was resolved, so a repo-id ``tokenizer_path`` can be pinned to
    an immutable commit/tag (Bandit B615: unsafe unpinned Hub download)."""
    calls: list[dict] = []

    class _FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(path, revision=None):
            calls.append({"path": path, "revision": revision})
            return object()

    fake_transformers = types.ModuleType("transformers")
    fake_transformers.AutoTokenizer = _FakeAutoTokenizer
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    load_namo_tokenizer("org/some-repo", revision="deadbeef")
    assert calls == [{"path": "org/some-repo", "revision": "deadbeef"}]

    load_namo_tokenizer("/local/tokenizer/dir")
    assert calls[-1] == {"path": "/local/tokenizer/dir", "revision": None}


async def test_load_end_to_end_threads_revision_into_tokenizer(
    tmp_path, monkeypatch
) -> None:
    model_file = tmp_path / "model.onnx"
    model_file.write_bytes(b"\x00")
    monkeypatch.setenv(NAMO_MODEL_ENV_VAR, str(model_file))
    monkeypatch.setenv(NAMO_TOKENIZER_ENV_VAR, "org/some-repo")
    monkeypatch.delenv(NAMO_REVISION_ENV_VAR, raising=False)

    captured: dict = {}

    def _fake_load_namo_tokenizer(tokenizer_path, *, revision=None):
        captured["tokenizer_path"] = tokenizer_path
        captured["revision"] = revision
        return object()

    monkeypatch.setattr(
        namo_module, "load_namo_tokenizer", _fake_load_namo_tokenizer
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # never reached

    with pytest.raises(ImportError, match="onnxruntime"):
        NamoTurnDetector.load(revision="pinned-sha")

    assert captured == {"tokenizer_path": "org/some-repo", "revision": "pinned-sha"}


# ---------------------------------------------------------------------------
# Optional-dependency handling → clear ImportError (test d)
# ---------------------------------------------------------------------------


def test_new_session_without_onnxruntime_raises_install_hint(
    tmp_path, monkeypatch
) -> None:
    """Missing onnxruntime → a descriptive ImportError naming the extra, not
    an opaque ModuleNotFoundError."""
    model_file = tmp_path / "model.onnx"
    model_file.write_bytes(b"\x00")
    # sys.modules[name] = None makes ``import name`` raise ImportError,
    # regardless of whether the real package is installed.
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    with pytest.raises(ImportError, match="namo-turn-detector"):
        new_namo_session(force_cpu=True, model_path=model_file)


def test_new_session_without_numpy_raises_install_hint(
    tmp_path, monkeypatch
) -> None:
    model_file = tmp_path / "model.onnx"
    model_file.write_bytes(b"\x00")
    monkeypatch.setattr(namo_module, "np", None)  # simulate missing extra
    with pytest.raises(ImportError, match="numpy"):
        new_namo_session(force_cpu=True, model_path=model_file)


def test_load_tokenizer_without_transformers_raises_install_hint(
    monkeypatch,
) -> None:
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(ImportError, match="namo-turn-detector"):
        load_namo_tokenizer("/any/tokenizer/dir")


def test_load_end_to_end_surfaces_missing_transformers(tmp_path, monkeypatch) -> None:
    model_file = tmp_path / "model.onnx"
    model_file.write_bytes(b"\x00")
    monkeypatch.setenv(NAMO_MODEL_ENV_VAR, str(model_file))
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(ImportError, match="transformers"):
        NamoTurnDetector.load()


# ---------------------------------------------------------------------------
# maybe_load — degrade (warn once + None) instead of raising
# ---------------------------------------------------------------------------


def test_maybe_load_unconfigured_returns_none_with_single_warning(
    monkeypatch, caplog
) -> None:
    monkeypatch.delenv(NAMO_MODEL_ENV_VAR, raising=False)
    with caplog.at_level(logging.WARNING, logger="getpatter"):
        assert NamoTurnDetector.maybe_load() is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "falling back to plain" in warnings[0].getMessage()


def test_maybe_load_still_rejects_out_of_range_threshold(monkeypatch) -> None:
    monkeypatch.delenv(NAMO_MODEL_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="threshold"):
        NamoTurnDetector.maybe_load(threshold=1.5)


# ---------------------------------------------------------------------------
# completion_probability — logits → probability
# ---------------------------------------------------------------------------


def test_completion_probability_two_class_softmax() -> None:
    # Class 1 (COMPLETE) dominates → high probability.
    assert completion_probability([0.0, 5.0]) > 0.99
    # Class 0 (INCOMPLETE) dominates → low probability.
    assert completion_probability([5.0, 0.0]) < 0.01


def test_completion_probability_single_logit_sigmoid() -> None:
    assert completion_probability([0.0]) == pytest.approx(0.5, abs=1e-6)
    assert completion_probability([10.0]) > 0.99
    assert completion_probability([-10.0]) < 0.01


# ---------------------------------------------------------------------------
# predict() — text scoring through a stubbed ONNX session (tests a, b)
# ---------------------------------------------------------------------------


async def test_complete_transcript_scores_at_or_above_threshold() -> None:
    detector, session, tokenizer = _build_detector(logits_rows=[(0.0, 6.0)])
    p = await detector.predict(b"", transcript="What time is the meeting tomorrow?")
    assert p >= detector.threshold
    assert len(session.calls) == 1
    assert len(tokenizer.calls) == 1


async def test_incomplete_transcript_scores_below_threshold() -> None:
    detector, session, _ = _build_detector(logits_rows=[(6.0, 0.0)])
    # A trailing-conjunction / mid-sentence utterance the model judges "not
    # done yet".
    p = await detector.predict(b"", transcript="I would like to order a pizza and")
    assert p < detector.threshold
    assert len(session.calls) == 1


async def test_empty_transcript_short_circuits_to_zero() -> None:
    detector, session, tokenizer = _build_detector(logits_rows=[(0.0, 9.0)])
    assert await detector.predict(b"\x00" * 640, transcript=None) == 0.0
    assert await detector.predict(b"\x00" * 640, transcript="   ") == 0.0
    # The model is never consulted when there is no text to score.
    assert session.calls == []
    assert tokenizer.calls == []


async def test_predict_feeds_input_ids_and_attention_mask_int64() -> None:
    detector, session, _ = _build_detector(logits_rows=[(0.0, 3.0)])
    await detector.predict(b"", transcript="hello there")
    feeds = session.calls[0]
    assert set(feeds.keys()) == {"input_ids", "attention_mask"}
    assert feeds["input_ids"].dtype == np.int64
    assert feeds["attention_mask"].dtype == np.int64
    assert feeds["input_ids"].shape[0] == 1
    assert feeds["attention_mask"].shape == feeds["input_ids"].shape


async def test_predict_truncates_transcript_to_max_tokens() -> None:
    detector, session, tokenizer = _build_detector(
        logits_rows=[(0.0, 1.0)], max_tokens=8
    )
    await detector.predict(b"", transcript="a" * 200)
    # Tokenizer asked to truncate at the configured budget …
    assert tokenizer.calls[0]["truncation"] is True
    assert tokenizer.calls[0]["max_length"] == 8
    # … and the fed tensor honours it.
    assert session.calls[0]["input_ids"].shape[1] == 8


async def test_predict_ignores_audio_window() -> None:
    detector, session, _ = _build_detector(logits_rows=[(0.0, 5.0)])
    p_audio = await detector.predict(b"\x11" * 3200, transcript="are we done here?")
    detector2, _, _ = _build_detector(logits_rows=[(0.0, 5.0)])
    p_no_audio = await detector2.predict(b"", transcript="are we done here?")
    assert p_audio == pytest.approx(p_no_audio, abs=1e-9)


# ---------------------------------------------------------------------------
# Surface / lifecycle
# ---------------------------------------------------------------------------


def test_is_turn_detector_provider() -> None:
    detector, _, _ = _build_detector()
    assert isinstance(detector, TurnDetectorProvider)


def test_surface_tags_and_defaults() -> None:
    detector, _, _ = _build_detector(threshold=0.62, max_tokens=64)
    assert detector.model == "namo-turn-detector-v1"
    assert detector.provider == "ONNX"
    assert detector.threshold == 0.62
    assert detector.max_tokens == 64


def test_default_threshold() -> None:
    detector, _, _ = _build_detector()
    assert detector.threshold == DEFAULT_NAMO_THRESHOLD == 0.5


def test_from_session_rejects_out_of_range_threshold() -> None:
    with pytest.raises(ValueError, match="threshold"):
        NamoTurnDetector.from_session(
            session=_FakeOnnxSession([(0.0, 1.0)]),  # type: ignore[arg-type]
            tokenizer=_FakeTokenizer(),
            threshold=2.0,
        )


async def test_close_is_idempotent_and_predict_after_close_raises() -> None:
    detector, _, _ = _build_detector()
    await detector.close()
    await detector.close()  # must not raise
    with pytest.raises(RuntimeError, match="closed"):
        await detector.predict(b"", transcript="anything")

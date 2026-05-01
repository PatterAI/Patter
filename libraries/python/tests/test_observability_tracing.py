"""Unit tests for the observability module."""

from __future__ import annotations

import pytest

from getpatter.observability import tracing


def test_is_enabled_false_when_flag_unset(monkeypatch):
    monkeypatch.delenv(tracing.ENV_FLAG, raising=False)
    tracing.shutdown_tracing()
    assert tracing.is_enabled() is False


def test_start_span_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv(tracing.ENV_FLAG, raising=False)
    tracing.shutdown_tracing()
    with tracing.start_span("getpatter.stt", {"a": 1}) as span:
        assert span is None  # no-op mode yields None


def test_init_tracing_returns_false_when_flag_unset(monkeypatch):
    monkeypatch.delenv(tracing.ENV_FLAG, raising=False)
    tracing.shutdown_tracing()
    assert tracing.init_tracing() is False


def test_init_tracing_warns_when_sdk_missing(monkeypatch, caplog):
    # Simulate OTel SDK being unavailable by making the import fail.
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setenv(tracing.ENV_FLAG, "1")
    tracing.shutdown_tracing()
    monkeypatch.setattr(builtins, "__import__", _blocked)

    result = tracing.init_tracing()
    assert result is False


def test_init_tracing_with_sdk_sets_is_enabled(monkeypatch):
    # Only run if the SDK is actually installed.
    try:
        import opentelemetry  # noqa: F401
        import opentelemetry.sdk  # noqa: F401
        import opentelemetry.sdk.resources  # noqa: F401
        import opentelemetry.sdk.trace  # noqa: F401
    except ImportError:
        pytest.skip("opentelemetry SDK not installed")

    monkeypatch.setenv(tracing.ENV_FLAG, "1")
    tracing.shutdown_tracing()

    try:
        ok = tracing.init_tracing(service_name="patter-test")
        assert ok is True
        assert tracing.is_enabled() is True

        # A span context manager should succeed and yield a real span.
        with tracing.start_span("getpatter.stt", {"getpatter.stt.text_len": 4}) as span:
            assert span is not None
    finally:
        tracing.shutdown_tracing()


def test_span_names_are_stable():
    assert tracing.SPAN_STT == "getpatter.stt"
    assert tracing.SPAN_LLM == "getpatter.llm"
    assert tracing.SPAN_TTS == "getpatter.tts"
    assert tracing.SPAN_TOOL == "getpatter.tool"
    assert tracing.SPAN_CALL == "getpatter.call"

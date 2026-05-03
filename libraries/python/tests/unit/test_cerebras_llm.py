"""Regression tests for the Cerebras provider default model + 404 handling.

Why: the previous default ``llama-3.3-70b`` returned a silent 404 on Cerebras
free tier (model gated to paid plans). The fix lowers the default to
``llama3.1-8b`` (free-tier available, sub-100ms TTFT) and translates 404
model_not_found into a clear log message that names override candidates.

Behaviour matches the TS provider: log at ERROR level and exit the stream
quietly. Voice pipelines treat LLM provider failures as recoverable (the
call continues, the user just hears no LLM response), so raising would be
a behavioural change for callers.

The Cerebras ``stream()`` is now a thin wrapper over the parent
``OpenAILLMProvider.stream`` (refactor: sampling kwargs live in the parent).
Tests mock the parent's ``stream`` so the 404 try/except wrapper is what's
exercised, regardless of which transport-layer call actually raised.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator
from unittest.mock import patch

import pytest

from getpatter.providers.cerebras_llm import CerebrasLLMProvider
from getpatter.services.llm_loop import OpenAILLMProvider


def _provider(model: str | None = None) -> CerebrasLLMProvider:
    """Build a CerebrasLLMProvider with the optional extras disabled.

    The base install of the test environment doesn't pull in ``msgpack``;
    these tests don't exercise wire compression, so we always disable it.
    """
    kwargs: dict = {
        "api_key": "csk-test",
        "gzip_compression": False,
        "msgpack_encoding": False,
    }
    if model is not None:
        kwargs["model"] = model
    return CerebrasLLMProvider(**kwargs)


def _failing_parent_stream(exc: Exception):
    """Patch ``OpenAILLMProvider.stream`` to raise ``exc`` on iteration.

    The Cerebras subclass calls ``super().stream(...)`` and wraps the
    iterator in a try/except; the patched async generator raises so the
    wrapper's exception path is what the test exercises.
    """

    async def _raises(self, messages, tools=None, **_kwargs):  # type: ignore[no-untyped-def]
        raise exc
        yield  # pragma: no cover — make the function an async generator

    return patch.object(OpenAILLMProvider, "stream", new=_raises)


def test_default_model_is_gpt_oss_120b() -> None:
    """Default Cerebras model is the highest-throughput production tier.

    On WSE-3 hardware, gpt-oss-120b runs at ~3000 tok/sec — well above the
    TTS consumption rate (~150-300 tok/sec), so model size doesn't bottleneck
    realtime voice. The 8B and preview models remain reachable via
    ``model=`` for accounts whose tier doesn't include this default.
    """
    assert _provider()._model == "gpt-oss-120b"


def test_explicit_model_override_is_honoured() -> None:
    assert _provider("llama3.1-8b")._model == "llama3.1-8b"


@pytest.mark.asyncio
async def test_404_model_not_found_is_logged_with_recovery_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A gated model surfaces an ERROR log naming override candidates and
    /v1/models, then the stream completes without yielding chunks."""

    provider = _provider("gated-model")

    upstream = RuntimeError(
        'HTTP 404 — {"message":"Model gated-model does not exist or you do '
        'not have access to it.","type":"not_found_error","param":"model",'
        '"code":"model_not_found"}'
    )

    with _failing_parent_stream(upstream):
        with caplog.at_level(logging.ERROR, logger="getpatter.providers.cerebras_llm"):
            chunks = [
                chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}])
            ]

    assert chunks == []  # stream exits silently — no chunks emitted
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "gated-model" in log_text
    assert "not available on your tier" in log_text
    assert "llama3.1-8b" in log_text  # override hint
    assert "/v1/models" in log_text  # discovery hint


@pytest.mark.asyncio
async def test_other_errors_are_re_raised_unchanged() -> None:
    """Non-model errors should propagate unchanged."""

    provider = _provider()

    upstream = ValueError("unrelated failure")

    with _failing_parent_stream(upstream):
        with pytest.raises(ValueError, match="unrelated failure"):
            async for _ in provider.stream([{"role": "user", "content": "hi"}]):
                pass


@pytest.mark.asyncio
async def test_stream_delegates_to_parent_for_chunks() -> None:
    """Happy path: chunks yielded by the parent are forwarded unchanged.

    Confirms the thin-wrapper design — Cerebras owns no SSE parsing of its
    own; everything goes through ``OpenAILLMProvider.stream``.
    """

    provider = _provider()

    parent_chunks = [
        {"type": "text", "content": "hello"},
        {"type": "text", "content": " world"},
    ]

    async def _yields(self, messages, tools=None, **_kwargs) -> AsyncIterator[dict]:  # type: ignore[no-untyped-def]
        for c in parent_chunks:
            yield c

    with patch.object(OpenAILLMProvider, "stream", new=_yields):
        out = [c async for c in provider.stream([{"role": "user", "content": "hi"}])]

    assert out == parent_chunks

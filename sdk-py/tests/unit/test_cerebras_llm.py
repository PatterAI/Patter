"""Regression tests for the Cerebras provider default model + 404 handling.

Why: the previous default ``llama-3.3-70b`` returned a silent 404 on Cerebras
free tier (model gated to paid plans). The fix lowers the default to
``llama3.1-8b`` (free-tier available, sub-100ms TTFT) and translates 404
model_not_found into a clear log message that names override candidates.

Behaviour matches the TS provider: log at ERROR level and exit the stream
quietly. Voice pipelines treat LLM provider failures as recoverable (the
call continues, the user just hears no LLM response), so raising would be
a behavioural change for callers.
"""

from __future__ import annotations

import logging
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


def _async_iter_raises(exc: Exception):
    """Return an async-iterable that raises ``exc`` on the first ``__anext__``.

    Replaces the prior ``side_effect=async_gen_func`` pattern, which relied on
    the implicit semantics of mocking an async generator function. Returning
    a real async iterable from ``side_effect`` is unambiguous: the mock call
    returns this object, and the caller's ``async for`` invokes ``__anext__``,
    which raises.
    """

    class _Iter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise exc

    return _Iter()


def test_default_model_is_free_tier_safe() -> None:
    assert _provider()._model == "llama3.1-8b"


def test_explicit_model_override_is_honoured() -> None:
    assert _provider("llama-3.3-70b")._model == "llama-3.3-70b"


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

    # Patch via the class object itself so MRO resolution from
    # CerebrasLLMProvider's super().stream() unambiguously hits the mock,
    # regardless of whether OpenAILLMProvider was imported under another name.
    with patch.object(
        OpenAILLMProvider, "stream", return_value=_async_iter_raises(upstream)
    ):
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

    with patch.object(
        OpenAILLMProvider, "stream", return_value=_async_iter_raises(upstream)
    ):
        with pytest.raises(ValueError, match="unrelated failure"):
            async for _ in provider.stream([{"role": "user", "content": "hi"}]):
                pass

"""Regression tests for the Cerebras provider default model + 404 handling.

Why: the previous default ``llama-3.3-70b`` returned a silent 404 on Cerebras
free tier (model gated to paid plans). The fix lowers the default to
``llama3.1-8b`` (free-tier available, sub-100ms TTFT) and translates 404
model_not_found into a recovery hint that names override candidates.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from getpatter.providers.cerebras_llm import CerebrasLLMProvider


def test_default_model_is_free_tier_safe() -> None:
    provider = CerebrasLLMProvider(api_key="csk-test", gzip_compression=False, msgpack_encoding=False)
    assert provider._model == "llama3.1-8b"


def test_explicit_model_override_is_honoured() -> None:
    provider = CerebrasLLMProvider(
        api_key="csk-test",
        model="llama-3.3-70b",
        gzip_compression=False,
        msgpack_encoding=False,
    )
    assert provider._model == "llama-3.3-70b"


@pytest.mark.asyncio
async def test_404_model_not_found_raises_recovery_hint() -> None:
    """A gated model surfaces a clear error with override candidates."""

    provider = CerebrasLLMProvider(
        api_key="csk-test",
        model="gated-model",
        gzip_compression=False,
        msgpack_encoding=False,
    )

    async def _raise_404(*_args: object, **_kwargs: object):
        raise RuntimeError(
            'HTTP 404 — {"message":"Model gated-model does not exist or you do '
            'not have access to it.","type":"not_found_error","param":"model",'
            '"code":"model_not_found"}'
        )
        yield  # make this an async generator (unreachable)

    with patch(
        "getpatter.services.llm_loop.OpenAILLMProvider.stream",
        side_effect=_raise_404,
    ):
        with pytest.raises(RuntimeError) as excinfo:
            async for _ in provider.stream([{"role": "user", "content": "hi"}]):
                pass

    msg = str(excinfo.value)
    assert "gated-model" in msg
    assert "not available on your tier" in msg
    assert "llama3.1-8b" in msg  # override hint
    assert "/v1/models" in msg  # discovery hint


@pytest.mark.asyncio
async def test_other_errors_are_re_raised_unchanged() -> None:
    """Non-model errors should propagate unchanged."""

    provider = CerebrasLLMProvider(api_key="csk-test", gzip_compression=False, msgpack_encoding=False)

    async def _raise_other(*_args: object, **_kwargs: object):
        raise ValueError("unrelated failure")
        yield

    with patch(
        "getpatter.services.llm_loop.OpenAILLMProvider.stream",
        side_effect=_raise_other,
    ):
        with pytest.raises(ValueError, match="unrelated failure"):
            async for _ in provider.stream([{"role": "user", "content": "hi"}]):
                pass

"""Tests for the LiteLLM LLM provider."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_chunk(content=None, tool_calls=None, usage=None):
    chunk = MagicMock()
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    choice = MagicMock()
    choice.delta = delta
    chunk.choices = [choice] if (content is not None or tool_calls is not None) else []
    chunk.usage = usage
    return chunk


def _make_usage(prompt=10, completion=20):
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.prompt_tokens_details = None
    return usage


async def _async_iter(items):
    for item in items:
        yield item


@pytest.fixture
def mock_litellm():
    mock = MagicMock()
    with patch.dict(sys.modules, {"litellm": mock}):
        yield mock


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_litellm_stream_text(mock_litellm):
    mock_litellm.acompletion = AsyncMock(
        return_value=_async_iter([
            _make_chunk(content="Hello "),
            _make_chunk(content="world!"),
            _make_chunk(usage=_make_usage()),
        ])
    )
    from getpatter.providers.litellm_llm import LiteLLMProvider

    provider = LiteLLMProvider(api_key="test-key", model="gpt-4o")
    chunks = []
    async for chunk in provider.stream([{"role": "user", "content": "Hi"}]):
        chunks.append(chunk)

    text_chunks = [c for c in chunks if c["type"] == "text"]
    assert text_chunks == [
        {"type": "text", "content": "Hello "},
        {"type": "text", "content": "world!"},
    ]
    assert any(c["type"] == "usage" for c in chunks)
    assert any(c["type"] == "done" for c in chunks)


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_litellm_stream_tool_call(mock_litellm):
    tc = MagicMock()
    tc.index = 0
    tc.id = "call_123"
    tc.function.name = "get_weather"
    tc.function.arguments = '{"city": "Paris"}'

    mock_litellm.acompletion = AsyncMock(
        return_value=_async_iter([
            _make_chunk(tool_calls=[tc]),
        ])
    )
    from getpatter.providers.litellm_llm import LiteLLMProvider

    provider = LiteLLMProvider(api_key="test-key", model="gpt-4o")
    chunks = []
    async for chunk in provider.stream([{"role": "user", "content": "weather?"}]):
        chunks.append(chunk)

    tool_chunks = [c for c in chunks if c["type"] == "tool_call"]
    assert len(tool_chunks) == 1
    assert tool_chunks[0]["name"] == "get_weather"
    assert tool_chunks[0]["id"] == "call_123"


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_litellm_cancel_event(mock_litellm):
    cancel = asyncio.Event()
    cancel.set()

    mock_litellm.acompletion = AsyncMock(
        return_value=_async_iter([
            _make_chunk(content="should not appear"),
        ])
    )
    from getpatter.providers.litellm_llm import LiteLLMProvider

    provider = LiteLLMProvider(api_key="test-key", model="gpt-4o")
    chunks = []
    async for chunk in provider.stream(
        [{"role": "user", "content": "Hi"}], cancel_event=cancel
    ):
        chunks.append(chunk)

    assert len(chunks) == 0


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_litellm_kwargs_forwarded(mock_litellm):
    mock_litellm.acompletion = AsyncMock(return_value=_async_iter([]))
    from getpatter.providers.litellm_llm import LiteLLMProvider

    provider = LiteLLMProvider(
        api_key="test-key",
        model="gpt-4o",
        temperature=0.5,
        max_tokens=100,
        api_base="https://my-proxy.com/v1",
    )
    chunks = [c async for c in provider.stream([{"role": "user", "content": "Hi"}])]

    call_kwargs = mock_litellm.acompletion.call_args[1]
    assert call_kwargs["temperature"] == 0.5
    assert call_kwargs["max_completion_tokens"] == 100
    assert call_kwargs["api_base"] == "https://my-proxy.com/v1"
    assert call_kwargs["drop_params"] is True


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_litellm_error_propagates(mock_litellm):
    mock_litellm.acompletion = AsyncMock(side_effect=Exception("API Error"))
    from getpatter.providers.litellm_llm import LiteLLMProvider

    provider = LiteLLMProvider(api_key="test-key", model="gpt-4o")
    with pytest.raises(Exception, match="API Error"):
        async for _ in provider.stream([{"role": "user", "content": "Hi"}]):
            pass


@pytest.mark.mocked
def test_litellm_llm_wrapper_env_fallback(mock_litellm):
    with patch.dict("os.environ", {"LITELLM_API_KEY": "env-key"}):
        from getpatter.llm.litellm import LLM

        llm = LLM(model="gpt-4o")
        assert llm._api_key == "env-key"


@pytest.mark.mocked
def test_litellm_missing_dependency():
    with patch.dict(sys.modules, {"litellm": None}):
        with pytest.raises(RuntimeError, match="litellm"):
            from getpatter.providers.litellm_llm import LiteLLMProvider

            LiteLLMProvider(api_key="test")

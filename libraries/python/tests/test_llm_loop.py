"""Tests for the built-in LLM loop (pipeline mode without on_message)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _aiter(items):
    for item in items:
        yield item


class FakeLLMProvider:
    """A fake LLM provider that yields pre-defined chunk sequences.

    Each call to ``stream()`` pops the next sequence from ``call_results``.
    If only one sequence is provided it is reused for every call.
    """

    def __init__(self, call_results: list[list[dict]]) -> None:
        self._call_results = list(call_results)
        self._call_index = 0

    async def stream(self, messages, tools=None):
        idx = min(self._call_index, len(self._call_results) - 1)
        self._call_index += 1
        for chunk in self._call_results[idx]:
            yield chunk


@pytest.fixture
def mock_openai():
    with patch("getpatter.services.llm_loop.LLMLoop.__init__", return_value=None) as mock_init:
        yield mock_init


def _make_llm_loop(tools=None, tool_executor=None, provider=None):
    """Create an LLMLoop with a fake provider."""
    from getpatter.services.llm_loop import LLMLoop

    loop = LLMLoop.__new__(LLMLoop)
    loop._provider = provider or FakeLLMProvider([[]])
    loop._system_prompt = "You are a test assistant."
    loop._tools = tools
    loop._tool_executor = tool_executor
    loop._metrics = None
    loop._event_bus = None
    loop._model = "fake-model"
    loop._provider_name = "fake"
    loop._openai_tools = None
    loop._tool_map = {}
    if tools:
        loop._openai_tools = []
        for t in tools:
            fn = {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            }
            loop._openai_tools.append({"type": "function", "function": fn})
            loop._tool_map[t["name"]] = t
    return loop


@pytest.mark.asyncio
async def test_streaming_text_response():
    """LLM loop yields text tokens from a simple response."""
    provider = FakeLLMProvider([
        [
            {"type": "text", "content": "Hello "},
            {"type": "text", "content": "world!"},
        ],
    ])
    loop = _make_llm_loop(provider=provider)

    tokens = []
    async for token in loop.run("Hi", [], {"call_id": "test"}):
        tokens.append(token)

    assert tokens == ["Hello ", "world!"]


@pytest.mark.asyncio
async def test_tool_call_then_text():
    """LLM loop handles tool call, re-submits, then yields text."""
    tool = {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {"type": "object", "properties": {}},
        "handler": lambda args, ctx: '{"temp": 72}',
    }
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value='{"temp": 72}')

    provider = FakeLLMProvider([
        # First call: tool call
        [
            {"type": "tool_call", "index": 0, "id": "call_123", "name": "get_weather", "arguments": "{}"},
        ],
        # Second call: text response
        [
            {"type": "text", "content": "It's 72\u00b0F."},
        ],
    ])

    loop = _make_llm_loop(tools=[tool], tool_executor=executor, provider=provider)

    tokens = []
    async for token in loop.run("What's the weather?", [], {"call_id": "test"}):
        tokens.append(token)

    assert tokens == ["It's 72\u00b0F."]
    executor.execute.assert_called_once_with(
        tool_name="get_weather",
        arguments={},
        call_context={"call_id": "test"},
        webhook_url="",
        handler=tool["handler"],
    )


@pytest.mark.asyncio
async def test_empty_response():
    """LLM loop handles empty response gracefully."""
    provider = FakeLLMProvider([
        [],
    ])
    loop = _make_llm_loop(provider=provider)

    tokens = []
    async for token in loop.run("Hi", [], {"call_id": "test"}):
        tokens.append(token)

    assert tokens == []


@pytest.mark.asyncio
async def test_build_messages_from_history():
    """_build_messages correctly constructs OpenAI messages."""
    loop = _make_llm_loop()

    history = [
        {"role": "user", "text": "Hello", "timestamp": 1.0},
        {"role": "assistant", "text": "Hi there!", "timestamp": 2.0},
    ]
    messages = loop._build_messages(history, "How are you?")

    assert messages[0] == {"role": "system", "content": "You are a test assistant."}
    assert messages[1] == {"role": "user", "content": "Hello"}
    assert messages[2] == {"role": "assistant", "content": "Hi there!"}
    assert messages[3] == {"role": "user", "content": "How are you?"}


@pytest.mark.asyncio
async def test_max_iterations_guard():
    """LLM loop stops after max iterations to prevent infinite tool loops."""
    tool = {
        "name": "loop_tool",
        "description": "Always called",
        "parameters": {"type": "object", "properties": {}},
    }
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value='{"ok": true}')

    # Every call returns a tool call (infinite loop scenario).
    # FakeLLMProvider reuses the last entry when exhausted, so a single
    # sequence suffices.
    provider = FakeLLMProvider([
        [
            {"type": "tool_call", "index": 0, "id": "call_inf", "name": "loop_tool", "arguments": "{}"},
        ],
    ])

    loop = _make_llm_loop(tools=[tool], tool_executor=executor, provider=provider)

    tokens = []
    async for token in loop.run("trigger", [], {"call_id": "test"}):
        tokens.append(token)

    # Should have called execute 10 times (max_iterations)
    assert executor.execute.call_count == 10


@pytest.mark.asyncio
async def test_custom_llm_provider_via_constructor():
    """LLMLoop accepts a custom llm_provider, skipping OpenAI init."""
    from getpatter.services.llm_loop import LLMLoop

    provider = FakeLLMProvider([
        [{"type": "text", "content": "custom!"}],
    ])

    loop = LLMLoop(
        openai_key="unused",
        model="unused",
        system_prompt="test",
        llm_provider=provider,
    )

    tokens = []
    async for token in loop.run("Hi", [], {"call_id": "test"}):
        tokens.append(token)

    assert tokens == ["custom!"]

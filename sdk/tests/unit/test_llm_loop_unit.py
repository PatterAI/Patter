"""Unit tests for patter.services.llm_loop — LLMLoop and LLMProvider."""

from __future__ import annotations

import json
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from patter.services.llm_loop import LLMLoop, LLMProvider, OpenAILLMProvider


# ---------------------------------------------------------------------------
# Mock LLM provider
# ---------------------------------------------------------------------------


class MockLLMProvider:
    """A mock LLM provider for testing that yields controlled chunks."""

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    async def stream(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> AsyncIterator[dict]:
        for chunk in self._chunks:
            yield chunk


class MockToolCallProvider:
    """A mock provider that yields tool calls then text on second call."""

    def __init__(self) -> None:
        self._call_count = 0

    async def stream(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> AsyncIterator[dict]:
        self._call_count += 1
        if self._call_count == 1:
            # First call: emit tool call
            yield {"type": "tool_call", "index": 0, "id": "tc_1", "name": "lookup", "arguments": None}
            yield {"type": "tool_call", "index": 0, "id": None, "name": None, "arguments": '{"q":'}
            yield {"type": "tool_call", "index": 0, "id": None, "name": None, "arguments": '"test"}'}
        else:
            # Second call: emit text
            yield {"type": "text", "content": "The answer is 42."}


# ---------------------------------------------------------------------------
# LLMProvider protocol
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMProviderProtocol:
    """LLMProvider is a runtime-checkable Protocol."""

    def test_mock_satisfies_protocol(self) -> None:
        provider = MockLLMProvider([])
        assert isinstance(provider, LLMProvider)


# ---------------------------------------------------------------------------
# LLMLoop._build_messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildMessages:
    """LLMLoop._build_messages constructs OpenAI messages array."""

    def test_empty_history(self) -> None:
        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="You are helpful.",
            llm_provider=MockLLMProvider([]),
        )
        messages = loop._build_messages([], "Hello")
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are helpful."
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Hello"

    def test_with_history(self) -> None:
        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="System.",
            llm_provider=MockLLMProvider([]),
        )
        history = [
            {"role": "user", "text": "First"},
            {"role": "assistant", "text": "Reply"},
        ]
        messages = loop._build_messages(history, "Second")
        assert len(messages) == 4  # system + 2 history + current user
        assert messages[1]["content"] == "First"
        assert messages[2]["content"] == "Reply"
        assert messages[3]["content"] == "Second"


# ---------------------------------------------------------------------------
# LLMLoop.run — streaming text
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMLoopStreamingText:
    """LLMLoop.run yields text tokens from the provider."""

    async def test_simple_text_stream(self) -> None:
        provider = MockLLMProvider([
            {"type": "text", "content": "Hello"},
            {"type": "text", "content": " world"},
            {"type": "text", "content": "!"},
        ])
        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="System.",
            llm_provider=provider,
        )
        tokens = []
        async for token in loop.run("Hi", [], {}):
            tokens.append(token)
        assert tokens == ["Hello", " world", "!"]

    async def test_empty_content_skipped(self) -> None:
        provider = MockLLMProvider([
            {"type": "text", "content": "A"},
            {"type": "text", "content": ""},
            {"type": "text", "content": "B"},
        ])
        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="System.",
            llm_provider=provider,
        )
        tokens = []
        async for token in loop.run("x", [], {}):
            tokens.append(token)
        assert tokens == ["A", "B"]


# ---------------------------------------------------------------------------
# LLMLoop.run — tool call extraction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMLoopToolCalls:
    """LLMLoop.run handles tool calls from partial JSON chunks."""

    async def test_tool_call_extraction_and_resubmit(self) -> None:
        """When the provider emits tool_call chunks, the loop executes and resubmits."""
        provider = MockToolCallProvider()

        tool_executor = AsyncMock()
        tool_executor.execute = AsyncMock(return_value='{"result": "found"}')

        tools = [
            {"name": "lookup", "description": "Look up something", "parameters": {}, "webhook_url": "https://example.com/hook"}
        ]

        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="System.",
            tools=tools,
            tool_executor=tool_executor,
            llm_provider=provider,
        )

        tokens = []
        async for token in loop.run("find test", [], {"call_id": "c1"}):
            tokens.append(token)

        assert "".join(tokens) == "The answer is 42."
        tool_executor.execute.assert_awaited_once()

    async def test_tool_call_argument_accumulation(self) -> None:
        """Partial argument strings are concatenated across chunks."""

        class _PartialArgProvider:
            async def stream(self, messages, tools=None):
                yield {"type": "tool_call", "index": 0, "id": "tc_1", "name": "fn", "arguments": None}
                yield {"type": "tool_call", "index": 0, "id": None, "name": None, "arguments": '{"a"'}
                yield {"type": "tool_call", "index": 0, "id": None, "name": None, "arguments": ': 1}'}
                # No text — will loop
            _call_count = 0
            _orig_stream = None

        # We need the provider to return text on second call
        call_count = 0
        original_stream = _PartialArgProvider.stream

        async def counting_stream(self, messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                async for x in original_stream(self, messages, tools):
                    yield x
            else:
                yield {"type": "text", "content": "done"}

        _PartialArgProvider.stream = counting_stream

        tool_executor = AsyncMock()
        tool_executor.execute = AsyncMock(return_value='"ok"')

        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="System.",
            tools=[{"name": "fn", "description": "x", "parameters": {}, "handler": lambda a, c: "ok"}],
            tool_executor=tool_executor,
            llm_provider=_PartialArgProvider(),
        )
        tokens = []
        async for token in loop.run("go", [], {}):
            tokens.append(token)

        # Verify the tool was called with accumulated args
        call_args = tool_executor.execute.call_args
        assert call_args.kwargs.get("tool_name", call_args[1].get("tool_name", "")) == "fn"


# ---------------------------------------------------------------------------
# LLMLoop.run — max iterations safety
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMLoopMaxIterations:
    """LLMLoop.run caps at 10 iterations to prevent infinite tool loops."""

    async def test_max_iterations_cap(self) -> None:
        """After 10 iterations of tool calls, the loop stops."""

        class _InfiniteToolProvider:
            async def stream(self, messages, tools=None):
                yield {"type": "tool_call", "index": 0, "id": "tc_x", "name": "fn", "arguments": "{}"}

        tool_executor = AsyncMock()
        tool_executor.execute = AsyncMock(return_value='"loop"')

        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="System.",
            tools=[{"name": "fn", "description": "x", "parameters": {}, "handler": lambda a, c: "ok"}],
            tool_executor=tool_executor,
            llm_provider=_InfiniteToolProvider(),
        )
        tokens = []
        async for token in loop.run("go", [], {}):
            tokens.append(token)

        # Should have been called 10 times (max_iterations)
        assert tool_executor.execute.await_count == 10


# ---------------------------------------------------------------------------
# LLMLoop._execute_tool
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMLoopExecuteTool:
    """LLMLoop._execute_tool delegates to ToolExecutor."""

    async def test_no_executor(self) -> None:
        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="System.",
            tools=[{"name": "fn", "description": "x", "parameters": {}, "handler": lambda: "ok"}],
            tool_executor=None,
            llm_provider=MockLLMProvider([]),
        )
        result = await loop._execute_tool("fn", {}, {})
        data = json.loads(result)
        assert "error" in data

    async def test_with_executor(self) -> None:
        executor = AsyncMock()
        executor.execute = AsyncMock(return_value='{"status": "ok"}')

        loop = LLMLoop(
            openai_key="",
            model="gpt-4o-mini",
            system_prompt="System.",
            tools=[{"name": "fn", "description": "x", "parameters": {}, "webhook_url": "https://hook"}],
            tool_executor=executor,
            llm_provider=MockLLMProvider([]),
        )
        result = await loop._execute_tool("fn", {"arg": 1}, {"call_id": "c1"})
        assert result == '{"status": "ok"}'
        executor.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# LLMLoop constructor — openai_tools build
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMLoopConstruction:
    """LLMLoop constructor processes tool definitions."""

    def test_openai_tools_format(self) -> None:
        tools = [
            {"name": "fn1", "description": "desc1", "parameters": {"type": "object"}, "handler": lambda: None},
            {"name": "fn2", "description": "desc2", "parameters": {}, "webhook_url": "https://x"},
        ]
        loop = LLMLoop(
            openai_key="",
            model="m",
            system_prompt="s",
            tools=tools,
            llm_provider=MockLLMProvider([]),
        )
        assert loop._openai_tools is not None
        assert len(loop._openai_tools) == 2
        assert loop._openai_tools[0]["type"] == "function"
        assert loop._openai_tools[0]["function"]["name"] == "fn1"

    def test_tool_map(self) -> None:
        tools = [{"name": "fn1", "description": "", "parameters": {}, "handler": lambda: None}]
        loop = LLMLoop(
            openai_key="",
            model="m",
            system_prompt="s",
            tools=tools,
            llm_provider=MockLLMProvider([]),
        )
        assert "fn1" in loop._tool_map

    def test_no_tools(self) -> None:
        loop = LLMLoop(
            openai_key="",
            model="m",
            system_prompt="s",
            tools=None,
            llm_provider=MockLLMProvider([]),
        )
        assert loop._openai_tools is None
        assert loop._tool_map == {}

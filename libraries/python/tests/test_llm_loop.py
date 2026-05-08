"""Tests for the built-in LLM loop (pipeline mode without on_message)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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

    async def stream(self, messages, tools=None, **_kwargs):
        idx = min(self._call_index, len(self._call_results) - 1)
        self._call_index += 1
        for chunk in self._call_results[idx]:
            yield chunk


@pytest.fixture
def mock_openai():
    with patch(
        "getpatter.services.llm_loop.LLMLoop.__init__", return_value=None
    ) as mock_init:
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
    loop._on_tool_call = None
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
    provider = FakeLLMProvider(
        [
            [
                {"type": "text", "content": "Hello "},
                {"type": "text", "content": "world!"},
            ],
        ]
    )
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

    provider = FakeLLMProvider(
        [
            # First call: tool call
            [
                {
                    "type": "tool_call",
                    "index": 0,
                    "id": "call_123",
                    "name": "get_weather",
                    "arguments": "{}",
                },
            ],
            # Second call: text response
            [
                {"type": "text", "content": "It's 72\u00b0F."},
            ],
        ]
    )

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
    provider = FakeLLMProvider(
        [
            [],
        ]
    )
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
    provider = FakeLLMProvider(
        [
            [
                {
                    "type": "tool_call",
                    "index": 0,
                    "id": "call_inf",
                    "name": "loop_tool",
                    "arguments": "{}",
                },
            ],
        ]
    )

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

    provider = FakeLLMProvider(
        [
            [{"type": "text", "content": "custom!"}],
        ]
    )

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


# ---------------------------------------------------------------------------
# on_tool_call observer (pipeline parity with realtime _emit_tool_event)
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_on_tool_call_observer_fires_after_successful_tool_execution():
    """LLMLoop fires the ``on_tool_call`` observer with (name, args, result)
    after each successful tool execution. Pipeline mode wires this hook
    to ``StreamHandler._record_tool_call`` so tool calls appear in the
    transcript timeline / ``on_transcript`` callback the same way they
    appear in realtime mode (where the handler emits two events directly).
    """
    tool = {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {"type": "object", "properties": {}},
        "handler": lambda args, ctx: '{"temp": 72}',
    }
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value='{"temp": 72}')

    provider = FakeLLMProvider(
        [
            [
                {
                    "type": "tool_call",
                    "index": 0,
                    "id": "call_w1",
                    "name": "get_weather",
                    "arguments": '{"city": "Rome"}',
                },
            ],
            [
                {"type": "text", "content": "It's 72."},
            ],
        ]
    )

    observed: list[tuple[str, dict, str]] = []

    async def on_tool_call(name, args, result):
        observed.append((name, args, result))

    loop = _make_llm_loop(tools=[tool], tool_executor=executor, provider=provider)
    loop.set_on_tool_call(on_tool_call)

    tokens = []
    async for token in loop.run("Weather?", [], {"call_id": "test"}):
        tokens.append(token)

    assert tokens == ["It's 72."]
    assert observed == [("get_weather", {"city": "Rome"}, '{"temp": 72}')]


@pytest.mark.mocked
async def test_on_tool_call_observer_exceptions_do_not_abort_loop():
    """A throwing observer must NOT propagate into the LLM loop —
    logged and swallowed so the live call continues.
    """
    tool = {
        "name": "noisy_tool",
        "description": "Throws in observer",
        "parameters": {"type": "object", "properties": {}},
        "handler": lambda args, ctx: '"ok"',
    }
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value='"ok"')

    provider = FakeLLMProvider(
        [
            [
                {
                    "type": "tool_call",
                    "index": 0,
                    "id": "call_n1",
                    "name": "noisy_tool",
                    "arguments": "{}",
                },
            ],
            [{"type": "text", "content": "done"}],
        ]
    )

    async def boom(*_args, **_kwargs):
        raise RuntimeError("observer failed")

    loop = _make_llm_loop(tools=[tool], tool_executor=executor, provider=provider)
    loop.set_on_tool_call(boom)

    tokens = []
    async for token in loop.run("Run", [], {"call_id": "test"}):
        tokens.append(token)
    # Loop completed despite observer failure
    assert tokens == ["done"]


@pytest.mark.mocked
async def test_pipeline_stream_handler_emits_three_transcript_events_for_tool_turn():
    """End-to-end mocked pipeline LLM-loop: a single user turn that
    triggers one tool call must produce THREE ``on_transcript`` events:
    1) ``role=tool`` with the call (``name(argsJson)``)
    2) ``role=tool`` with the result (``name(...) → result``)
    3) ``role=assistant`` with the final text response

    Mocks only the LLM provider WebSocket (paid external boundary). The
    StreamHandler `_record_tool_call` and `_emit_assistant_transcript`
    helpers run real code paths.
    """
    from collections import deque

    from getpatter.stream_handler import PipelineStreamHandler

    tool = {
        "name": "lookup",
        "description": "Lookup data",
        "parameters": {"type": "object", "properties": {}},
        "handler": lambda args, ctx: '{"value": 42}',
    }
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value='{"value": 42}')

    provider = FakeLLMProvider(
        [
            [
                {
                    "type": "tool_call",
                    "index": 0,
                    "id": "call_l1",
                    "name": "lookup",
                    "arguments": '{"q": "x"}',
                },
            ],
            [{"type": "text", "content": "Found 42."}],
        ]
    )

    # Build a real LLMLoop hooked to a real PipelineStreamHandler
    # (constructed via ``__new__`` so we don't need a live telephony bridge).
    loop = _make_llm_loop(tools=[tool], tool_executor=executor, provider=provider)

    handler = PipelineStreamHandler.__new__(PipelineStreamHandler)
    handler.call_id = "call-abc"
    handler.caller = "+15555550100"
    handler.callee = "+15555550101"
    handler.conversation_history = deque(maxlen=200)
    handler.transcript_entries = deque(maxlen=200)

    transcript_events: list[dict] = []

    async def capture(evt):
        transcript_events.append(evt)

    handler.on_transcript = capture

    # Wire the loop the same way ``PipelineStreamHandler.start`` does.
    loop.set_on_tool_call(handler._record_tool_call)

    # Drive the loop and forward the final assistant text into
    # ``_emit_assistant_transcript`` (mirrors what
    # ``_process_streaming_response`` would do at end-of-turn in real flow).
    parts: list[str] = []
    async for token in loop.run("Go", [], {"call_id": handler.call_id}):
        parts.append(token)
    final_text = "".join(parts)
    if final_text:
        await handler._emit_assistant_transcript(final_text)

    # Assert: 3 transcript events in order — tool call, tool result, assistant
    assert len(transcript_events) == 3, transcript_events
    call_evt, result_evt, assistant_evt = transcript_events

    assert call_evt["role"] == "tool"
    assert call_evt["tool_name"] == "lookup"
    assert call_evt["tool_args"] == {"q": "x"}
    assert call_evt["tool_result"] is None
    assert call_evt["text"].startswith("lookup(")
    assert call_evt["call_id"] == "call-abc"

    assert result_evt["role"] == "tool"
    assert result_evt["tool_name"] == "lookup"
    assert result_evt["tool_result"] == '{"value": 42}'
    assert "→" in result_evt["text"]

    assert assistant_evt["role"] == "assistant"
    assert assistant_evt["text"] == "Found 42."
    assert assistant_evt["call_id"] == "call-abc"
    # History contains tool-call, tool-result, assistant entries
    history_roles = [e["role"] for e in handler.conversation_history]
    assert history_roles == ["tool", "tool", "assistant"]

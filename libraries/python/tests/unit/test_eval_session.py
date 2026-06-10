"""Unit tests for the real-pipeline eval harness (``getpatter.evals.session``).

These tests drive an ACTUAL ``PipelineStreamHandler`` — the real STT receive
loop, ``_handle_barge_in`` / ``_commit_transcript`` / ``_dispatch_turn``, the
real ``LLMLoop`` with the real ``ToolExecutor``, guardrails, sentence
chunking, and metrics — with only the paid/external boundary faked
(telephony audio sender, STT/TTS sockets, and a scripted LLM provider).
No network anywhere.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from getpatter.evals.assertions import expect
from getpatter.evals.session import (
    EvalSession,
    ScriptedLLMProvider,
    text_turn,
    tool_call_turn,
)
from getpatter.exceptions import ErrorCode, PatterConfigError, PatterError
from getpatter.models import Agent


def _make_agent(**overrides) -> Agent:
    defaults = {
        "system_prompt": "You are a concise test agent.",
        "provider": "pipeline",
        "first_message": "",
    }
    return Agent(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# (a) Tool-call case — the REAL ToolExecutor runs a local handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_tool_call_runs_real_tool_executor_and_is_assertable():
    handler_calls: list[tuple[dict, dict]] = []

    async def get_weather(arguments: dict, call_context: dict) -> str:
        handler_calls.append((arguments, call_context))
        return json.dumps({"forecast": "sunny", "city": arguments.get("city")})

    agent = _make_agent(
        tools=(
            {
                "name": "get_weather",
                "description": "Get the weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
                "handler": get_weather,
            },
        ),
    )
    provider = ScriptedLLMProvider(
        [
            tool_call_turn("get_weather", {"city": "Paris"}),
            text_turn("It is sunny in Paris today."),
        ]
    )

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        result = await s.user_says("what's the weather in paris?")

    # Chainable assertion helpers.
    expect(result).tool_called(
        "get_weather", args_subset={"city": "Paris"}
    ).agent_text_contains("sunny in Paris")

    # The REAL ToolExecutor invoked the local handler with the real
    # call_context (call_id / caller / callee threaded by the handler).
    assert len(handler_calls) == 1
    args, call_context = handler_calls[0]
    assert args == {"city": "Paris"}
    assert call_context["call_id"] == s.call_id
    assert call_context["caller"] == s.caller

    # The recorded ToolCallRecord carries the executor's result string.
    assert len(result.tool_calls) == 1
    record = result.tool_calls[0]
    assert record.name == "get_weather"
    assert json.loads(record.result) == {"forecast": "sunny", "city": "Paris"}

    # The tool result was fed back to the model: the provider's second call
    # contains the assistant tool_calls message + the role="tool" result.
    assert len(provider.calls) == 2
    second = provider.calls[1]["messages"]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "sunny" in tool_msgs[0]["content"]

    # The handler's tool-event path also surfaced role="tool" entries into
    # the conversation history (dashboard timeline parity).
    tool_history = [
        e for e in result.history_snapshot if e.get("role") == "tool"
    ]
    assert any("get_weather" in e["text"] for e in tool_history)

    # And the model saw the tool schema (not the handler) via the LLM loop.
    tools_sent = provider.calls[0]["tools"]
    assert tools_sent is not None
    assert tools_sent[0]["function"]["name"] == "get_weather"
    assert "handler" not in tools_sent[0]["function"]


# ---------------------------------------------------------------------------
# (b) Multi-turn — history accumulates through the real handler exactly once
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multi_turn_history_accumulates_without_duplicates():
    provider = ScriptedLLMProvider(
        [text_turn("First reply."), text_turn("Second reply.")]
    )
    agent = _make_agent()

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        r1 = await s.user_says("tell me a joke")
        r2 = await s.user_says("explain that joke")

    assert r1.agent_text == "First reply."
    assert r2.agent_text == "Second reply."

    # History snapshot after turn 2: both exchanges, in order, exactly once.
    roles_texts = [
        (e["role"], e["text"]) for e in r2.history_snapshot
    ]
    assert roles_texts == [
        ("user", "tell me a joke"),
        ("assistant", "First reply."),
        ("user", "explain that joke"),
        ("assistant", "Second reply."),
    ]

    # What the provider actually received on turn 2 — the history snapshot
    # handed to LLMLoop.run EXCLUDES the current user turn, and
    # LLMLoop._build_messages appends user_text exactly once, so every turn
    # (including the current one) reaches the provider exactly once and in
    # order. This is the cross-SDK contract; a regression re-introducing the
    # old trailing duplicate of the current user message must fail here.
    msgs = provider.calls[1]["messages"]
    assert msgs[0]["role"] == "system"
    assert [(m["role"], m["content"]) for m in msgs[1:]] == [
        ("user", "tell me a joke"),
        ("assistant", "First reply."),
        ("user", "explain that joke"),
    ]
    # No entry is ever duplicated.
    assert (
        sum(1 for m in msgs if m.get("content") == "tell me a joke") == 1
    )
    assert sum(1 for m in msgs if m.get("content") == "First reply.") == 1
    assert (
        sum(1 for m in msgs if m.get("content") == "explain that joke") == 1
    )


@pytest.mark.unit
async def test_first_message_flows_through_real_start_path():
    provider = ScriptedLLMProvider([text_turn("Sure thing.")])
    agent = _make_agent(first_message="Hello, thanks for calling!")

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        # The REAL start() path spoke the greeting through the fake TTS,
        # billed it, and pushed it into history.
        assert s.tts.spoken == ["Hello, thanks for calling!"]
        assert s.history[0]["role"] == "assistant"
        assert s.history[0]["text"] == "Hello, thanks for calling!"
        assert len(s.audio_sender.sent_audio) >= 1

        result = await s.user_says("hi, I need help")

    # The greeting is part of the LLM context on the first user turn.
    msgs = provider.calls[0]["messages"]
    assert ("assistant", "Hello, thanks for calling!") == (
        msgs[1]["role"],
        msgs[1]["content"],
    )
    assert result.history_snapshot[0]["text"] == "Hello, thanks for calling!"


# ---------------------------------------------------------------------------
# (c) Guardrail replacement is observable in agent_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_guardrail_replacement_observable_in_agent_text():
    provider = ScriptedLLMProvider(
        [text_turn("The secret password is swordfish.")]
    )
    agent = _make_agent(
        guardrails=(
            {
                "name": "no-secrets",
                "blocked_terms": ["swordfish"],
                "replacement": "I cannot share that information.",
            },
        ),
    )

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        result = await s.user_says("what is the secret password?")
        spoken = list(s.tts.spoken)

    # agent_text reflects what the caller HEARD: the guardrail replacement.
    expect(result).agent_text_contains("cannot share that information")
    assert "swordfish" not in result.agent_text
    # The raw sentence never reached TTS.
    assert spoken == ["I cannot share that information."]
    # Pipeline-streaming history keeps the RAW LLM text (existing handler
    # behaviour, mirrored from the live path) — documented on TurnResult.
    assistant_entries = [
        e for e in result.history_snapshot if e["role"] == "assistant"
    ]
    assert assistant_entries[-1]["text"] == "The secret password is swordfish."


# ---------------------------------------------------------------------------
# (d) cleanup() leaves no pending tasks
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cleanup_leaves_no_pending_tasks():
    tasks_before = set(asyncio.all_tasks())

    provider = ScriptedLLMProvider(
        [text_turn("Reply one."), text_turn("Reply two.")]
    )
    agent = _make_agent(first_message="Hi!")
    async with EvalSession(agent=agent, llm_provider=provider) as s:
        await s.user_says("first question")
        await s.user_says("second question")

    leaked = {
        t for t in asyncio.all_tasks() if not t.done()
    } - tasks_before
    assert leaked == set(), (
        f"EvalSession cleanup leaked pending tasks: {leaked!r}"
    )
    # The fakes were closed through the handler's real cleanup().
    assert s.stt.closed is True
    assert s.tts.closed is True


@pytest.mark.unit
async def test_cleanup_runs_when_body_raises():
    tasks_before = set(asyncio.all_tasks())
    provider = ScriptedLLMProvider([text_turn("ok")])
    agent = _make_agent()

    with pytest.raises(RuntimeError, match="boom"):
        async with EvalSession(agent=agent, llm_provider=provider) as s:
            raise RuntimeError("boom")

    assert s.stt.closed is True
    leaked = {t for t in asyncio.all_tasks() if not t.done()} - tasks_before
    assert leaked == set()


# ---------------------------------------------------------------------------
# Real commit-path filters apply (dedup / hallucination), loudly
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_duplicate_transcript_is_dropped_by_real_commit_filter():
    provider = ScriptedLLMProvider([text_turn("Once."), text_turn("Twice.")])
    agent = _make_agent()

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        await s.user_says("repeat after me")
        # The REAL ``_commit_transcript`` dedup throttle drops an identical
        # final within 2 s — exactly as on a live call. The harness surfaces
        # that as a loud, typed error instead of a silent no-op turn.
        with pytest.raises(PatterError) as excinfo:
            await s.user_says("repeat after me")
    assert excinfo.value.code == ErrorCode.INPUT_VALIDATION


@pytest.mark.unit
async def test_stt_hallucination_is_dropped_by_real_commit_filter():
    provider = ScriptedLLMProvider([text_turn("unused")])
    agent = _make_agent()

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        with pytest.raises(PatterError) as excinfo:
            await s.user_says("thank you for watching")
    assert excinfo.value.code == ErrorCode.INPUT_VALIDATION
    assert provider.calls == []  # never reached the LLM


# ---------------------------------------------------------------------------
# Metrics / transcript-event observability
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_metrics_turn_and_transcript_events_are_captured():
    provider = ScriptedLLMProvider([text_turn("All sorted.")])
    agent = _make_agent()

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        result = await s.user_says("sort it out")

    turn = result.metrics_turn
    assert turn is not None
    assert turn.user_text == "sort it out"
    assert turn.agent_text == "All sorted."
    assert turn.latency is not None
    assert result.interrupted is False

    # on_transcript fired for both roles through the real handler.
    roles = [e.get("role") for e in s.transcript_events]
    assert "user" in roles and "assistant" in roles
    user_events = [e for e in s.transcript_events if e["role"] == "user"]
    assert user_events[0]["text"] == "sort it out"
    assert user_events[0]["call_id"] == s.call_id


@pytest.mark.unit
async def test_hooks_run_on_the_real_path():
    """after_transcribe rewrites the user text before the LLM sees it —
    proof the real PipelineHookExecutor path is exercised."""
    from getpatter.models import PipelineHooks

    def redact(text: str, ctx) -> str:
        return text.replace("4111-1111", "[card]")

    provider = ScriptedLLMProvider([text_turn("Noted.")])
    agent = _make_agent(hooks=PipelineHooks(after_transcribe=redact))

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        result = await s.user_says("my card is 4111-1111")

    msgs = provider.calls[0]["messages"]
    user_contents = [m["content"] for m in msgs if m["role"] == "user"]
    assert all("[card]" in c for c in user_contents)
    assert all("4111-1111" not in c for c in user_contents)
    # History carries the redacted text too (what the LLM actually saw).
    assert result.history_snapshot[0]["text"] == "my card is [card]"


# ---------------------------------------------------------------------------
# Configuration errors are loud and typed
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_session_without_llm_raises_config_error():
    agent = _make_agent()
    with pytest.raises(PatterConfigError):
        async with EvalSession(agent=agent):
            pass  # pragma: no cover - never reached


@pytest.mark.unit
async def test_user_says_before_start_raises():
    agent = _make_agent()
    session = EvalSession(agent=agent, llm_provider=ScriptedLLMProvider())
    with pytest.raises(PatterError) as excinfo:
        await session.user_says("hello")
    assert excinfo.value.code == ErrorCode.CONFIG

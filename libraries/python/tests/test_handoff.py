"""Tests for MULTI-AGENT HANDOFF (built-in ``handoff_to`` tool).

Covers:
- ``build_handoff_tool`` schema (names surfaced as an enum).
- ``Patter.agent(handoffs={...})`` validation + Agent field.
- Pipeline mode: a handoff swaps the LLM loop's system prompt + tool list
  for the NEXT turn (fake LLM asserts the new system prompt).
- Realtime mode: a handoff sends the expected ``session.update`` payload via
  the adapter and ALWAYS sends a function result; an unknown handoff name
  returns an error envelope (never silence).
- Adapter ``update_session`` wire shape (v1-beta + GA).
"""

from __future__ import annotations

import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.services.llm_loop import DEFAULT_PHONE_PREAMBLE, LLMLoop
from getpatter.stream_handler import (
    END_CALL_TOOL,
    HANDOFF_TOOL_NAME,
    TRANSFER_CALL_TOOL,
    OpenAIRealtimeStreamHandler,
    PipelineStreamHandler,
    build_handoff_tool,
)
from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# build_handoff_tool — schema
# ---------------------------------------------------------------------------


def test_handoff_tool_schema():
    tool = build_handoff_tool(["billing", "support"])
    assert tool["name"] == HANDOFF_TOOL_NAME == "handoff_to"
    props = tool["parameters"]["properties"]
    assert props["name"]["enum"] == ["billing", "support"]
    assert "reason" in props
    assert tool["parameters"]["required"] == ["name"]
    assert "billing, support" in tool["description"]


def test_handoff_tool_names_sorted_deterministic():
    a = build_handoff_tool(["zeta", "alpha"])
    b = build_handoff_tool(["alpha", "zeta"])
    assert a == b
    assert a["parameters"]["properties"]["name"]["enum"] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# Agent factory — handoffs param
# ---------------------------------------------------------------------------


class TestAgentFactoryHandoffs:
    def _phone(self):
        from getpatter import Patter, Twilio

        return Patter(
            carrier=Twilio(account_sid="AC" + "0" * 32, auth_token="token"),
            phone_number="+15550000000",
            webhook_url="example.ngrok.io",
        )

    def _engine(self):
        from getpatter import OpenAIRealtime

        return OpenAIRealtime(api_key="sk-test")

    def test_handoffs_stored_on_agent(self):
        phone = self._phone()
        billing = phone.agent(
            system_prompt="You handle billing.", engine=self._engine()
        )
        triage = phone.agent(
            system_prompt="You triage calls.",
            engine=self._engine(),
            handoffs={"billing": billing},
        )
        assert triage.handoffs == {"billing": billing}
        # Default stays None — zero behavior change for existing agents.
        assert billing.handoffs is None

    def test_handoffs_must_be_dict(self):
        phone = self._phone()
        with pytest.raises(TypeError, match="handoffs must be a dict"):
            phone.agent(
                system_prompt="x", engine=self._engine(), handoffs=["billing"]  # type: ignore[arg-type]
            )

    def test_handoff_values_must_be_agents(self):
        phone = self._phone()
        with pytest.raises(TypeError, match="must be an Agent"):
            phone.agent(
                system_prompt="x",
                engine=self._engine(),
                handoffs={"billing": "not-an-agent"},  # type: ignore[dict-item]
            )

    def test_handoff_names_must_be_non_empty(self):
        phone = self._phone()
        billing = phone.agent(
            system_prompt="You handle billing.", engine=self._engine()
        )
        with pytest.raises(ValueError, match="non-empty strings"):
            phone.agent(
                system_prompt="x", engine=self._engine(), handoffs={"": billing}
            )


# ---------------------------------------------------------------------------
# LLMLoop.update_agent — next turn runs with the new prompt + tools
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """Fake LLMProvider that records the messages/tools of every stream call."""

    def __init__(self):
        self.calls: list[tuple[list[dict], list[dict] | None]] = []

    async def stream(self, messages, tools=None, *, cancel_event=None, call_id=None):
        self.calls.append(([dict(m) for m in messages], tools))
        yield {"type": "text", "content": "ok"}
        yield {"type": "done"}


@pytest.mark.asyncio
async def test_llm_loop_update_agent_swaps_prompt_and_tools_for_next_turn():
    provider = _RecordingProvider()
    loop = LLMLoop(
        openai_key="",
        model="fake",
        system_prompt="You are agent A.",
        tools=[{"name": "tool_a", "description": "", "parameters": {}}],
        llm_provider=provider,
    )

    async for _ in loop.run("hi", [], {"call_id": "c1"}):
        pass
    first_messages, first_tools = provider.calls[0]
    assert first_messages[0]["role"] == "system"
    assert "You are agent A." in first_messages[0]["content"]
    assert [t["function"]["name"] for t in first_tools] == ["tool_a"]

    loop.update_agent(
        system_prompt="You are agent B.",
        tools=[{"name": "tool_b", "description": "", "parameters": {}}],
    )

    async for _ in loop.run("hi again", [], {"call_id": "c1"}):
        pass
    second_messages, second_tools = provider.calls[1]
    assert "You are agent B." in second_messages[0]["content"]
    assert "You are agent A." not in second_messages[0]["content"]
    # Phone preamble still applied on the swapped prompt.
    assert second_messages[0]["content"].startswith(DEFAULT_PHONE_PREAMBLE)
    assert [t["function"]["name"] for t in second_tools] == ["tool_b"]


@pytest.mark.asyncio
async def test_llm_loop_update_agent_respects_disable_phone_preamble():
    provider = _RecordingProvider()
    loop = LLMLoop(
        openai_key="",
        model="fake",
        system_prompt="A",
        llm_provider=provider,
    )
    loop.update_agent(system_prompt="Verbatim.", disable_phone_preamble=True)
    async for _ in loop.run("hi", [], {"call_id": "c1"}):
        pass
    assert provider.calls[0][0][0]["content"] == "Verbatim."


@pytest.mark.asyncio
async def test_llm_loop_skips_system_history_entries():
    """Handoff markers (role=system) in conversation history must not be
    replayed as fabricated user turns."""
    provider = _RecordingProvider()
    loop = LLMLoop(openai_key="", model="fake", system_prompt="A", llm_provider=provider)
    history = [
        {"role": "user", "text": "hello"},
        {"role": "system", "text": "[handoff] Conversation handed to agent 'b'"},
        {"role": "assistant", "text": "hi there"},
    ]
    async for _ in loop.run("next", history, {"call_id": "c1"}):
        pass
    roles = [m["role"] for m in provider.calls[0][0]]
    assert roles == ["system", "user", "assistant", "user"]
    assert all("[handoff]" not in (m.get("content") or "") for m in provider.calls[0][0])


# ---------------------------------------------------------------------------
# Pipeline handler — _perform_handoff
# ---------------------------------------------------------------------------


def _make_pipeline_handler(agent):
    return PipelineStreamHandler(
        agent=agent,
        audio_sender=MagicMock(),
        call_id="CA" + "a" * 32,
        caller="+15551234567",
        callee="+15559876543",
        resolved_prompt=agent.system_prompt,
        metrics=None,
    )


@pytest.mark.asyncio
async def test_pipeline_handoff_swaps_prompt_and_tools():
    billing_tool = {"name": "lookup_invoice", "description": "", "parameters": {}}
    billing = make_agent(
        system_prompt="You are the billing specialist.",
        tools=(billing_tool,),
        first_message="",
    )
    triage = make_agent(
        system_prompt="You are the triage agent.",
        handoffs={"billing": billing},
        first_message="",
    )
    handler = _make_pipeline_handler(triage)
    llm_loop = MagicMock()
    handler._llm_loop = llm_loop
    handler._transfer_fn = AsyncMock()
    handler._hangup_fn = AsyncMock()

    result = json.loads(await handler._perform_handoff("billing", "billing question"))

    assert result == {"status": "handed_off", "to": "billing"}
    # Agent reference swapped (frozen dataclass — replaced, not mutated).
    assert handler.agent.system_prompt == "You are the billing specialist."
    assert triage.system_prompt == "You are the triage agent."  # original untouched
    # LLM loop got the new prompt + rebuilt tool list for the NEXT turn.
    llm_loop.update_agent.assert_called_once()
    kwargs = llm_loop.update_agent.call_args.kwargs
    assert kwargs["system_prompt"] == "You are the billing specialist."
    tool_names = [t["name"] for t in kwargs["tools"]]
    assert tool_names == ["lookup_invoice", "transfer_call", "end_call"]
    # History records the handoff as a system-style entry.
    assert any(
        e["role"] == "system" and "handed to agent 'billing'" in e["text"]
        for e in handler.conversation_history
    )


@pytest.mark.asyncio
async def test_pipeline_handoff_chains_follow_target_map():
    """The target's own handoffs map governs onward handoffs — the rebuilt
    tool list advertises handoff_to again with the target's names."""
    support = make_agent(system_prompt="Support.", first_message="")
    billing = make_agent(
        system_prompt="Billing.", handoffs={"support": support}, first_message=""
    )
    triage = make_agent(
        system_prompt="Triage.", handoffs={"billing": billing}, first_message=""
    )
    handler = _make_pipeline_handler(triage)
    llm_loop = MagicMock()
    handler._llm_loop = llm_loop
    handler._transfer_fn = AsyncMock()
    handler._hangup_fn = AsyncMock()

    await handler._perform_handoff("billing", "")

    tools = llm_loop.update_agent.call_args.kwargs["tools"]
    handoff_tools = [t for t in tools if t["name"] == HANDOFF_TOOL_NAME]
    assert len(handoff_tools) == 1
    assert handoff_tools[0]["parameters"]["properties"]["name"]["enum"] == ["support"]


@pytest.mark.asyncio
async def test_pipeline_handoff_unknown_name_returns_error_envelope():
    billing = make_agent(system_prompt="Billing.", first_message="")
    triage = make_agent(
        system_prompt="Triage.", handoffs={"billing": billing}, first_message=""
    )
    handler = _make_pipeline_handler(triage)
    llm_loop = MagicMock()
    handler._llm_loop = llm_loop

    result = json.loads(await handler._perform_handoff("sales", ""))

    assert "Unknown handoff agent 'sales'" in result["error"]
    assert result["available"] == ["billing"]
    llm_loop.update_agent.assert_not_called()
    # No swap, no history entry.
    assert handler.agent.system_prompt == "Triage."
    assert not any(e["role"] == "system" for e in handler.conversation_history)


@pytest.mark.asyncio
async def test_pipeline_handoff_tool_injected_into_combined_tools():
    billing = make_agent(system_prompt="Billing.", first_message="")
    triage = make_agent(
        system_prompt="Triage.", handoffs={"billing": billing}, first_message=""
    )
    handler = _make_pipeline_handler(triage)
    handler._transfer_fn = AsyncMock()
    handler._hangup_fn = AsyncMock()

    tools = handler._build_combined_pipeline_tools()
    names = [t["name"] for t in tools]
    assert names == ["transfer_call", "end_call", HANDOFF_TOOL_NAME]
    handoff_tool = tools[-1]
    assert callable(handoff_tool["handler"])
    # The handler closure dispatches into _perform_handoff (error envelope
    # proves the round trip — no LLM loop is wired in this minimal handler).
    result = json.loads(await handoff_tool["handler"]({"name": "nope"}, {}))
    assert "Unknown handoff agent" in result["error"]


# ---------------------------------------------------------------------------
# Realtime handler — _handle_handoff_function_call
# ---------------------------------------------------------------------------


def _make_realtime_handler(agent):
    handler = OpenAIRealtimeStreamHandler(
        agent=agent,
        audio_sender=MagicMock(),
        call_id="CA" + "a" * 32,
        caller="+15551234567",
        callee="+15559876543",
        resolved_prompt=agent.system_prompt,
        metrics=None,
        openai_key="sk-test",
    )
    handler._adapter = AsyncMock()
    return handler


@pytest.mark.asyncio
async def test_realtime_handoff_sends_session_update_then_function_result():
    billing_tool = {"name": "lookup_invoice", "description": "d", "parameters": {}}
    billing = make_agent(
        system_prompt="You are the billing specialist.",
        provider="openai_realtime",
        tools=(billing_tool,),
        first_message="",
        voice="echo",
    )
    triage = make_agent(
        system_prompt="You are the triage agent.",
        provider="openai_realtime",
        handoffs={"billing": billing},
        first_message="",
        voice="alloy",
    )
    handler = _make_realtime_handler(triage)
    adapter = handler._adapter

    await handler._handle_handoff_function_call(
        {
            "call_id": "fc-1",
            "name": HANDOFF_TOOL_NAME,
            "arguments": json.dumps({"name": "billing", "reason": "invoice question"}),
        }
    )

    # session.update carries the target's prompt + rebuilt tool surface.
    adapter.update_session.assert_awaited_once()
    kwargs = adapter.update_session.await_args.kwargs
    assert kwargs["instructions"] == "You are the billing specialist."
    tool_names = [t["name"] for t in kwargs["tools"]]
    assert tool_names == ["lookup_invoice", "transfer_call", "end_call"]

    # A function result is ALWAYS sent — success envelope here.
    adapter.send_function_result.assert_awaited_once()
    fc_call_id, result_str = adapter.send_function_result.await_args.args
    assert fc_call_id == "fc-1"
    assert json.loads(result_str) == {"status": "handed_off", "to": "billing"}

    # Handler agent reference swapped; voice intentionally unchanged.
    assert handler.agent.system_prompt == "You are the billing specialist."
    assert handler.agent.voice == "alloy"

    # History records the handoff with the reason.
    assert any(
        e["role"] == "system"
        and "handed to agent 'billing'" in e["text"]
        and "invoice question" in e["text"]
        for e in handler.conversation_history
    )


@pytest.mark.asyncio
async def test_realtime_handoff_session_update_precedes_function_result():
    billing = make_agent(
        system_prompt="Billing.", provider="openai_realtime", first_message=""
    )
    triage = make_agent(
        system_prompt="Triage.",
        provider="openai_realtime",
        handoffs={"billing": billing},
        first_message="",
    )
    handler = _make_realtime_handler(triage)
    order: list[str] = []
    handler._adapter.update_session.side_effect = lambda **kw: order.append("update")
    handler._adapter.send_function_result.side_effect = lambda *a: order.append(
        "result"
    )

    await handler._handle_handoff_function_call(
        {
            "call_id": "fc-1",
            "name": HANDOFF_TOOL_NAME,
            "arguments": json.dumps({"name": "billing"}),
        }
    )
    # The function result triggers the model's next response, which must
    # already run under the new instructions.
    assert order == ["update", "result"]


@pytest.mark.asyncio
async def test_realtime_handoff_unknown_name_sends_error_envelope():
    billing = make_agent(
        system_prompt="Billing.", provider="openai_realtime", first_message=""
    )
    triage = make_agent(
        system_prompt="Triage.",
        provider="openai_realtime",
        handoffs={"billing": billing},
        first_message="",
    )
    handler = _make_realtime_handler(triage)
    adapter = handler._adapter

    await handler._handle_handoff_function_call(
        {
            "call_id": "fc-2",
            "name": HANDOFF_TOOL_NAME,
            "arguments": json.dumps({"name": "sales"}),
        }
    )

    adapter.update_session.assert_not_awaited()
    adapter.send_function_result.assert_awaited_once()
    _, result_str = adapter.send_function_result.await_args.args
    result = json.loads(result_str)
    assert "Unknown handoff agent 'sales'" in result["error"]
    assert result["available"] == ["billing"]
    assert handler.agent.system_prompt == "Triage."


@pytest.mark.asyncio
async def test_realtime_handoff_malformed_args_sends_error_envelope():
    billing = make_agent(
        system_prompt="Billing.", provider="openai_realtime", first_message=""
    )
    triage = make_agent(
        system_prompt="Triage.",
        provider="openai_realtime",
        handoffs={"billing": billing},
        first_message="",
    )
    handler = _make_realtime_handler(triage)
    adapter = handler._adapter

    await handler._handle_handoff_function_call(
        {"call_id": "fc-3", "name": HANDOFF_TOOL_NAME, "arguments": "{not json"}
    )

    adapter.send_function_result.assert_awaited_once()
    _, result_str = adapter.send_function_result.await_args.args
    assert "error" in json.loads(result_str)


@pytest.mark.asyncio
async def test_realtime_handoff_chained_targets_advertised():
    support = make_agent(
        system_prompt="Support.", provider="openai_realtime", first_message=""
    )
    billing = make_agent(
        system_prompt="Billing.",
        provider="openai_realtime",
        handoffs={"support": support},
        first_message="",
    )
    triage = make_agent(
        system_prompt="Triage.",
        provider="openai_realtime",
        handoffs={"billing": billing},
        first_message="",
    )
    handler = _make_realtime_handler(triage)

    await handler._handle_handoff_function_call(
        {
            "call_id": "fc-4",
            "name": HANDOFF_TOOL_NAME,
            "arguments": json.dumps({"name": "billing"}),
        }
    )
    tools = handler._adapter.update_session.await_args.kwargs["tools"]
    handoff_tools = [t for t in tools if t["name"] == HANDOFF_TOOL_NAME]
    assert len(handoff_tools) == 1
    assert handoff_tools[0]["parameters"]["properties"]["name"]["enum"] == ["support"]


# ---------------------------------------------------------------------------
# Adapter update_session — wire shape (v1-beta + GA)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v1_adapter_update_session_wire_shape():
    from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

    adapter = OpenAIRealtimeAdapter(api_key="sk-test", instructions="old")
    ws = AsyncMock()
    adapter._ws = ws

    await adapter.update_session(
        instructions="new instructions",
        tools=[TRANSFER_CALL_TOOL, END_CALL_TOOL],
    )

    ws.send.assert_awaited_once()
    sent = json.loads(ws.send.await_args.args[0])
    assert sent["type"] == "session.update"
    session = sent["session"]
    assert "type" not in session  # v1-beta shape — no discriminator
    assert session["instructions"] == "new instructions"
    assert [t["name"] for t in session["tools"]] == ["transfer_call", "end_call"]
    assert all(t["type"] == "function" for t in session["tools"])
    # Adapter state updated so reconnect paths rebuild with the new config.
    assert adapter.instructions == "new instructions"


@pytest.mark.asyncio
async def test_ga_adapter_update_session_includes_realtime_type():
    from getpatter.providers.openai_realtime_2 import OpenAIRealtime2Adapter

    adapter = OpenAIRealtime2Adapter(api_key="sk-test", instructions="old")
    ws = AsyncMock()
    adapter._ws = ws

    await adapter.update_session(instructions="new instructions")

    sent = json.loads(ws.send.await_args.args[0])
    assert sent["type"] == "session.update"
    assert sent["session"]["type"] == "realtime"  # GA requires the discriminator
    assert sent["session"]["instructions"] == "new instructions"
    assert "tools" not in sent["session"]  # partial update — only what changed


@pytest.mark.asyncio
async def test_update_session_noop_without_fields_or_ws():
    from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

    adapter = OpenAIRealtimeAdapter(api_key="sk-test")
    ws = AsyncMock()
    adapter._ws = ws
    await adapter.update_session()
    ws.send.assert_not_awaited()

    adapter._ws = None
    # No socket — state still updated, no crash.
    await adapter.update_session(instructions="x")
    assert adapter.instructions == "x"


# ---------------------------------------------------------------------------
# Realtime start() advertises handoff_to
# ---------------------------------------------------------------------------


def test_realtime_handler_dispatch_requires_configured_handoffs():
    """A user-defined tool named handoff_to on an agent WITHOUT handoffs must
    fall through to the generic dispatch — the built-in branch is guarded by
    ``agent.handoffs``."""
    agent = make_agent(provider="openai_realtime", first_message="")
    handler = _make_realtime_handler(agent)
    assert getattr(handler.agent, "handoffs", None) is None


def test_handoff_history_text_format():
    from getpatter.stream_handler import _handoff_history_text

    assert (
        _handoff_history_text("billing", "")
        == "[handoff] Conversation handed to agent 'billing'"
    )
    assert (
        _handoff_history_text("billing", "invoice")
        == "[handoff] Conversation handed to agent 'billing' — invoice"
    )

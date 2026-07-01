"""Tests for the opt-in ``transfer_call`` DESTINATION POLICY (allowlist).

``transfer_call``'s ``number`` argument is chosen by the LLM, which is driven
by caller speech — a prompt-injected caller can steer the agent into dialing
an arbitrary (premium-rate / international) E.164 number billed to the
operator. The destination policy is the deterministic defense-in-depth
control: an exact-number allowlist and/or a prefix allowlist enforced at
every transfer guard site (pipeline built-in handler, OpenAI Realtime
``function_call`` path, ElevenLabs ConvAI client-tool path) BEFORE any
carrier REST call. No policy configured (default) keeps today's format-only
E.164 gate byte-identical.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.stream_handler import (
    ElevenLabsConvAIStreamHandler,
    OpenAIRealtimeStreamHandler,
    _augment_with_builtin_handoff_tools,
)
from getpatter.telephony.common import _transfer_destination_allowed
from tests.conftest import make_agent

POLICY_REJECTION = {
    "error": "Transfer destination not allowed by policy",
    "status": "rejected",
}
ALLOWED = "+15551230000"
DENIED = "+19005551234"  # premium-rate-looking number NOT in any policy below


# ---------------------------------------------------------------------------
# _transfer_destination_allowed — pure policy semantics
# ---------------------------------------------------------------------------


class TestDestinationAllowedHelper:
    def test_no_policy_allows_any_destination(self):
        assert _transfer_destination_allowed(DENIED, None, None)

    def test_exact_number_allowlist(self):
        assert _transfer_destination_allowed(ALLOWED, (ALLOWED,), None)
        assert not _transfer_destination_allowed("+15551230001", (ALLOWED,), None)

    def test_prefix_allowlist(self):
        prefixes = ("+1415", "+44")
        assert _transfer_destination_allowed("+14155551234", None, prefixes)
        assert _transfer_destination_allowed("+442071234567", None, prefixes)
        assert not _transfer_destination_allowed(DENIED, None, prefixes)

    def test_union_semantics_number_or_prefix_passes(self):
        numbers, prefixes = (ALLOWED,), ("+44",)
        assert _transfer_destination_allowed(ALLOWED, numbers, prefixes)
        assert _transfer_destination_allowed("+442071234567", numbers, prefixes)
        assert not _transfer_destination_allowed(DENIED, numbers, prefixes)

    def test_empty_policy_denies_all(self):
        # Configured-but-empty = explicit lockdown, distinct from None.
        assert not _transfer_destination_allowed(ALLOWED, (), None)
        assert not _transfer_destination_allowed(ALLOWED, None, ())


# ---------------------------------------------------------------------------
# Patter.agent() factory — validation + normalization
# ---------------------------------------------------------------------------


class TestAgentFactoryTransferPolicy:
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

    def test_fields_default_none_backward_compat(self):
        agent = self._phone().agent(system_prompt="hi", engine=self._engine())
        assert agent.transfer_allowed_numbers is None
        assert agent.transfer_allowed_prefixes is None

    def test_lists_normalized_to_tuples(self):
        agent = self._phone().agent(
            system_prompt="hi",
            engine=self._engine(),
            transfer_allowed_numbers=[ALLOWED],
            transfer_allowed_prefixes=["+1"],
        )
        assert agent.transfer_allowed_numbers == (ALLOWED,)
        assert agent.transfer_allowed_prefixes == ("+1",)

    def test_invalid_allowlist_number_rejected(self):
        with pytest.raises(ValueError, match="transfer_allowed_numbers"):
            self._phone().agent(
                system_prompt="hi",
                engine=self._engine(),
                transfer_allowed_numbers=["555-1234"],
            )

    def test_invalid_prefix_rejected(self):
        with pytest.raises(ValueError, match="transfer_allowed_prefixes"):
            self._phone().agent(
                system_prompt="hi",
                engine=self._engine(),
                transfer_allowed_prefixes=["1415"],  # missing leading '+'
            )
        with pytest.raises(ValueError, match="transfer_allowed_prefixes"):
            self._phone().agent(
                system_prompt="hi",
                engine=self._engine(),
                transfer_allowed_prefixes=["+"],  # no digits
            )


# ---------------------------------------------------------------------------
# Pipeline mode — built-in transfer handler enforces the policy
# ---------------------------------------------------------------------------


class TestPipelineBuiltinHandlerPolicy:
    def _tools(self, transfer_fn, **policy):
        return _augment_with_builtin_handoff_tools(
            None, transfer_fn=transfer_fn, hangup_fn=None, **policy
        )

    async def test_denied_destination_rejected_before_carrier(self):
        calls: list[str] = []

        async def transfer_fn(number: str) -> None:
            calls.append(number)

        tools = self._tools(transfer_fn, transfer_allowed_numbers=(ALLOWED,))
        handler = tools[0]["handler"]
        result = json.loads(await handler({"number": DENIED, "mode": "cold"}, {}))
        assert result == POLICY_REJECTION
        assert calls == []

    async def test_allowed_destination_transfers(self):
        calls: list[str] = []

        async def transfer_fn(number: str) -> None:
            calls.append(number)

        tools = self._tools(transfer_fn, transfer_allowed_numbers=(ALLOWED,))
        handler = tools[0]["handler"]
        result = await handler({"number": ALLOWED, "mode": "cold"}, {})
        assert result == f"Transferring to {ALLOWED}"
        assert calls == [ALLOWED]

    async def test_warm_mode_also_gated(self):
        transfer_fn = AsyncMock()
        tools = self._tools(transfer_fn, transfer_allowed_prefixes=("+1415",))
        handler = tools[0]["handler"]
        result = json.loads(await handler({"number": DENIED, "mode": "warm"}, {}))
        assert result == POLICY_REJECTION
        transfer_fn.assert_not_awaited()

    async def test_no_policy_keeps_existing_behaviour(self):
        calls: list[str] = []

        async def transfer_fn(number: str) -> None:
            calls.append(number)

        tools = self._tools(transfer_fn)
        handler = tools[0]["handler"]
        result = await handler({"number": DENIED, "mode": "cold"}, {})
        assert result == f"Transferring to {DENIED}"
        assert calls == [DENIED]


# ---------------------------------------------------------------------------
# OpenAI Realtime mode — function_call guard site enforces the policy
# ---------------------------------------------------------------------------


def _make_realtime_handler(agent) -> OpenAIRealtimeStreamHandler:
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


def _single_event_stream(event):
    async def _events():
        yield event

    return _events


class TestRealtimeFunctionCallPolicy:
    async def test_realtime_transfer_denied_by_policy(self):
        agent = make_agent(
            provider="openai_realtime",
            transfer_allowed_numbers=(ALLOWED,),
        )
        handler = _make_realtime_handler(agent)
        handler._transfer_fn = AsyncMock()
        handler._emit_tool_event = AsyncMock()
        handler._adapter.receive_events = _single_event_stream(
            (
                "function_call",
                {
                    "name": "transfer_call",
                    "call_id": "fc-1",
                    "arguments": json.dumps({"number": DENIED}),
                },
            )
        )

        await handler._forward_events()

        handler._transfer_fn.assert_not_awaited()
        handler._adapter.send_function_result.assert_awaited_once()
        args = handler._adapter.send_function_result.await_args.args
        assert args[0] == "fc-1"
        assert json.loads(args[1]) == POLICY_REJECTION

    async def test_realtime_transfer_allowed_by_prefix(self):
        agent = make_agent(
            provider="openai_realtime",
            transfer_allowed_prefixes=("+1555",),
        )
        handler = _make_realtime_handler(agent)
        handler._transfer_fn = AsyncMock()
        handler._emit_tool_event = AsyncMock()
        handler._adapter.receive_events = _single_event_stream(
            (
                "function_call",
                {
                    "name": "transfer_call",
                    "call_id": "fc-2",
                    "arguments": json.dumps({"number": ALLOWED}),
                },
            )
        )

        await handler._forward_events()

        handler._transfer_fn.assert_awaited_once_with(ALLOWED)
        args = handler._adapter.send_function_result.await_args.args
        assert json.loads(args[1]) == {"status": "transferring", "to": ALLOWED}


# ---------------------------------------------------------------------------
# ElevenLabs ConvAI mode — client-tool guard site enforces the policy
# ---------------------------------------------------------------------------


def _make_convai_handler(agent) -> ElevenLabsConvAIStreamHandler:
    handler = ElevenLabsConvAIStreamHandler(
        agent=agent,
        audio_sender=MagicMock(),
        call_id="CA" + "b" * 32,
        caller="+15551234567",
        callee="+15559876543",
        resolved_prompt=agent.system_prompt,
        metrics=None,
        elevenlabs_key="el-test",
    )
    handler._adapter = AsyncMock()
    return handler


class TestConvAIClientToolPolicy:
    async def test_convai_transfer_denied_by_policy(self):
        agent = make_agent(
            provider="elevenlabs_convai",
            transfer_allowed_numbers=(ALLOWED,),
        )
        handler = _make_convai_handler(agent)
        handler._transfer_fn = AsyncMock()

        await handler._handle_convai_client_tool(
            {
                "call_id": "ct-1",
                "name": "transfer_call",
                "arguments": {"number": DENIED},
            }
        )

        handler._transfer_fn.assert_not_awaited()
        handler._adapter.send_client_tool_result.assert_awaited_once()
        call = handler._adapter.send_client_tool_result.await_args
        assert call.args[0] == "ct-1"
        assert json.loads(call.args[1]) == POLICY_REJECTION
        assert call.kwargs.get("is_error") is True

    async def test_convai_transfer_allowed_by_policy(self):
        agent = make_agent(
            provider="elevenlabs_convai",
            transfer_allowed_numbers=(ALLOWED,),
        )
        handler = _make_convai_handler(agent)
        handler._transfer_fn = AsyncMock()

        await handler._handle_convai_client_tool(
            {
                "call_id": "ct-2",
                "name": "transfer_call",
                "arguments": {"number": ALLOWED},
            }
        )

        handler._transfer_fn.assert_awaited_once_with(ALLOWED)
        call = handler._adapter.send_client_tool_result.await_args
        assert call.args[1] == f"Transferring to {ALLOWED}"

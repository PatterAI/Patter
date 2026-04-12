"""Unit tests for patter.handlers.stream_handler — shared helpers, base class, guardrails."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.handlers.stream_handler import (
    AudioSender,
    END_CALL_TOOL,
    StreamHandler,
    TRANSFER_CALL_TOOL,
    apply_call_overrides,
    create_metrics_accumulator,
    evaluate_guardrails,
    resolve_agent_prompt,
)
from patter.models import Agent, STTConfig, TTSConfig

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# resolve_agent_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveAgentPrompt:
    """resolve_agent_prompt substitutes variables in the system prompt."""

    def test_no_variables(self) -> None:
        agent = make_agent(system_prompt="Hello world")
        assert resolve_agent_prompt(agent) == "Hello world"

    def test_with_variables(self) -> None:
        agent = make_agent(
            system_prompt="Hello {name}, you are {role}.",
            variables={"name": "Alice", "role": "helpful"},
        )
        assert resolve_agent_prompt(agent) == "Hello Alice, you are helpful."

    def test_with_custom_params_override(self) -> None:
        agent = make_agent(
            system_prompt="Hello {name}",
            variables={"name": "default"},
        )
        result = resolve_agent_prompt(agent, custom_params={"name": "override"})
        assert result == "Hello override"

    def test_custom_params_sanitized(self) -> None:
        """Control characters are stripped from custom param values."""
        agent = make_agent(system_prompt="Hello {name}")
        result = resolve_agent_prompt(agent, custom_params={"name": "bad\x00val"})
        assert "\x00" not in result
        assert "badval" in result


# ---------------------------------------------------------------------------
# apply_call_overrides
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyCallOverrides:
    """apply_call_overrides returns a new Agent with per-call config."""

    def test_override_simple_fields(self) -> None:
        agent = make_agent(voice="alloy", language="en")
        updated = apply_call_overrides(agent, {"voice": "echo", "language": "it"})
        assert updated.voice == "echo"
        assert updated.language == "it"
        # Original unchanged
        assert agent.voice == "alloy"

    def test_override_stt_config(self) -> None:
        agent = make_agent()
        updated = apply_call_overrides(
            agent,
            {"stt_config": {"provider": "deepgram", "api_key": "k", "language": "en"}},
        )
        assert updated.stt is not None
        assert updated.stt.provider == "deepgram"

    def test_override_tts_config(self) -> None:
        agent = make_agent()
        updated = apply_call_overrides(
            agent,
            {"tts_config": {"provider": "elevenlabs", "api_key": "k", "voice": "rachel"}},
        )
        assert updated.tts is not None
        assert updated.tts.provider == "elevenlabs"

    def test_no_overrides_returns_same(self) -> None:
        agent = make_agent()
        updated = apply_call_overrides(agent, {})
        assert updated.system_prompt == agent.system_prompt


# ---------------------------------------------------------------------------
# evaluate_guardrails
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluateGuardrails:
    """evaluate_guardrails — output guardrail filtering."""

    def test_no_guardrails(self) -> None:
        agent = make_agent(guardrails=None)
        blocked, name = evaluate_guardrails(agent, "anything")
        assert not blocked
        assert name == ""

    def test_blocked_terms_match(self) -> None:
        agent = make_agent(
            guardrails=[
                {"name": "medical", "blocked_terms": ["diagnosis", "prescription"], "check": None, "replacement": "See a doctor."}
            ]
        )
        blocked, name = evaluate_guardrails(agent, "I recommend a diagnosis")
        assert blocked
        assert name == "medical"

    def test_blocked_terms_case_insensitive(self) -> None:
        agent = make_agent(
            guardrails=[
                {"name": "profanity", "blocked_terms": ["BadWord"], "check": None, "replacement": "No."}
            ]
        )
        blocked, _ = evaluate_guardrails(agent, "this has badword in it")
        assert blocked

    def test_check_function(self) -> None:
        agent = make_agent(
            guardrails=[
                {"name": "custom", "blocked_terms": None, "check": lambda t: "secret" in t.lower(), "replacement": "Nope."}
            ]
        )
        blocked, _ = evaluate_guardrails(agent, "This is a SECRET message")
        assert blocked

    def test_check_not_called_when_already_blocked(self) -> None:
        """If blocked_terms already match, check fn is not evaluated."""
        check_fn = MagicMock(return_value=False)
        agent = make_agent(
            guardrails=[
                {"name": "combo", "blocked_terms": ["bad"], "check": check_fn, "replacement": "Blocked."}
            ]
        )
        blocked, _ = evaluate_guardrails(agent, "this is bad")
        assert blocked
        check_fn.assert_not_called()

    def test_unblocked_response(self) -> None:
        agent = make_agent(
            guardrails=[
                {"name": "g1", "blocked_terms": ["forbidden"], "check": None, "replacement": "No."}
            ]
        )
        blocked, _ = evaluate_guardrails(agent, "this is perfectly fine")
        assert not blocked

    def test_check_exception_handled(self) -> None:
        """A failing check function does not crash — treated as not blocked."""
        agent = make_agent(
            guardrails=[
                {"name": "buggy", "blocked_terms": None, "check": lambda t: 1/0, "replacement": "Oops."}
            ]
        )
        blocked, _ = evaluate_guardrails(agent, "trigger")
        assert not blocked


# ---------------------------------------------------------------------------
# create_metrics_accumulator
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateMetricsAccumulator:
    """create_metrics_accumulator factory function."""

    def test_pipeline_mode(self) -> None:
        agent = make_agent(provider="pipeline")
        m = create_metrics_accumulator(
            call_id="c1",
            provider="pipeline",
            telephony_provider="twilio",
            agent=agent,
            deepgram_key="dg_key",
            elevenlabs_key="el_key",
            pricing=None,
        )
        assert m.call_id == "c1"
        assert m.provider_mode == "pipeline"
        assert m.telephony_provider == "twilio"
        assert m.stt_provider == "deepgram"
        assert m.tts_provider == "elevenlabs"

    def test_openai_realtime_mode(self) -> None:
        agent = make_agent(provider="openai_realtime")
        m = create_metrics_accumulator(
            call_id="c2",
            provider="openai_realtime",
            telephony_provider="telnyx",
            agent=agent,
            deepgram_key="",
            elevenlabs_key="",
            pricing=None,
        )
        assert m.stt_provider == "openai"
        assert m.tts_provider == "openai"
        assert m.llm_provider == "openai"

    def test_elevenlabs_convai_mode(self) -> None:
        agent = make_agent(provider="elevenlabs_convai")
        m = create_metrics_accumulator(
            call_id="c3",
            provider="elevenlabs_convai",
            telephony_provider="twilio",
            agent=agent,
            deepgram_key="",
            elevenlabs_key="el_key",
            pricing=None,
        )
        assert m.stt_provider == "elevenlabs"
        assert m.tts_provider == "elevenlabs"
        assert m.llm_provider == "elevenlabs"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestToolDefinitions:
    """Shared tool definitions (TRANSFER_CALL_TOOL, END_CALL_TOOL)."""

    def test_transfer_call_tool_shape(self) -> None:
        assert TRANSFER_CALL_TOOL["name"] == "transfer_call"
        assert "number" in TRANSFER_CALL_TOOL["parameters"]["properties"]
        assert "number" in TRANSFER_CALL_TOOL["parameters"]["required"]

    def test_end_call_tool_shape(self) -> None:
        assert END_CALL_TOOL["name"] == "end_call"
        assert "reason" in END_CALL_TOOL["parameters"]["properties"]


# ---------------------------------------------------------------------------
# AudioSender ABC
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAudioSenderABC:
    """AudioSender cannot be instantiated directly."""

    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            AudioSender()


# ---------------------------------------------------------------------------
# Concurrent StreamHandler instances share no state
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStreamHandlerIsolation:
    """Multiple StreamHandler instances do not share mutable state."""

    def test_independent_conversation_history(self) -> None:
        """Two handlers with separate deques do not cross-contaminate."""

        class _ConcreteHandler(StreamHandler):
            async def start(self): pass
            async def on_audio_received(self, audio_bytes): pass
            async def cleanup(self): pass

        agent = make_agent()
        sender = AsyncMock(spec=AudioSender)
        metrics = MagicMock()

        h1 = _ConcreteHandler(
            agent=agent, audio_sender=sender, call_id="c1",
            caller="+1", callee="+2", resolved_prompt="p1", metrics=metrics,
        )
        h2 = _ConcreteHandler(
            agent=agent, audio_sender=sender, call_id="c2",
            caller="+3", callee="+4", resolved_prompt="p2", metrics=metrics,
        )

        h1.conversation_history.append({"role": "user", "text": "hello from h1"})
        assert len(h2.conversation_history) == 0

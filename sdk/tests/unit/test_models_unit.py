"""Unit tests for patter.models — dataclasses and CallControl."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from patter.models import (
    Agent,
    CallControl,
    CallEvent,
    CallMetrics,
    CostBreakdown,
    Guardrail,
    IncomingMessage,
    LatencyBreakdown,
    STTConfig,
    TTSConfig,
    TurnMetrics,
)


# ---------------------------------------------------------------------------
# Frozen dataclass basics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDataclasses:
    """All model dataclasses are frozen (immutable)."""

    def test_agent_frozen(self) -> None:
        agent = Agent(system_prompt="Hi")
        with pytest.raises(AttributeError):
            agent.system_prompt = "Changed"

    def test_guardrail_frozen(self) -> None:
        g = Guardrail(name="g1")
        with pytest.raises(AttributeError):
            g.name = "changed"

    def test_incoming_message_frozen(self) -> None:
        msg = IncomingMessage(text="hi", call_id="c1", caller="+1")
        with pytest.raises(AttributeError):
            msg.text = "changed"

    def test_stt_config_frozen(self) -> None:
        cfg = STTConfig(provider="deepgram", api_key="k")
        with pytest.raises(AttributeError):
            cfg.provider = "whisper"

    def test_tts_config_frozen(self) -> None:
        cfg = TTSConfig(provider="elevenlabs", api_key="k")
        with pytest.raises(AttributeError):
            cfg.provider = "openai"

    def test_cost_breakdown_frozen(self) -> None:
        c = CostBreakdown(stt=0.1)
        with pytest.raises(AttributeError):
            c.stt = 0.2

    def test_latency_breakdown_frozen(self) -> None:
        lb = LatencyBreakdown(stt_ms=10.0)
        with pytest.raises(AttributeError):
            lb.stt_ms = 20.0

    def test_call_event_frozen(self) -> None:
        ev = CallEvent(call_id="c1")
        with pytest.raises(AttributeError):
            ev.call_id = "c2"


# ---------------------------------------------------------------------------
# Agent defaults and field values
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAgentDefaults:
    """Agent field defaults and custom values."""

    def test_defaults(self) -> None:
        agent = Agent(system_prompt="You help.")
        assert agent.voice == "alloy"
        assert agent.model == "gpt-4o-mini-realtime-preview"
        assert agent.language == "en"
        assert agent.first_message == ""
        assert agent.tools is None
        assert agent.provider == "openai_realtime"
        assert agent.stt is None
        assert agent.tts is None
        assert agent.variables is None
        assert agent.guardrails is None

    def test_custom_values(self) -> None:
        agent = Agent(
            system_prompt="Custom",
            voice="echo",
            model="gpt-4o",
            language="it",
            first_message="Ciao!",
            provider="pipeline",
            variables={"name": "World"},
        )
        assert agent.voice == "echo"
        assert agent.language == "it"
        assert agent.first_message == "Ciao!"
        assert agent.variables == {"name": "World"}


# ---------------------------------------------------------------------------
# STTConfig / TTSConfig serialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigSerialization:
    """to_dict() round-trip for STTConfig and TTSConfig."""

    def test_stt_to_dict(self) -> None:
        cfg = STTConfig(provider="deepgram", api_key="dg_key", language="fr")
        d = cfg.to_dict()
        assert d == {"provider": "deepgram", "api_key": "dg_key", "language": "fr"}

    def test_stt_roundtrip(self) -> None:
        original = STTConfig(provider="whisper", api_key="w_key", language="en")
        d = original.to_dict()
        reconstructed = STTConfig(**d)
        assert original == reconstructed

    def test_tts_to_dict(self) -> None:
        cfg = TTSConfig(provider="elevenlabs", api_key="el_key", voice="emma")
        d = cfg.to_dict()
        assert d == {"provider": "elevenlabs", "api_key": "el_key", "voice": "emma"}

    def test_tts_roundtrip(self) -> None:
        original = TTSConfig(provider="openai", api_key="oai_key", voice="nova")
        d = original.to_dict()
        reconstructed = TTSConfig(**d)
        assert original == reconstructed


# ---------------------------------------------------------------------------
# CostBreakdown / LatencyBreakdown defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBreakdownDefaults:
    """CostBreakdown and LatencyBreakdown default to zeros."""

    def test_cost_defaults(self) -> None:
        c = CostBreakdown()
        assert c.stt == 0.0
        assert c.tts == 0.0
        assert c.llm == 0.0
        assert c.telephony == 0.0
        assert c.total == 0.0

    def test_latency_defaults(self) -> None:
        lb = LatencyBreakdown()
        assert lb.stt_ms == 0.0
        assert lb.llm_ms == 0.0
        assert lb.tts_ms == 0.0
        assert lb.total_ms == 0.0


# ---------------------------------------------------------------------------
# TurnMetrics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTurnMetrics:
    """TurnMetrics construction."""

    def test_basic(self) -> None:
        lb = LatencyBreakdown(stt_ms=10.0, llm_ms=20.0, tts_ms=5.0, total_ms=35.0)
        turn = TurnMetrics(
            turn_index=0,
            user_text="Hello",
            agent_text="Hi there",
            latency=lb,
            stt_audio_seconds=1.5,
            tts_characters=8,
            timestamp=1000.0,
        )
        assert turn.turn_index == 0
        assert turn.user_text == "Hello"
        assert turn.latency.total_ms == 35.0


# ---------------------------------------------------------------------------
# CallMetrics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCallMetrics:
    """CallMetrics — frozen aggregate metrics."""

    def test_construction(self) -> None:
        cm = CallMetrics(
            call_id="c1",
            duration_seconds=30.5,
            turns=(),
            cost=CostBreakdown(total=0.05),
            latency_avg=LatencyBreakdown(),
            latency_p95=LatencyBreakdown(),
            provider_mode="pipeline",
        )
        assert cm.call_id == "c1"
        assert cm.duration_seconds == 30.5
        assert cm.cost.total == 0.05
        assert cm.provider_mode == "pipeline"

    def test_frozen(self) -> None:
        cm = CallMetrics(
            call_id="c1",
            duration_seconds=10.0,
            turns=(),
            cost=CostBreakdown(),
            latency_avg=LatencyBreakdown(),
            latency_p95=LatencyBreakdown(),
            provider_mode="openai_realtime",
        )
        with pytest.raises(AttributeError):
            cm.call_id = "c2"


# ---------------------------------------------------------------------------
# CallControl
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCallControl:
    """CallControl — in-call control interface."""

    def test_initial_state(self) -> None:
        cc = CallControl(
            call_id="c1",
            caller="+1111",
            callee="+2222",
            telephony_provider="twilio",
        )
        assert not cc.is_transferred
        assert not cc.is_hung_up
        assert not cc.ended

    async def test_transfer(self) -> None:
        transfer_fn = AsyncMock()
        cc = CallControl(
            call_id="c1",
            caller="+1111",
            callee="+2222",
            telephony_provider="twilio",
            _transfer_fn=transfer_fn,
        )
        await cc.transfer("+3333")
        transfer_fn.assert_awaited_once_with("+3333")
        assert cc.is_transferred
        assert cc.ended

    async def test_hangup(self) -> None:
        hangup_fn = AsyncMock()
        cc = CallControl(
            call_id="c1",
            caller="+1111",
            callee="+2222",
            telephony_provider="twilio",
            _hangup_fn=hangup_fn,
        )
        await cc.hangup()
        hangup_fn.assert_awaited_once()
        assert cc.is_hung_up
        assert cc.ended

    async def test_transfer_no_fn(self) -> None:
        """transfer() without a function is a no-op (warning logged)."""
        cc = CallControl(
            call_id="c1",
            caller="+1",
            callee="+2",
            telephony_provider="telnyx",
        )
        await cc.transfer("+3333")
        assert not cc.is_transferred

    async def test_hangup_no_fn(self) -> None:
        """hangup() without a function is a no-op (warning logged)."""
        cc = CallControl(
            call_id="c1",
            caller="+1",
            callee="+2",
            telephony_provider="telnyx",
        )
        await cc.hangup()
        assert not cc.is_hung_up


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGuardrail:
    """Guardrail dataclass."""

    def test_defaults(self) -> None:
        g = Guardrail(name="test")
        assert g.check is None
        assert g.blocked_terms is None
        assert g.replacement == "I'm sorry, I can't respond to that."

    def test_custom_check(self) -> None:
        fn = lambda text: "bad" in text
        g = Guardrail(name="custom", check=fn, replacement="Nope.")
        assert g.check("this is bad") is True
        assert g.check("this is fine") is False
        assert g.replacement == "Nope."

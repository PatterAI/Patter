"""Plumbing + parity for the issue-#154 response-decoupling opt-out flag.

``gate_response_on_transcript`` (snake_case, default ``False``) must flow from
the engine markers and from ``Patter.agent(...)`` down onto the ``Agent`` and
into the adapter constructor, mirroring the existing ``noise_reduction`` /
``turn_detection`` wiring exactly. Default everywhere is ``False`` = the new
decoupled behavior (response on speech-stop, not gated on Whisper).
"""

from __future__ import annotations

import pytest

from getpatter import (
    OpenAIRealtime,
    OpenAIRealtime2,
    Patter,
    Twilio,
)
from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter
from getpatter.providers.openai_realtime_2 import OpenAIRealtime2Adapter


@pytest.fixture(autouse=True)
def _openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def _phone() -> Patter:
    return Patter(
        carrier=Twilio(account_sid="ACtest", auth_token="tok"),
        phone_number="+15555550100",
    )


@pytest.mark.unit
class TestEngineMarkerCarriesFlag:
    def test_realtime2_marker_default_is_none(self) -> None:
        eng = OpenAIRealtime2()
        assert eng.gate_response_on_transcript is None

    def test_realtime_marker_default_is_none(self) -> None:
        eng = OpenAIRealtime()
        assert eng.gate_response_on_transcript is None

    def test_realtime2_marker_carries_true(self) -> None:
        eng = OpenAIRealtime2(gate_response_on_transcript=True)
        assert eng.gate_response_on_transcript is True

    def test_realtime_marker_carries_true(self) -> None:
        eng = OpenAIRealtime(gate_response_on_transcript=True)
        assert eng.gate_response_on_transcript is True


@pytest.mark.unit
class TestAgentPlumbing:
    def test_agent_default_decoupled(self) -> None:
        phone = _phone()
        agent = phone.agent(system_prompt="hi", engine=OpenAIRealtime2())
        # Default leaves it unset -> adapter applies the False (decoupled) default.
        assert agent.realtime_gate_response_on_transcript is None

    def test_agent_picks_up_engine_flag(self) -> None:
        phone = _phone()
        agent = phone.agent(
            system_prompt="hi",
            engine=OpenAIRealtime2(gate_response_on_transcript=True),
        )
        assert agent.realtime_gate_response_on_transcript is True

    def test_v1_engine_carries_flag(self) -> None:
        phone = _phone()
        agent = phone.agent(
            system_prompt="hi",
            engine=OpenAIRealtime(gate_response_on_transcript=True),
        )
        assert agent.realtime_gate_response_on_transcript is True

    def test_explicit_agent_kwarg_overrides_engine(self) -> None:
        phone = _phone()
        agent = phone.agent(
            system_prompt="hi",
            realtime_gate_response_on_transcript=False,
            engine=OpenAIRealtime2(gate_response_on_transcript=True),
        )
        # Explicit kwarg wins over the engine marker value (precedence mirrors
        # noise_reduction / turn_detection).
        assert agent.realtime_gate_response_on_transcript is False


@pytest.mark.unit
class TestAdapterConstructorDefault:
    def test_base_adapter_default_false(self) -> None:
        ad = OpenAIRealtimeAdapter(api_key="sk-test")
        assert ad.gate_response_on_transcript is False

    def test_base_adapter_explicit_true(self) -> None:
        ad = OpenAIRealtimeAdapter(api_key="sk-test", gate_response_on_transcript=True)
        assert ad.gate_response_on_transcript is True

    def test_base_adapter_explicit_none_is_false(self) -> None:
        ad = OpenAIRealtimeAdapter(api_key="sk-test", gate_response_on_transcript=None)
        assert ad.gate_response_on_transcript is False

    def test_ga_adapter_inherits_flag_via_kwargs(self) -> None:
        # The GA adapter forwards *args/**kwargs to the base ctor, so the
        # flag must reach it unchanged.
        ad = OpenAIRealtime2Adapter(api_key="sk-test", gate_response_on_transcript=True)
        assert ad.gate_response_on_transcript is True

    def test_ga_adapter_default_false(self) -> None:
        ad = OpenAIRealtime2Adapter(api_key="sk-test")
        assert ad.gate_response_on_transcript is False

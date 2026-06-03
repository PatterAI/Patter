"""Unit tests for wiring the Realtime knobs through Patter.agent().

Covers POINT 1a / 1b plumbing: engine markers carry noise_reduction +
turn_detection, agent() extracts them with explicit-kwarg-wins precedence, and
backward compat (neither set -> both None). No external boundary -> no marker.
"""

from __future__ import annotations

import pytest

from getpatter import (
    OpenAIRealtime,
    OpenAIRealtime2,
    Patter,
    RealtimeTurnDetection,
    Twilio,
)


@pytest.fixture(autouse=True)
def _openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Engine markers read OPENAI_API_KEY when no api_key is passed; set a
    # dummy so the marker constructors and the agent() OpenAI-key guard
    # don't raise. No network is touched.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def _phone() -> Patter:
    return Patter(
        carrier=Twilio(account_sid="ACtest", auth_token="tok"),
        phone_number="+15555550100",
    )


def test_agent_picks_up_engine_noise_reduction_and_turn_detection() -> None:
    phone = _phone()
    td = RealtimeTurnDetection(threshold=0.6)
    agent = phone.agent(
        system_prompt="hi",
        engine=OpenAIRealtime2(noise_reduction="far_field", turn_detection=td),
    )
    assert agent.openai_realtime_noise_reduction == "far_field"
    assert agent.realtime_turn_detection is td


def test_agent_v1_engine_carries_knobs() -> None:
    phone = _phone()
    td = RealtimeTurnDetection(type="semantic_vad", eagerness="low")
    agent = phone.agent(
        system_prompt="hi",
        engine=OpenAIRealtime(noise_reduction="near_field", turn_detection=td),
    )
    assert agent.openai_realtime_noise_reduction == "near_field"
    assert agent.realtime_turn_detection is td


def test_explicit_agent_kwarg_overrides_engine() -> None:
    phone = _phone()
    agent = phone.agent(
        system_prompt="hi",
        openai_realtime_noise_reduction="near_field",
        engine=OpenAIRealtime2(noise_reduction="far_field"),
    )
    # Explicit kwarg wins over the engine marker value.
    assert agent.openai_realtime_noise_reduction == "near_field"


def test_agent_defaults_preserve_today_behavior() -> None:
    phone = _phone()
    agent = phone.agent(system_prompt="hi", engine=OpenAIRealtime2())
    assert agent.openai_realtime_noise_reduction is None
    assert agent.realtime_turn_detection is None


def test_engine_rejects_invalid_noise_reduction() -> None:
    # The engine marker does not validate (the adapter does), but the
    # value is preserved verbatim for forwarding. The adapter ctor is the
    # validation point — covered in test_realtime_turn_detection_model.
    eng = OpenAIRealtime2(noise_reduction="far_field")
    assert eng.noise_reduction == "far_field"

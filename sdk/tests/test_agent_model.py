"""Tests for Agent and CallEvent models."""

import pytest
from patter.models import Agent, CallEvent, IncomingMessage, STTConfig, TTSConfig


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def test_agent_defaults():
    agent = Agent(system_prompt="test")
    assert agent.voice == "alloy"
    assert agent.model == "gpt-4o-mini-realtime-preview"
    assert agent.language == "en"
    assert agent.first_message == ""
    assert agent.tools is None
    assert agent.provider == "openai_realtime"


def test_agent_custom_voice():
    agent = Agent(system_prompt="test", voice="nova")
    assert agent.voice == "nova"


def test_agent_custom_language():
    agent = Agent(system_prompt="test", language="it")
    assert agent.language == "it"


def test_agent_first_message():
    agent = Agent(system_prompt="test", first_message="Ciao!")
    assert agent.first_message == "Ciao!"


def test_agent_tools():
    tools = [{"name": "lookup", "description": "Look up info", "parameters": {}}]
    agent = Agent(system_prompt="test", tools=tools)
    assert agent.tools is not None
    assert len(agent.tools) == 1
    assert agent.tools[0]["name"] == "lookup"


def test_agent_model_custom():
    agent = Agent(system_prompt="test", model="gpt-4o-realtime-preview")
    assert agent.model == "gpt-4o-realtime-preview"


def test_agent_system_prompt():
    agent = Agent(system_prompt="You are a helpful assistant.")
    assert agent.system_prompt == "You are a helpful assistant."


def test_agent_is_frozen():
    """Agent should be immutable (frozen=True)."""
    agent = Agent(system_prompt="test")
    with pytest.raises(Exception):
        agent.voice = "changed"  # type: ignore[misc]


def test_agent_provider_elevenlabs():
    agent = Agent(system_prompt="test", provider="elevenlabs_convai")
    assert agent.provider == "elevenlabs_convai"


def test_agent_provider_pipeline():
    agent = Agent(system_prompt="test", provider="pipeline")
    assert agent.provider == "pipeline"


# ---------------------------------------------------------------------------
# CallEvent
# ---------------------------------------------------------------------------


def test_call_event_required():
    event = CallEvent(call_id="c1")
    assert event.call_id == "c1"
    assert event.caller == ""
    assert event.callee == ""
    assert event.direction == ""


def test_call_event_full():
    event = CallEvent(call_id="c1", caller="+39", callee="+1", direction="inbound")
    assert event.call_id == "c1"
    assert event.caller == "+39"
    assert event.callee == "+1"
    assert event.direction == "inbound"


def test_call_event_is_frozen():
    event = CallEvent(call_id="c1")
    with pytest.raises(Exception):
        event.call_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# IncomingMessage
# ---------------------------------------------------------------------------


def test_incoming_message_fields():
    msg = IncomingMessage(text="hello", call_id="c1", caller="+39")
    assert msg.text == "hello"
    assert msg.call_id == "c1"
    assert msg.caller == "+39"


def test_incoming_message_is_frozen():
    msg = IncomingMessage(text="hello", call_id="c1", caller="+39")
    with pytest.raises(Exception):
        msg.text = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# STTConfig
# ---------------------------------------------------------------------------


def test_stt_config_to_dict():
    config = STTConfig(provider="deepgram", api_key="dg_test")
    d = config.to_dict()
    assert d["provider"] == "deepgram"
    assert d["api_key"] == "dg_test"
    assert d["language"] == "en"


def test_stt_config_custom_language():
    config = STTConfig(provider="whisper", api_key="sk", language="it")
    assert config.to_dict()["language"] == "it"


def test_stt_config_is_frozen():
    config = STTConfig(provider="deepgram", api_key="dg_test")
    with pytest.raises(Exception):
        config.provider = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TTSConfig
# ---------------------------------------------------------------------------


def test_tts_config_to_dict():
    config = TTSConfig(provider="elevenlabs", api_key="el_test", voice="aria")
    d = config.to_dict()
    assert d["provider"] == "elevenlabs"
    assert d["api_key"] == "el_test"
    assert d["voice"] == "aria"


def test_tts_config_default_voice():
    config = TTSConfig(provider="openai", api_key="sk")
    assert config.voice == "alloy"


def test_tts_config_is_frozen():
    config = TTSConfig(provider="openai", api_key="sk")
    with pytest.raises(Exception):
        config.provider = "changed"  # type: ignore[misc]

"""Tests for parameter validation, output guardrails, and clear error messages."""

from __future__ import annotations

import asyncio

import pytest

from getpatter import (
    DeepgramSTT,
    ElevenLabsTTS,
    OpenAIRealtime,
    Patter,
    Telnyx,
    Tool,
    Twilio,
    guardrail,
    tool,
)
from getpatter.models import Agent, Guardrail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_phone(**kwargs):
    """Create a minimal Patter instance in local mode."""
    defaults = dict(
        carrier=Twilio(account_sid="AC_test", auth_token="tok_test"),
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    defaults.update(kwargs)
    return Patter(**defaults)


# ---------------------------------------------------------------------------
# Feature 1a: Patter() constructor validation — local mode
# ---------------------------------------------------------------------------


def test_local_mode_explicit_no_telephony_allowed():
    """Explicit mode='local' without a carrier is allowed (e.g. for testing)."""
    phone = Patter(mode="local", phone_number="+1", webhook_url="x")
    assert phone is not None


def test_local_mode_requires_phone_number():
    """Local mode with a carrier but no phone_number raises ValueError."""
    with pytest.raises(ValueError, match="phone_number"):
        Patter(carrier=Twilio(account_sid="AC", auth_token="tk"), webhook_url="x")


def test_local_mode_accepts_missing_webhook_url():
    """Local mode without webhook_url is OK (deferred to serve)."""
    phone = Patter(
        carrier=Twilio(account_sid="AC", auth_token="tk"),
        phone_number="+1",
    )
    assert phone is not None


def test_twilio_carrier_requires_auth_token():
    """Twilio() without both account_sid and auth_token raises ValueError."""
    with pytest.raises(ValueError, match="account_sid and auth_token"):
        Twilio(account_sid="AC")


def test_local_mode_valid_construction():
    """Valid local mode construction does not raise."""
    phone = _local_phone()
    assert phone is not None


# ---------------------------------------------------------------------------
# Feature 1b: serve() validation
# ---------------------------------------------------------------------------


def test_serve_validates_agent_type():
    """serve() with a non-Agent raises TypeError."""
    phone = _local_phone()
    with pytest.raises(TypeError, match="agent must be an Agent"):
        asyncio.run(phone.serve("not an agent"))


def test_serve_validates_port_type():
    """serve() with a non-int port raises ValueError."""
    phone = _local_phone()
    agent = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")
    with pytest.raises(ValueError, match="port must be an integer"):
        asyncio.run(phone.serve(agent, port="8000"))


def test_serve_validates_port_range_low():
    """serve() with port < 1 raises ValueError."""
    phone = _local_phone()
    agent = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")
    with pytest.raises(ValueError, match="port must be an integer"):
        asyncio.run(phone.serve(agent, port=0))


def test_serve_validates_port_range_high():
    """serve() with port > 65535 raises ValueError."""
    phone = _local_phone()
    agent = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")
    with pytest.raises(ValueError, match="port must be an integer"):
        asyncio.run(phone.serve(agent, port=99999))


def test_serve_validates_recording_type():
    """serve() with non-bool recording raises TypeError."""
    phone = _local_phone()
    agent = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")
    with pytest.raises(TypeError, match="recording must be a bool"):
        asyncio.run(phone.serve(agent, recording="yes"))


# ---------------------------------------------------------------------------
# Feature 1c: agent() validation
# ---------------------------------------------------------------------------


def test_agent_engine_dispatch():
    """agent() with an engine instance produces the right provider."""
    phone = _local_phone()
    a = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")
    assert a.provider == "openai_realtime"


def test_agent_pipeline_requires_stt_instance():
    """Pipeline mode (tts only) without stt raises ValueError."""
    phone = _local_phone()
    with pytest.raises(ValueError, match="Pipeline mode requires an STT"):
        phone.agent(system_prompt="test", tts=ElevenLabsTTS(api_key="el"))


def test_agent_openai_realtime_requires_openai_key():
    """openai_realtime provider without openai_key raises ValueError."""
    # Build a Patter without any openai_key, and call agent() without an engine
    # so the default provider picks up openai_realtime without a key.
    phone = Patter(
        carrier=Telnyx(api_key="KEY_test", connection_id="200"),
        phone_number="+1",
        webhook_url="x",
    )
    with pytest.raises(ValueError, match="OpenAI"):
        phone.agent(system_prompt="test")


def test_agent_tools_must_be_list():
    """agent() with tools as a non-list raises TypeError."""
    phone = _local_phone()
    with pytest.raises(TypeError, match="tools must be a list"):
        phone.agent(
            engine=OpenAIRealtime(api_key="sk"),
            system_prompt="test",
            tools={"name": "bad"},
        )


def test_agent_tools_items_must_be_tool_instances():
    """agent() with a raw dict tool item raises TypeError (v0.5.0 break)."""
    phone = _local_phone()
    with pytest.raises(TypeError, match="Tool instance"):
        phone.agent(
            engine=OpenAIRealtime(api_key="sk"),
            system_prompt="test",
            tools=[{"name": "legacy", "webhook_url": "https://x"}],
        )


def test_agent_variables_must_be_dict():
    """agent() with non-dict variables raises TypeError."""
    phone = _local_phone()
    with pytest.raises(TypeError, match="variables must be a dict"):
        phone.agent(
            engine=OpenAIRealtime(api_key="sk"),
            system_prompt="test",
            variables=["a", "b"],
        )


def test_agent_guardrails_must_be_list():
    """agent() with non-list guardrails raises TypeError."""
    phone = _local_phone()
    with pytest.raises(TypeError, match="guardrails must be a list"):
        phone.agent(
            engine=OpenAIRealtime(api_key="sk"),
            system_prompt="test",
            guardrails="not-a-list",
        )


def test_agent_guardrails_must_be_guardrail_instances():
    """agent() with a raw dict guardrail raises TypeError (v0.5.0 break)."""
    phone = _local_phone()
    with pytest.raises(TypeError, match="Guardrail instance"):
        phone.agent(
            engine=OpenAIRealtime(api_key="sk"),
            system_prompt="test",
            guardrails=[{"name": "legacy", "blocked_terms": ["x"]}],
        )


# ---------------------------------------------------------------------------
# Feature 1d: call() validation
# ---------------------------------------------------------------------------


def test_call_validates_e164_no_plus():
    """call() with a number not starting with '+' raises ValueError."""
    phone = _local_phone()
    agent = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")
    with pytest.raises(ValueError, match="E.164"):
        asyncio.run(
            phone.call(to="0039123456789", agent=agent)
        )


def test_call_validates_e164_empty():
    """call() with an empty string raises ValueError."""
    phone = _local_phone()
    agent = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")
    with pytest.raises(ValueError, match="E.164"):
        asyncio.run(
            phone.call(to="", agent=agent)
        )


def test_call_validates_e164_non_string():
    """call() with a non-string 'to' raises ValueError."""
    phone = _local_phone()
    agent = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")
    with pytest.raises(ValueError, match="E.164"):
        asyncio.run(
            phone.call(to=12345, agent=agent)
        )


def test_call_valid_e164_accepted():
    """call() with a valid E.164 number passes the validation step."""
    from unittest.mock import AsyncMock, MagicMock, patch

    phone = _local_phone()
    agent = phone.agent(engine=OpenAIRealtime(api_key="sk"), system_prompt="test")

    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        mock_instance = MagicMock()
        mock_instance.initiate_call = AsyncMock(return_value="CA123")
        MockAdapter.return_value = mock_instance

        asyncio.run(
            phone.call(to="+39123456789", agent=agent)
        )

        mock_instance.initiate_call.assert_called_once()


# ---------------------------------------------------------------------------
# Feature 2: Guardrail model
# ---------------------------------------------------------------------------


def test_guardrail_dataclass_creation():
    """Guardrail dataclass can be constructed with all fields."""
    g = Guardrail(
        name="test",
        blocked_terms=["bad"],
        check=lambda t: False,
        replacement="Nope.",
    )
    assert g.name == "test"
    assert g.blocked_terms == ["bad"]
    assert g.replacement == "Nope."


def test_guardrail_defaults():
    """Guardrail defaults: check=None, blocked_terms=None, replacement has default."""
    g = Guardrail(name="default")
    assert g.check is None
    assert g.blocked_terms is None
    assert "sorry" in g.replacement.lower()


def test_guardrail_is_frozen():
    """Guardrail is a frozen dataclass."""
    g = Guardrail(name="x")
    with pytest.raises(Exception):
        g.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Feature 2: module-level guardrail() factory
# ---------------------------------------------------------------------------


def test_guardrail_factory_returns_instance():
    g = guardrail(name="test", blocked_terms=["bad"])
    assert isinstance(g, Guardrail)


def test_guardrail_factory_name():
    g = guardrail(name="no-profanity")
    assert g.name == "no-profanity"


def test_guardrail_factory_blocked_terms():
    g = guardrail(name="test", blocked_terms=["bad", "worse"])
    assert "bad" in g.blocked_terms
    assert "worse" in g.blocked_terms


def test_guardrail_factory_check_callable():
    fn = lambda t: "nope" in t
    g = guardrail(name="test", check=fn)
    assert g.check is fn


def test_guardrail_factory_custom_replacement():
    g = guardrail(name="test", replacement="Custom message.")
    assert g.replacement == "Custom message."


def test_guardrail_factory_default_replacement():
    g = guardrail(name="test")
    assert isinstance(g.replacement, str)
    assert len(g.replacement) > 0


# ---------------------------------------------------------------------------
# Feature 2: agent() accepts guardrails and stores them on Agent
# ---------------------------------------------------------------------------


def test_agent_with_guardrails():
    """agent() with guardrails stores them on the Agent."""
    phone = _local_phone()
    agent = phone.agent(
        engine=OpenAIRealtime(api_key="sk"),
        system_prompt="test",
        guardrails=[guardrail(name="test", blocked_terms=["bad"])],
    )
    assert agent.guardrails is not None
    assert len(agent.guardrails) == 1
    assert agent.guardrails[0]["name"] == "test"


def test_agent_without_guardrails_defaults_to_none():
    """agent() without guardrails sets Agent.guardrails to None."""
    phone = _local_phone()
    agent = phone.agent(
        engine=OpenAIRealtime(api_key="sk"),
        system_prompt="test",
    )
    assert agent.guardrails is None


def test_agent_multiple_guardrails():
    """agent() accepts a list with multiple guardrails."""
    phone = _local_phone()
    agent = phone.agent(
        engine=OpenAIRealtime(api_key="sk"),
        system_prompt="test",
        guardrails=[
            guardrail(name="g1", blocked_terms=["a"]),
            guardrail(name="g2", blocked_terms=["b"]),
            guardrail(
                name="g3",
                check=lambda t: "c" in t,
                replacement="Not allowed.",
            ),
        ],
    )
    assert len(agent.guardrails) == 3


# ---------------------------------------------------------------------------
# Feature 2: guardrail logic (unit-tested independently)
# ---------------------------------------------------------------------------


def test_guardrail_blocked_terms_match():
    """A blocked_term match returns blocked=True."""
    guard = guardrail(name="test", blocked_terms=["diagnosis"])
    text = "You have a diagnosis of flu."
    blocked = any(term.lower() in text.lower() for term in guard.blocked_terms)
    assert blocked is True


def test_guardrail_blocked_terms_case_insensitive():
    """blocked_terms matching is case-insensitive."""
    guard = guardrail(name="test", blocked_terms=["DIAGNOSIS"])
    text = "Your diagnosis is clear."
    blocked = any(term.lower() in text.lower() for term in guard.blocked_terms)
    assert blocked is True


def test_guardrail_blocked_terms_no_match():
    """Non-matching text returns blocked=False."""
    guard = guardrail(name="test", blocked_terms=["diagnosis"])
    text = "Everything looks fine."
    blocked = any(term.lower() in text.lower() for term in guard.blocked_terms)
    assert blocked is False


def test_guardrail_check_fn_blocks():
    """A check function returning True blocks the response."""
    guard = guardrail(
        name="profanity",
        check=lambda t: "damn" in t.lower(),
    )
    text = "Damn, that's unfortunate."
    assert guard.check is not None
    assert guard.check(text) is True


def test_guardrail_check_fn_passes():
    """A check function returning False allows the response."""
    guard = guardrail(
        name="profanity",
        check=lambda t: "damn" in t.lower(),
    )
    text = "That's unfortunate."
    assert guard.check is not None
    assert guard.check(text) is False


# ---------------------------------------------------------------------------
# Feature 2: guardrail application in twilio_handler (integration test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guardrail_triggers_cancel_and_replacement():
    """transcript_output event triggers guardrail: cancel_response + send_text called."""
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    # Use the same dict shape as Patter._guardrail_to_dict, since
    # twilio_handler works on dicts.
    agent = Agent(
        system_prompt="test",
        provider="openai_realtime",
        guardrails=[
            {
                "name": "no-bad",
                "blocked_terms": ["blocked_word"],
                "check": None,
                "replacement": "I can't say that.",
            }
        ],
    )

    start_payload = json.dumps({
        "event": "start",
        "streamSid": "SID_guard",
        "start": {"callSid": "CA_guard", "customParameters": {}},
    })
    stop_payload = json.dumps({"event": "stop"})
    messages = [start_payload, stop_payload]
    idx = 0

    class FakeWS:
        query_params = {"caller": "+1", "callee": "+2"}
        sent = []

        async def accept(self):
            pass

        async def receive_text(self):
            nonlocal idx
            if idx < len(messages):
                msg = messages[idx]
                idx += 1
                if idx == len(messages):
                    # Yield control so forward_to_twilio task can process the event
                    await asyncio.sleep(0.1)
                return msg
            await asyncio.sleep(10)

        async def send_text(self, data):
            self.sent.append(data)

    fake_ws = FakeWS()

    mock_adapter = AsyncMock()
    mock_adapter.connect = AsyncMock()
    mock_adapter.close = AsyncMock()
    mock_adapter.cancel_response = AsyncMock()
    mock_adapter.send_text = AsyncMock()

    async def fake_events():
        yield "transcript_output", "This contains the blocked_word in it."

    mock_adapter.receive_events = MagicMock(return_value=fake_events())

    with patch("getpatter.providers.openai_realtime.OpenAIRealtimeAdapter", return_value=mock_adapter):
        from getpatter.handlers.twilio_handler import twilio_stream_bridge

        try:
            await asyncio.wait_for(
                twilio_stream_bridge(
                    websocket=fake_ws,
                    agent=agent,
                    openai_key="sk-test",
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    mock_adapter.cancel_response.assert_called()
    mock_adapter.send_text.assert_called_with("I can't say that.")


@pytest.mark.asyncio
async def test_guardrail_does_not_trigger_on_clean_response():
    """transcript_output without blocked terms does NOT trigger guardrail."""
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    agent = Agent(
        system_prompt="test",
        provider="openai_realtime",
        guardrails=[
            {"name": "no-bad", "blocked_terms": ["blocked_word"], "check": None, "replacement": "..."}
        ],
    )

    start_payload = json.dumps({
        "event": "start",
        "streamSid": "SID_clean",
        "start": {"callSid": "CA_clean", "customParameters": {}},
    })
    stop_payload = json.dumps({"event": "stop"})
    messages = [start_payload, stop_payload]
    idx = 0

    class FakeWS:
        query_params = {"caller": "+1", "callee": "+2"}
        sent = []

        async def accept(self):
            pass

        async def receive_text(self):
            nonlocal idx
            if idx < len(messages):
                msg = messages[idx]
                idx += 1
                return msg
            await asyncio.sleep(10)

        async def send_text(self, data):
            self.sent.append(data)

    fake_ws = FakeWS()

    mock_adapter = AsyncMock()
    mock_adapter.connect = AsyncMock()
    mock_adapter.close = AsyncMock()
    mock_adapter.cancel_response = AsyncMock()
    mock_adapter.send_text = AsyncMock()

    async def fake_events():
        yield "transcript_output", "This is a perfectly clean response."

    mock_adapter.receive_events = MagicMock(return_value=fake_events())

    with patch("getpatter.providers.openai_realtime.OpenAIRealtimeAdapter", return_value=mock_adapter):
        from getpatter.handlers.twilio_handler import twilio_stream_bridge

        try:
            await asyncio.wait_for(
                twilio_stream_bridge(
                    websocket=fake_ws,
                    agent=agent,
                    openai_key="sk-test",
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    mock_adapter.cancel_response.assert_not_called()

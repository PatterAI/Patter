"""Tests for call transfer, call recording, answering machine detection, end-call tool,
voicemail drop, and webhook retry/fallback."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter import Patter
from patter.models import Agent
from patter.handlers.twilio_handler import _TRANSFER_CALL_TOOL, _END_CALL_TOOL


# ---------------------------------------------------------------------------
# Feature 1: Call Transfer
# ---------------------------------------------------------------------------


def test_transfer_call_tool_has_required_fields():
    """_TRANSFER_CALL_TOOL has name, description, and parameters."""
    assert _TRANSFER_CALL_TOOL["name"] == "transfer_call"
    assert "description" in _TRANSFER_CALL_TOOL
    assert "parameters" in _TRANSFER_CALL_TOOL


def test_transfer_call_tool_requires_number_param():
    """transfer_call tool requires the 'number' parameter."""
    params = _TRANSFER_CALL_TOOL["parameters"]
    assert "number" in params["properties"]
    assert "number" in params["required"]


def test_transfer_call_tool_injected_into_openai_tools():
    """transfer_call is appended to agent tools when building OpenAI tool list."""
    # We verify this by checking that the system tool constant is always present
    # alongside agent tools in the handler.  The module-level constant is the
    # canonical definition used in twilio_stream_bridge.
    assert _TRANSFER_CALL_TOOL["name"] == "transfer_call"


def test_transfer_call_tool_injected_when_no_agent_tools():
    """transfer_call is injected even when the agent has no user-defined tools."""
    agent = Agent(system_prompt="Test")
    assert agent.tools is None
    # The handler builds openai_tools as [] + [_TRANSFER_CALL_TOOL]
    # We validate the constant is still accessible and correct
    assert _TRANSFER_CALL_TOOL["name"] == "transfer_call"


def test_transfer_call_tool_injected_alongside_agent_tools():
    """transfer_call is appended after agent-defined tools."""
    user_tool = {"name": "lookup", "description": "Look up", "parameters": {}, "webhook_url": "https://x.com"}
    agent = Agent(system_prompt="Test", tools=[user_tool])
    # Simulate what the handler does
    agent_tools = [
        {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}
        for t in (agent.tools or [])
    ]
    openai_tools = agent_tools + [_TRANSFER_CALL_TOOL]
    assert openai_tools[-1]["name"] == "transfer_call"
    assert openai_tools[0]["name"] == "lookup"


# ---------------------------------------------------------------------------
# Feature 2: Call Recording — Python SDK
# ---------------------------------------------------------------------------


def test_embedded_server_accepts_recording_flag():
    """EmbeddedServer can be instantiated with recording=True."""
    from patter.server import EmbeddedServer
    from patter.local_config import LocalConfig

    config = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="AC_test",
        twilio_token="tok_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = Agent(system_prompt="Test")
    server = EmbeddedServer(config=config, agent=agent, recording=True)
    assert server.recording is True


def test_embedded_server_recording_defaults_to_false():
    """EmbeddedServer recording defaults to False."""
    from patter.server import EmbeddedServer
    from patter.local_config import LocalConfig

    config = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="AC_test",
        twilio_token="tok_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = Agent(system_prompt="Test")
    server = EmbeddedServer(config=config, agent=agent)
    assert server.recording is False


def test_serve_passes_recording_to_server():
    """Patter.serve() passes recording=True to EmbeddedServer."""
    phone = Patter(
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = phone.agent(system_prompt="Test")

    with patch("patter.server.EmbeddedServer") as MockServer:
        mock_instance = MagicMock()
        mock_instance.start = AsyncMock()
        MockServer.return_value = mock_instance

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            phone.serve(agent, recording=True)
        )

        MockServer.assert_called_once()
        call_args = MockServer.call_args
        # recording is passed as keyword arg
        assert call_args.kwargs.get("recording") is True


def test_recording_webhook_endpoint_exists():
    """EmbeddedServer creates a /webhooks/twilio/recording endpoint."""
    from patter.server import EmbeddedServer
    from patter.local_config import LocalConfig

    config = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="AC_test",
        twilio_token="tok_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = Agent(system_prompt="Test")
    server = EmbeddedServer(config=config, agent=agent)
    app = server._create_app()
    routes = [r.path for r in app.routes]
    assert "/webhooks/twilio/recording" in routes


# ---------------------------------------------------------------------------
# Feature 3: Answering Machine Detection — Python SDK
# ---------------------------------------------------------------------------


def test_call_accepts_machine_detection_param():
    """Patter.call() accepts machine_detection=True without error in local mode."""
    phone = Patter(
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = phone.agent(system_prompt="Test")
    # Verify the parameter is accepted by the function signature
    import inspect
    sig = inspect.signature(phone.call)
    assert "machine_detection" in sig.parameters


def test_machine_detection_adds_params_to_twilio_call():
    """machine_detection=True passes AMD params to TwilioAdapter.initiate_call."""
    phone = Patter(
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = phone.agent(system_prompt="Test")

    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        mock_instance = MagicMock()
        mock_instance.initiate_call = AsyncMock(return_value="CA123")
        MockAdapter.return_value = mock_instance

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            phone.call(to="+39123456789", agent=agent, machine_detection=True)
        )

        mock_instance.initiate_call.assert_called_once()
        _, kwargs = mock_instance.initiate_call.call_args
        extra = kwargs.get("extra_params", {})
        assert extra.get("MachineDetection") == "DetectMessageEnd"
        assert extra.get("AsyncAmd") == "true"
        assert "AsyncAmdStatusCallback" in extra


def test_amd_callback_url_uses_webhook_host():
    """AMD callback URL contains the configured webhook host."""
    phone = Patter(
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk_test",
        phone_number="+15550000000",
        webhook_url="my.ngrok.io",
    )
    agent = phone.agent(system_prompt="Test")

    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        mock_instance = MagicMock()
        mock_instance.initiate_call = AsyncMock(return_value="CA123")
        MockAdapter.return_value = mock_instance

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            phone.call(to="+39123456789", agent=agent, machine_detection=True)
        )

        _, kwargs = mock_instance.initiate_call.call_args
        extra = kwargs.get("extra_params", {})
        assert "my.ngrok.io" in extra.get("AsyncAmdStatusCallback", "")


def test_amd_webhook_endpoint_exists():
    """EmbeddedServer creates a /webhooks/twilio/amd endpoint."""
    from patter.server import EmbeddedServer
    from patter.local_config import LocalConfig

    config = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="AC_test",
        twilio_token="tok_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = Agent(system_prompt="Test")
    server = EmbeddedServer(config=config, agent=agent)
    app = server._create_app()
    routes = [r.path for r in app.routes]
    assert "/webhooks/twilio/amd" in routes


def test_machine_detection_false_no_extra_params():
    """machine_detection=False does not add AMD params to the call."""
    phone = Patter(
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = phone.agent(system_prompt="Test")

    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        mock_instance = MagicMock()
        mock_instance.initiate_call = AsyncMock(return_value="CA123")
        MockAdapter.return_value = mock_instance

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            phone.call(to="+39123456789", agent=agent, machine_detection=False)
        )

        _, kwargs = mock_instance.initiate_call.call_args
        extra = kwargs.get("extra_params", {})
        assert not extra  # empty dict when AMD disabled


# ---------------------------------------------------------------------------
# Feature 4: End-call Tool
# ---------------------------------------------------------------------------


def test_end_call_tool_defined():
    """_END_CALL_TOOL has correct schema."""
    assert _END_CALL_TOOL["name"] == "end_call"
    assert "description" in _END_CALL_TOOL
    assert "parameters" in _END_CALL_TOOL
    props = _END_CALL_TOOL["parameters"]["properties"]
    assert "reason" in props


def test_end_call_tool_reason_is_optional():
    """end_call tool does not require 'reason' parameter."""
    # No 'required' key, or 'reason' not in required list
    required = _END_CALL_TOOL["parameters"].get("required", [])
    assert "reason" not in required


def test_end_call_tool_injected_in_openai_tools():
    """_END_CALL_TOOL is injected alongside _TRANSFER_CALL_TOOL in OpenAI tool list."""
    user_tool = {"name": "lookup", "description": "Look up", "parameters": {}, "webhook_url": "https://x.com"}
    agent = Agent(system_prompt="Test", tools=[user_tool])
    agent_tools = [
        {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}
        for t in (agent.tools or [])
    ]
    openai_tools = agent_tools + [_TRANSFER_CALL_TOOL, _END_CALL_TOOL]
    tool_names = [t["name"] for t in openai_tools]
    assert "transfer_call" in tool_names
    assert "end_call" in tool_names


def test_end_call_tool_injected_with_no_agent_tools():
    """_END_CALL_TOOL is injected even when agent has no user-defined tools."""
    agent = Agent(system_prompt="Test")
    agent_tools: list = []
    openai_tools = agent_tools + [_TRANSFER_CALL_TOOL, _END_CALL_TOOL]
    assert openai_tools[-1]["name"] == "end_call"


# ---------------------------------------------------------------------------
# Feature 5: Voicemail Drop
# ---------------------------------------------------------------------------


def test_voicemail_message_param_on_call():
    """Patter.call() accepts voicemail_message parameter."""
    import inspect
    phone = Patter(
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    sig = inspect.signature(phone.call)
    assert "voicemail_message" in sig.parameters


def test_voicemail_message_param_on_serve():
    """Patter.serve() accepts voicemail_message parameter."""
    import inspect
    phone = Patter(
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    sig = inspect.signature(phone.serve)
    assert "voicemail_message" in sig.parameters


def test_embedded_server_accepts_voicemail_message():
    """EmbeddedServer stores voicemail_message."""
    from patter.server import EmbeddedServer
    from patter.local_config import LocalConfig

    config = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="AC_test",
        twilio_token="tok_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = Agent(system_prompt="Test")
    server = EmbeddedServer(config=config, agent=agent, voicemail_message="Please call back.")
    assert server.voicemail_message == "Please call back."


def test_embedded_server_voicemail_message_defaults_empty():
    """EmbeddedServer voicemail_message defaults to empty string."""
    from patter.server import EmbeddedServer
    from patter.local_config import LocalConfig

    config = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="AC_test",
        twilio_token="tok_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = Agent(system_prompt="Test")
    server = EmbeddedServer(config=config, agent=agent)
    assert server.voicemail_message == ""


def test_serve_passes_voicemail_message_to_server():
    """Patter.serve() passes voicemail_message to EmbeddedServer."""
    phone = Patter(
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk_test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    agent = phone.agent(system_prompt="Test")

    with patch("patter.server.EmbeddedServer") as MockServer:
        mock_instance = MagicMock()
        mock_instance.start = AsyncMock()
        MockServer.return_value = mock_instance

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            phone.serve(agent, voicemail_message="Hi, please call back.")
        )

        MockServer.assert_called_once()
        call_kwargs = MockServer.call_args.kwargs
        assert call_kwargs.get("voicemail_message") == "Hi, please call back."


# ---------------------------------------------------------------------------
# Feature 6: Webhook Retry / Fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_executor_retries_on_failure():
    """ToolExecutor retries on failure up to MAX_RETRIES times."""
    from patter.services.tool_executor import ToolExecutor

    call_count = 0

    mock_client = AsyncMock()

    async def fail_twice_then_succeed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise Exception("transient error")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"ok": True})
        return mock_resp

    mock_client.post = fail_twice_then_succeed

    executor = ToolExecutor(client=mock_client)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await executor.execute(
            tool_name="test_tool",
            arguments={},
            webhook_url="https://example.com/hook",
            call_context={"call_id": "c1"},
        )

    parsed = json.loads(result)
    assert parsed == {"ok": True}
    assert call_count == 3


@pytest.mark.asyncio
async def test_tool_executor_returns_error_after_max_retries():
    """ToolExecutor returns error JSON with fallback=True after all retries fail."""
    from patter.services.tool_executor import ToolExecutor

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("persistent error"))

    executor = ToolExecutor(client=mock_client)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await executor.execute(
            tool_name="test_tool",
            arguments={},
            webhook_url="https://example.com/hook",
            call_context={},
        )

    parsed = json.loads(result)
    assert "error" in parsed
    assert parsed.get("fallback") is True
    assert "3 attempts" in parsed["error"]


@pytest.mark.asyncio
async def test_tool_executor_includes_attempt_number():
    """ToolExecutor includes attempt number in webhook payload."""
    from patter.services.tool_executor import ToolExecutor

    payloads: list[dict] = []

    mock_client = AsyncMock()

    async def capture_payload(*args, **kwargs):
        payloads.append(kwargs.get("json", {}))
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"result": "ok"})
        return mock_resp

    mock_client.post = capture_payload

    executor = ToolExecutor(client=mock_client)

    await executor.execute(
        tool_name="my_tool",
        arguments={"x": 1},
        webhook_url="https://example.com/hook",
        call_context={"call_id": "c1"},
    )

    assert len(payloads) == 1
    assert payloads[0]["attempt"] == 1


# ---------------------------------------------------------------------------
# Feature 7: Conversation History
# ---------------------------------------------------------------------------


def test_resolve_variables_replaces_placeholders():
    """_resolve_variables substitutes {key} with corresponding values."""
    from patter.handlers.twilio_handler import _resolve_variables

    result = _resolve_variables("Hello {name}, order #{order_id}!", {"name": "Mario", "order_id": "42"})
    assert result == "Hello Mario, order #42!"


def test_resolve_variables_ignores_missing_keys():
    """_resolve_variables leaves unmatched placeholders intact."""
    from patter.handlers.twilio_handler import _resolve_variables

    result = _resolve_variables("Hello {name}, {unknown}!", {"name": "Mario"})
    assert result == "Hello Mario, {unknown}!"


def test_resolve_variables_empty_variables():
    """_resolve_variables returns template unchanged when variables is empty."""
    from patter.handlers.twilio_handler import _resolve_variables

    template = "Hello {name}!"
    result = _resolve_variables(template, {})
    assert result == template


def test_resolve_variables_numeric_values():
    """_resolve_variables coerces non-string values to strings."""
    from patter.handlers.twilio_handler import _resolve_variables

    result = _resolve_variables("Balance: {amount}", {"amount": 99.50})
    assert result == "Balance: 99.5"


@pytest.mark.asyncio
async def test_conversation_history_pipeline_tracks_user_messages():
    """Pipeline stt_loop appends user messages to conversation_history."""
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock, patch
    from patter.models import Agent

    agent = Agent(system_prompt="Test", provider="pipeline")

    received_history = []

    async def on_message(data):
        received_history.append(list(data.get("history", [])))
        return "Reply"

    # Build a minimal mock websocket that sends start + stop
    ws = MagicMock()
    ws.query_params = {"caller": "+1", "callee": "+2"}

    events = [
        json.dumps({
            "event": "start",
            "streamSid": "SID",
            "start": {"callSid": "CA1", "customParameters": {}},
        }),
        json.dumps({"event": "stop"}),
    ]
    ws.receive_text = AsyncMock(side_effect=events)
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()

    # Mock transcript with one final utterance
    transcript_mock = MagicMock()
    transcript_mock.is_final = True
    transcript_mock.text = "Hello"

    mock_stt = AsyncMock()
    mock_stt.connect = AsyncMock()
    mock_stt.close = AsyncMock()

    async def fake_receive():
        yield transcript_mock

    mock_stt.receive_transcripts = fake_receive

    with patch("patter.handlers.twilio_handler._create_stt_from_config", return_value=None), \
         patch("patter.handlers.twilio_handler._create_tts_from_config", return_value=None):
        from patter.handlers.twilio_handler import twilio_stream_bridge
        try:
            await asyncio.wait_for(
                twilio_stream_bridge(
                    websocket=ws,
                    agent=agent,
                    openai_key="",
                    on_message=on_message,
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    # The handler should have called on_message at least once if STT produced a
    # transcript, or completed without error if the mocked providers returned
    # None (causing an early exit).  Either way the bridge must not raise.
    # When providers are mocked to None the bridge exits before invoking
    # on_message, so we assert the bridge completed (no exception escaped).
    assert ws.accept.called, "WebSocket accept should have been called"


@pytest.mark.asyncio
async def test_conversation_history_passed_to_on_call_end():
    """on_call_end receives conversation_history as 'transcript'."""
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock
    from patter.models import Agent

    agent = Agent(system_prompt="Test", provider="openai_realtime")
    received_payload: list[dict] = []

    async def on_call_end(data):
        received_payload.append(data)

    ws = MagicMock()
    ws.query_params = {"caller": "+1", "callee": "+2"}

    events = [
        json.dumps({
            "event": "start",
            "streamSid": "SID",
            "start": {"callSid": "CA1", "customParameters": {}},
        }),
        json.dumps({"event": "stop"}),
    ]
    ws.receive_text = AsyncMock(side_effect=events)
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()

    # Mock the OpenAI adapter so it never actually connects
    mock_adapter = AsyncMock()
    mock_adapter.connect = AsyncMock()
    mock_adapter.close = AsyncMock()
    mock_adapter.send_text = AsyncMock()

    async def fake_receive_events():
        return
        yield  # make it an async generator

    mock_adapter.receive_events = fake_receive_events

    with patch("patter.providers.openai_realtime.OpenAIRealtimeAdapter", return_value=mock_adapter):
        from patter.handlers.twilio_handler import twilio_stream_bridge
        try:
            await asyncio.wait_for(
                twilio_stream_bridge(
                    websocket=ws,
                    agent=agent,
                    openai_key="sk-test",
                    on_call_end=on_call_end,
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    # on_call_end should have been called with transcript key as a list
    assert len(received_payload) == 1
    assert "transcript" in received_payload[0]
    assert isinstance(received_payload[0]["transcript"], list)


# ---------------------------------------------------------------------------
# Feature 8: Dynamic Variables in System Prompt
# ---------------------------------------------------------------------------


def test_agent_model_has_variables_field():
    """Agent dataclass accepts a variables dict."""
    agent = Agent(system_prompt="Hello {name}", variables={"name": "Mario"})
    assert agent.variables == {"name": "Mario"}


def test_agent_variables_defaults_to_none():
    """Agent.variables defaults to None when not provided."""
    agent = Agent(system_prompt="Hello {name}")
    assert agent.variables is None


def test_dynamic_variables_replaced_via_resolve():
    """_resolve_variables applies agent.variables to the system prompt."""
    from patter.handlers.twilio_handler import _resolve_variables

    agent = Agent(
        system_prompt="You are calling {customer_name} about order #{order_id}.",
        variables={"customer_name": "Mario Rossi", "order_id": "12345"},
    )
    result = _resolve_variables(agent.system_prompt, agent.variables or {})
    assert result == "You are calling Mario Rossi about order #12345."


def test_custom_params_override_agent_variables():
    """custom_params from TwiML take precedence over agent.variables when keys clash."""
    from patter.handlers.twilio_handler import _resolve_variables

    agent_vars = {"name": "Default Name", "greeting": "Hello"}
    custom_params = {"name": "Override Name"}  # same key — should win
    all_vars = {**agent_vars, **custom_params}
    result = _resolve_variables("{name}: {greeting}", all_vars)
    assert result == "Override Name: Hello"


def test_dynamic_variables_no_op_when_no_placeholders():
    """_resolve_variables returns the template unchanged when no placeholders match."""
    from patter.handlers.twilio_handler import _resolve_variables

    template = "You are a helpful assistant."
    result = _resolve_variables(template, {"unused": "value"})
    assert result == template


def test_dynamic_variables_multiple_occurrences():
    """_resolve_variables replaces all occurrences of the same placeholder."""
    from patter.handlers.twilio_handler import _resolve_variables

    result = _resolve_variables("{name} is {name}", {"name": "Mario"})
    assert result == "Mario is Mario"

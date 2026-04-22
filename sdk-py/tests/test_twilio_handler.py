"""Tests for Twilio webhook handler."""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# twilio_webhook_handler
# ---------------------------------------------------------------------------


def test_twilio_webhook_generates_string():
    """twilio_webhook_handler returns a string."""
    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = "<Response/>"
        from getpatter.handlers.twilio_handler import twilio_webhook_handler

        result = twilio_webhook_handler("CA123", "+39111", "+16592", "abc.ngrok.io")
        assert isinstance(result, str)


def test_twilio_webhook_calls_generate_stream_twiml():
    """generate_stream_twiml is invoked exactly once."""
    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = "<Response/>"
        from getpatter.handlers.twilio_handler import twilio_webhook_handler

        twilio_webhook_handler("CA123", "+39111", "+16592", "abc.ngrok.io")
        MockAdapter.generate_stream_twiml.assert_called_once()


def test_twilio_webhook_returns_adapter_output():
    """Return value matches what generate_stream_twiml returns."""
    expected = "<Response><Connect><Stream/></Connect></Response>"
    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = expected
        from getpatter.handlers.twilio_handler import twilio_webhook_handler

        result = twilio_webhook_handler("CA123", "+39111", "+16592", "abc.ngrok.io")
        assert result == expected


def test_stream_url_is_wss():
    """Stream URL passed to generate_stream_twiml uses wss:// scheme."""
    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = "<Response/>"
        from getpatter.handlers.twilio_handler import twilio_webhook_handler

        twilio_webhook_handler("CA456", "+111", "+222", "my.host.io")
        url = MockAdapter.generate_stream_twiml.call_args[0][0]
        assert url.startswith("wss://")


def test_stream_url_contains_webhook_host():
    """Stream URL contains the webhook base URL."""
    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = "<Response/>"
        from getpatter.handlers.twilio_handler import twilio_webhook_handler

        twilio_webhook_handler("CA789", "+111", "+222", "custom.ngrok.io")
        url = MockAdapter.generate_stream_twiml.call_args[0][0]
        assert "custom.ngrok.io" in url


def test_stream_url_has_ws_stream_path():
    """Stream URL path includes /ws/stream/{call_sid}."""
    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = "<Response/>"
        from getpatter.handlers.twilio_handler import twilio_webhook_handler

        twilio_webhook_handler("CASID99", "+111", "+222", "abc.ngrok.io")
        url = MockAdapter.generate_stream_twiml.call_args[0][0]
        assert "/ws/stream/CASID99" in url


def test_stream_url_contains_caller_param():
    """Stream URL includes caller query param."""
    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = "<Response/>"
        from getpatter.handlers.twilio_handler import twilio_webhook_handler

        twilio_webhook_handler("CA123", "+39111", "+16592", "abc.ngrok.io")
        url = MockAdapter.generate_stream_twiml.call_args[0][0]
        assert "caller=" in url


def test_stream_url_contains_callee_param():
    """Stream URL includes callee query param."""
    with patch("getpatter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = "<Response/>"
        from getpatter.handlers.twilio_handler import twilio_webhook_handler

        twilio_webhook_handler("CA123", "+39111", "+16592", "abc.ngrok.io")
        url = MockAdapter.generate_stream_twiml.call_args[0][0]
        assert "callee=" in url


# ---------------------------------------------------------------------------
# telnyx_webhook_handler
# ---------------------------------------------------------------------------


def test_telnyx_webhook_returns_dict():
    from getpatter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler(
        call_id="ctrl_123",
        caller="+14155551234",
        callee="+15550001111",
        webhook_base_url="abc.ngrok.io",
    )
    assert isinstance(result, dict)


def test_telnyx_webhook_has_commands():
    from getpatter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler("ctrl_123", "+1", "+2", "abc.ngrok.io")
    assert "commands" in result


def test_telnyx_webhook_has_answer_command():
    from getpatter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler("ctrl_123", "+1", "+2", "abc.ngrok.io")
    commands = result["commands"]
    assert any(c["command"] == "answer" for c in commands)


def test_telnyx_webhook_has_stream_start_command():
    from getpatter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler("ctrl_123", "+1", "+2", "abc.ngrok.io")
    commands = result["commands"]
    stream_cmd = next((c for c in commands if c["command"] == "stream_start"), None)
    assert stream_cmd is not None


def test_telnyx_webhook_stream_url_wss():
    from getpatter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler("ctrl_123", "+1", "+2", "abc.ngrok.io")
    commands = result["commands"]
    stream_cmd = next(c for c in commands if c["command"] == "stream_start")
    assert stream_cmd["params"]["stream_url"].startswith("wss://")


def test_telnyx_webhook_stream_url_contains_call_id():
    from getpatter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler("ctrl_unique", "+1", "+2", "abc.ngrok.io")
    commands = result["commands"]
    stream_cmd = next(c for c in commands if c["command"] == "stream_start")
    assert "ctrl_unique" in stream_cmd["params"]["stream_url"]


def test_telnyx_webhook_stream_url_both_tracks():
    from getpatter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler("ctrl_123", "+1", "+2", "abc.ngrok.io")
    commands = result["commands"]
    stream_cmd = next(c for c in commands if c["command"] == "stream_start")
    assert stream_cmd["params"]["stream_track"] == "both_tracks"


def test_telnyx_webhook_stream_url_uses_telnyx_path():
    """Stream URL must point to the Telnyx-specific WebSocket handler, not the Twilio one."""
    from getpatter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler("ctrl_123", "+1", "+2", "abc.ngrok.io")
    commands = result["commands"]
    stream_cmd = next(c for c in commands if c["command"] == "stream_start")
    stream_url = stream_cmd["params"]["stream_url"]
    assert "/ws/telnyx/stream/" in stream_url, (
        f"Expected '/ws/telnyx/stream/' in URL, got: {stream_url!r}"
    )

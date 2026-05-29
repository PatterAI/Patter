"""Tests for the Plivo webhook handler and answer XML."""

from getpatter.providers.plivo_adapter import PlivoAdapter
from getpatter.telephony.plivo import plivo_webhook_handler

# ---------------------------------------------------------------------------
# plivo_webhook_handler
# ---------------------------------------------------------------------------


def test_plivo_webhook_generates_string():
    result = plivo_webhook_handler("CU123", "+39111", "+16592", "abc.ngrok.io")
    assert isinstance(result, str)


def test_plivo_webhook_is_stream_xml():
    """The answer XML opens a bidirectional <Stream>."""
    result = plivo_webhook_handler("CU123", "+1", "+2", "abc.ngrok.io")
    assert "<Response>" in result and "<Stream" in result
    assert 'bidirectional="true"' in result
    assert 'keepCallAlive="true"' in result


def test_plivo_webhook_pins_mulaw_8k():
    """Plivo is told to stream mulaw 8 kHz (so the bridge mirrors Twilio)."""
    result = plivo_webhook_handler("CU123", "+1", "+2", "abc.ngrok.io")
    assert "audio/x-mulaw;rate=8000" in result


def test_plivo_webhook_stream_url_is_wss():
    result = plivo_webhook_handler("CU456", "+1", "+2", "my.host.io")
    assert "wss://my.host.io" in result


def test_plivo_webhook_uses_plivo_ws_path():
    """Stream URL must point to the Plivo-specific WebSocket handler."""
    result = plivo_webhook_handler("CUNIQUE", "+1", "+2", "abc.ngrok.io")
    assert "/ws/plivo/stream/CUNIQUE" in result


def test_plivo_webhook_carries_caller_callee_in_query():
    """Caller / callee travel on the WSS query string (Plivo preserves it)."""
    result = plivo_webhook_handler("CU1", "+39111", "+16592", "abc.ngrok.io")
    # Query separators are XML-escaped inside the <Stream> text content.
    assert "caller=%2B39111" in result
    assert "callee=%2B16592" in result


def test_plivo_webhook_carries_extra_headers_fallback():
    """Caller / callee also travel via extraHeaders as a fallback channel."""
    result = plivo_webhook_handler("CU1", "+39111", "+16592", "abc.ngrok.io")
    assert "extraHeaders=" in result
    assert "X-PH-caller" in result


# ---------------------------------------------------------------------------
# PlivoAdapter.generate_stream_xml
# ---------------------------------------------------------------------------


def test_generate_stream_xml_url_is_text_content():
    """Unlike Twilio (url= attr), Plivo's WSS URL is the <Stream> text content."""
    xml = PlivoAdapter.generate_stream_xml("wss://h/ws/plivo/stream/x")
    assert ">wss://h/ws/plivo/stream/x</Stream>" in xml
    assert "url=" not in xml


def test_generate_stream_xml_escapes_query_ampersand():
    """The & between query params must become &amp; or Plivo truncates the URL."""
    xml = PlivoAdapter.generate_stream_xml(
        "wss://h/ws/plivo/stream/x?caller=%2B1&callee=%2B2"
    )
    assert "&amp;callee=%2B2" in xml
    assert "?caller=%2B1&amp;callee=%2B2" in xml


def test_generate_stream_xml_custom_content_type():
    xml = PlivoAdapter.generate_stream_xml(
        "wss://h/x", content_type="audio/x-l16;rate=16000"
    )
    assert 'contentType="audio/x-l16;rate=16000"' in xml


def test_generate_stream_xml_extra_headers():
    xml = PlivoAdapter.generate_stream_xml(
        "wss://h/x", extra_headers={"X-PH-caller": "+1", "X-PH-callee": "+2"}
    )
    assert "extraHeaders=" in xml
    assert "X-PH-caller=+1" in xml

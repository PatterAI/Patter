"""Unit tests for patter.handlers.twilio_handler — TwiML, validation, audio sender."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.handlers.twilio_handler import (
    TwilioAudioSender,
    _validate_twilio_sid,
    _xml_escape,
    twilio_webhook_handler,
)


# ---------------------------------------------------------------------------
# _validate_twilio_sid
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateTwilioSid:
    """Twilio SID format validation."""

    def test_valid_call_sid(self) -> None:
        sid = "CA" + "a" * 32
        assert _validate_twilio_sid(sid, "CA") is True

    def test_valid_account_sid(self) -> None:
        sid = "AC" + "0" * 32
        assert _validate_twilio_sid(sid, "AC") is True

    def test_wrong_prefix(self) -> None:
        sid = "XX" + "a" * 32
        assert _validate_twilio_sid(sid, "CA") is False

    def test_too_short(self) -> None:
        assert _validate_twilio_sid("CA" + "a" * 10, "CA") is False

    def test_too_long(self) -> None:
        assert _validate_twilio_sid("CA" + "a" * 33, "CA") is False

    def test_non_hex_chars(self) -> None:
        """Only hex characters after the 2-letter prefix."""
        sid = "CA" + "g" * 32  # 'g' is not hex
        assert _validate_twilio_sid(sid, "CA") is False

    def test_empty_string(self) -> None:
        assert _validate_twilio_sid("", "CA") is False


# ---------------------------------------------------------------------------
# _xml_escape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestXmlEscape:
    """XML special character escaping."""

    def test_ampersand(self) -> None:
        assert _xml_escape("A&B") == "A&amp;B"

    def test_less_than(self) -> None:
        assert _xml_escape("A<B") == "A&lt;B"

    def test_greater_than(self) -> None:
        assert _xml_escape("A>B") == "A&gt;B"

    def test_double_quote(self) -> None:
        assert _xml_escape('A"B') == "A&quot;B"

    def test_single_quote(self) -> None:
        assert _xml_escape("A'B") == "A&apos;B"

    def test_no_special_chars(self) -> None:
        assert _xml_escape("Hello World") == "Hello World"

    def test_multiple_special_chars(self) -> None:
        result = _xml_escape("<script>&'\"</script>")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&apos;" in result
        assert "&quot;" in result
        assert "&gt;" in result


# ---------------------------------------------------------------------------
# twilio_webhook_handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTwilioWebhookHandler:
    """twilio_webhook_handler generates valid TwiML."""

    @patch("patter.providers.twilio_adapter.TwilioAdapter")
    def test_generates_twiml(self, mock_adapter_cls) -> None:
        mock_adapter_cls.generate_stream_twiml.return_value = (
            '<?xml version="1.0"?><Response><Connect><Stream url="wss://host/ws/stream/CA123" /></Connect></Response>'
        )
        result = twilio_webhook_handler(
            call_sid="CA123",
            caller="+15551111111",
            callee="+15552222222",
            webhook_base_url="host.ngrok.io",
        )
        assert "<Response>" in result
        assert "<Stream" in result or "<Connect" in result
        mock_adapter_cls.generate_stream_twiml.assert_called_once()

    @patch("patter.providers.twilio_adapter.TwilioAdapter")
    def test_stream_url_includes_call_sid(self, mock_adapter_cls) -> None:
        mock_adapter_cls.generate_stream_twiml.return_value = "<Response/>"
        twilio_webhook_handler(
            call_sid="CA_test",
            caller="+1",
            callee="+2",
            webhook_base_url="example.com",
        )
        call_args = mock_adapter_cls.generate_stream_twiml.call_args
        stream_url = call_args[0][0] if call_args[0] else call_args[1].get("stream_url", "")
        assert "CA_test" in stream_url


# ---------------------------------------------------------------------------
# TwilioAudioSender
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTwilioAudioSender:
    """TwilioAudioSender transcoding and WebSocket messaging."""

    def _make_sender(self) -> tuple[TwilioAudioSender, AsyncMock]:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        with patch("patter.handlers.twilio_handler.pcm16_to_mulaw", create=True), \
             patch("patter.handlers.twilio_handler.resample_16k_to_8k", create=True):
            # Patch the imports within the constructor
            with patch(
                "patter.services.transcoding.pcm16_to_mulaw",
                side_effect=lambda x: x,
                create=True,
            ), patch(
                "patter.services.transcoding.resample_16k_to_8k",
                side_effect=lambda x: x,
                create=True,
            ):
                sender = TwilioAudioSender(ws, stream_sid="MZ_test")
        return sender, ws

    async def test_send_audio(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        mock_resample = MagicMock(side_effect=lambda x: x)
        mock_mulaw = MagicMock(side_effect=lambda x: x)

        with patch(
            "patter.services.transcoding.pcm16_to_mulaw",
            mock_mulaw,
            create=True,
        ), patch(
            "patter.services.transcoding.resample_16k_to_8k",
            mock_resample,
            create=True,
        ):
            sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        audio = b"\x00\x01\x02\x03"
        await sender.send_audio(audio)
        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["event"] == "media"
        assert payload["streamSid"] == "MZ_test"
        # Payload should be base64-encoded
        decoded = base64.b64decode(payload["media"]["payload"])
        assert decoded == audio  # identity mock

    async def test_send_clear(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        with patch(
            "patter.services.transcoding.pcm16_to_mulaw",
            lambda x: x,
            create=True,
        ), patch(
            "patter.services.transcoding.resample_16k_to_8k",
            lambda x: x,
            create=True,
        ):
            sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        await sender.send_clear()
        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["event"] == "clear"
        assert payload["streamSid"] == "MZ_test"

    async def test_send_mark_increments_count(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        with patch(
            "patter.services.transcoding.pcm16_to_mulaw",
            lambda x: x,
            create=True,
        ), patch(
            "patter.services.transcoding.resample_16k_to_8k",
            lambda x: x,
            create=True,
        ):
            sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        await sender.send_mark("m1")
        payload1 = json.loads(ws.send_text.call_args[0][0])
        assert payload1["mark"]["name"] == "audio_1"

        await sender.send_mark("m2")
        payload2 = json.loads(ws.send_text.call_args[0][0])
        assert payload2["mark"]["name"] == "audio_2"

    def test_on_mark_confirmed(self) -> None:
        ws = AsyncMock()
        with patch(
            "patter.services.transcoding.pcm16_to_mulaw",
            lambda x: x,
            create=True,
        ), patch(
            "patter.services.transcoding.resample_16k_to_8k",
            lambda x: x,
            create=True,
        ):
            sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        assert sender.last_confirmed_mark == ""
        sender.on_mark_confirmed("audio_1")
        assert sender.last_confirmed_mark == "audio_1"

"""Unit tests for getpatter.telephony.twilio — TwiML, validation, audio sender."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from getpatter.audio.transcoding import (
    PcmCarry,
    create_resampler_16k_to_8k,
    pcm16_to_mulaw,
)
from getpatter.telephony.twilio import (
    TwilioAudioSender,
    _validate_twilio_sid,
    _xml_escape,
    twilio_webhook_handler,
)


def _compute_expected_mulaw(audio: bytes) -> bytes:
    """Compute the expected wire bytes for a PCM16@16kHz chunk.

    Mirrors the exact chain inside TwilioAudioSender.send_audio:
    PcmCarry.align → StatefulResampler.process → pcm16_to_mulaw.
    Use a fresh carry+resampler pair so IIR filter state is cold, matching
    a newly-constructed sender.
    """
    carry = PcmCarry()
    resampler = create_resampler_16k_to_8k()
    aligned = carry.align(audio)
    if not aligned:
        return b""
    resampled = resampler.process(aligned)
    return pcm16_to_mulaw(resampled)


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

    @patch("getpatter.providers.twilio_adapter.TwilioAdapter")
    def test_generates_twiml(self, mock_adapter_cls) -> None:
        mock_adapter_cls.generate_stream_twiml.return_value = '<?xml version="1.0"?><Response><Connect><Stream url="wss://host/ws/stream/CA123" /></Connect></Response>'
        result = twilio_webhook_handler(
            call_sid="CA123",
            caller="+15551111111",
            callee="+15552222222",
            webhook_base_url="host.ngrok.io",
        )
        assert "<Response>" in result
        assert "<Stream" in result or "<Connect" in result
        mock_adapter_cls.generate_stream_twiml.assert_called_once()

    @patch("getpatter.providers.twilio_adapter.TwilioAdapter")
    def test_stream_url_includes_call_sid(self, mock_adapter_cls) -> None:
        mock_adapter_cls.generate_stream_twiml.return_value = "<Response/>"
        twilio_webhook_handler(
            call_sid="CA_test",
            caller="+1",
            callee="+2",
            webhook_base_url="example.com",
        )
        call_args = mock_adapter_cls.generate_stream_twiml.call_args
        stream_url = (
            call_args[0][0] if call_args[0] else call_args[1].get("stream_url", "")
        )
        assert "CA_test" in stream_url


# ---------------------------------------------------------------------------
# TwilioAudioSender
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTwilioAudioSender:
    """TwilioAudioSender transcoding and WebSocket messaging.

    Tests use the real pcm16_to_mulaw and create_resampler_16k_to_8k functions
    (no mocking of internal transcoding path) so the encoded wire payload is
    actually verified to be correct mulaw output. Per the authentic-tests rule,
    mocking internal module helpers is prohibited; only paid/external boundaries
    may be mocked.
    """

    def _make_sender(self) -> tuple[TwilioAudioSender, AsyncMock]:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")
        return sender, ws

    async def test_send_audio(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        # 4 bytes = 2 PCM16 samples @ 16 kHz (even-length, no carry)
        audio = b"\x00\x01\x02\x03"
        expected_mulaw = _compute_expected_mulaw(audio)

        await sender.send_audio(audio)

        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["event"] == "media"
        assert payload["streamSid"] == "MZ_test"
        decoded = base64.b64decode(payload["media"]["payload"])
        # Real 16kHz→8kHz downsample + mulaw encoding — result differs from raw input
        assert decoded == expected_mulaw

    async def test_send_clear(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        await sender.send_clear()
        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["event"] == "clear"
        assert payload["streamSid"] == "MZ_test"

    async def test_send_mark_increments_count(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        await sender.send_mark("m1")
        payload1 = json.loads(ws.send_text.call_args[0][0])
        assert payload1["mark"]["name"] == "audio_1"

        await sender.send_mark("m2")
        payload2 = json.loads(ws.send_text.call_args[0][0])
        assert payload2["mark"]["name"] == "audio_2"

    def test_on_mark_confirmed(self) -> None:
        ws = AsyncMock()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        assert sender.last_confirmed_mark == ""
        sender.on_mark_confirmed("audio_1")
        assert sender.last_confirmed_mark == "audio_1"

    async def test_reset_pcm_carry_drops_odd_byte(self) -> None:
        """``reset_pcm_carry`` must discard the buffered odd byte so the next
        TTS synthesis starts aligned. Parity with TS ``ttsByteCarry = null``
        at every synth boundary."""
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        # Push an odd-length chunk — the last byte is buffered into carry by
        # the real PcmCarry instance inside the sender.
        await sender.send_audio(b"\x00\x01\x02")
        assert sender._pcm_carry._carry == b"\x02"

        # Reset drops the carry so the next synth starts aligned
        sender.reset_pcm_carry()
        assert sender._pcm_carry._carry == b""

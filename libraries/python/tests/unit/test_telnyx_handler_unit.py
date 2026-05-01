"""Unit tests for getpatter.handlers.telnyx_handler — webhook response, audio sender."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.handlers.telnyx_handler import (
    TelnyxAudioSender,
    _MAX_WS_MESSAGE_BYTES,
    telnyx_webhook_handler,
)


# ---------------------------------------------------------------------------
# telnyx_webhook_handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelnyxWebhookHandler:
    """telnyx_webhook_handler generates Call Control commands."""

    def test_returns_answer_and_stream_start(self) -> None:
        result = telnyx_webhook_handler(
            call_id="v3:test-id",
            caller="+15551111111",
            callee="+15552222222",
            webhook_base_url="host.ngrok.io",
        )
        commands = result["commands"]
        assert len(commands) == 2
        assert commands[0]["command"] == "answer"
        assert commands[1]["command"] == "stream_start"

    def test_stream_url_format(self) -> None:
        result = telnyx_webhook_handler(
            call_id="v3:abc123",
            caller="+15551111111",
            callee="+15552222222",
            webhook_base_url="example.com",
        )
        stream_params = result["commands"][1]["params"]
        stream_url = stream_params["stream_url"]
        assert stream_url.startswith("wss://example.com/ws/telnyx/stream/v3:abc123")
        assert "caller=" in stream_url
        assert "callee=" in stream_url

    def test_stream_track_inbound_only(self) -> None:
        # ``inbound_track`` halves WS upstream bandwidth and avoids the
        # outbound-echo we used to filter on receive.
        result = telnyx_webhook_handler(
            call_id="id",
            caller="+1",
            callee="+2",
            webhook_base_url="host",
        )
        params = result["commands"][1]["params"]
        assert params["stream_track"] == "inbound_track"

    def test_connection_id_optional(self) -> None:
        """connection_id is accepted but does not affect the output."""
        result1 = telnyx_webhook_handler(
            call_id="id", caller="+1", callee="+2", webhook_base_url="host",
        )
        result2 = telnyx_webhook_handler(
            call_id="id",
            caller="+1",
            callee="+2",
            webhook_base_url="host",
            connection_id="conn-123",
        )
        assert result1["commands"][0] == result2["commands"][0]


# ---------------------------------------------------------------------------
# TelnyxAudioSender
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelnyxAudioSender:
    """TelnyxAudioSender — no transcoding, direct 16 kHz PCM."""

    async def test_send_audio(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        # ``input_is_mulaw_8k=True`` forces pass-through — the default
        # behaviour transcodes PCM16 → mulaw (BUG #18), which would alter
        # the byte stream and break this round-trip assertion.
        sender = TelnyxAudioSender(ws, input_is_mulaw_8k=True)

        audio = b"\x00\x01\x02\x03"
        await sender.send_audio(audio)

        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        # Telnyx media-stream wire format (BUG #18).
        assert payload["event"] == "media"
        decoded = base64.b64decode(payload["media"]["payload"])
        assert decoded == audio

    async def test_send_clear(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TelnyxAudioSender(ws)

        await sender.send_clear()
        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["event"] == "clear"

    async def test_send_mark_is_noop(self) -> None:
        """Telnyx does not support playback marks — send_mark is a no-op."""
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TelnyxAudioSender(ws)

        await sender.send_mark("test_mark")
        ws.send_text.assert_not_awaited()

    async def test_send_audio_base64_roundtrip(self) -> None:
        """Verify base64 encoding round-trips correctly."""
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        # Pass-through mode so the output bytes match the input exactly.
        sender = TelnyxAudioSender(ws, input_is_mulaw_8k=True)

        original_audio = bytes(range(256))
        await sender.send_audio(original_audio)

        payload = json.loads(ws.send_text.call_args[0][0])
        decoded = base64.b64decode(payload["media"]["payload"])
        assert decoded == original_audio


# ---------------------------------------------------------------------------
# Max WebSocket message size constant
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConstants:
    """Module-level constants."""

    def test_max_ws_message_bytes(self) -> None:
        assert _MAX_WS_MESSAGE_BYTES == 1 * 1024 * 1024

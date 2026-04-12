"""Unit tests for patter.handlers.telnyx_handler — webhook response, audio sender."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from patter.handlers.telnyx_handler import (
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

    def test_stream_track_both(self) -> None:
        result = telnyx_webhook_handler(
            call_id="id",
            caller="+1",
            callee="+2",
            webhook_base_url="host",
        )
        params = result["commands"][1]["params"]
        assert params["stream_track"] == "both_tracks"

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
        sender = TelnyxAudioSender(ws)

        audio = b"\x00\x01\x02\x03"
        await sender.send_audio(audio)

        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["event_type"] == "media"
        decoded = base64.b64decode(payload["payload"]["audio"]["chunk"])
        assert decoded == audio

    async def test_send_clear(self) -> None:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TelnyxAudioSender(ws)

        await sender.send_clear()
        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["event_type"] == "media_stop"

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
        sender = TelnyxAudioSender(ws)

        original_audio = bytes(range(256))
        await sender.send_audio(original_audio)

        payload = json.loads(ws.send_text.call_args[0][0])
        decoded = base64.b64decode(payload["payload"]["audio"]["chunk"])
        assert decoded == original_audio


# ---------------------------------------------------------------------------
# Max WebSocket message size constant
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConstants:
    """Module-level constants."""

    def test_max_ws_message_bytes(self) -> None:
        assert _MAX_WS_MESSAGE_BYTES == 1 * 1024 * 1024

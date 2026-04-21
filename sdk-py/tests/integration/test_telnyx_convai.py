"""Integration test: Telnyx x ElevenLabs ConvAI provider mode.

Simulates an inbound Telnyx call flowing through the WebSocket bridge
with a mocked ElevenLabs ConvAI handler. No real network calls.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.handlers.telnyx_handler import telnyx_stream_bridge, TelnyxAudioSender

from tests.conftest import fake_pcm_frame, make_agent

_PATCH_CONVAI = "patter.handlers.telnyx_handler.ElevenLabsConvAIStreamHandler"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws_mock(events: list[dict]) -> AsyncMock:
    ws = AsyncMock()
    ws.query_params = {"caller": "+15551111111", "callee": "+15552222222"}
    idx = 0

    async def _receive_text():
        nonlocal idx
        if idx < len(events):
            data = json.dumps(events[idx])
            idx += 1
            return data
        raise Exception("Stream ended")

    ws.receive_text = AsyncMock(side_effect=_receive_text)
    ws.send_text = AsyncMock()
    ws.accept = AsyncMock()
    return ws


def _telnyx_stream_started(call_control_id: str = "v3:convai-id") -> dict:
    # Telnyx media-stream wire format (BUG #17/#18): ``event: "start"`` with a
    # ``start`` dict carrying call_control_id + caller/callee.
    return {
        "event": "start",
        "start": {
            "call_control_id": call_control_id,
            "from": "+15551111111",
            "to": "+15552222222",
        },
    }


def _telnyx_media_event() -> dict:
    # Wire format: ``{"event":"media","media":{"payload":b64}}`` — BUG #18.
    return {
        "event": "media",
        "media": {"payload": base64.b64encode(fake_pcm_frame()).decode()},
    }


def _telnyx_stream_stopped() -> dict:
    return {"event": "stop"}


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTelnyxConvAI:
    """Telnyx + ElevenLabs ConvAI: inbound call lifecycle."""

    @patch(_PATCH_CONVAI)
    async def test_inbound_call_lifecycle(self, MockHandler) -> None:
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="elevenlabs_convai")

        ws = _make_ws_mock([
            _telnyx_stream_started(),
            _telnyx_media_event(),
            _telnyx_stream_stopped(),
        ])

        on_call_end = AsyncMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="",
            elevenlabs_key="el_test",
            on_call_end=on_call_end,
        )

        ws.accept.assert_awaited_once()
        MockHandler.assert_called_once()
        handler_instance.start.assert_awaited_once()
        handler_instance.on_audio_received.assert_awaited_once()
        handler_instance.cleanup.assert_awaited_once()
        on_call_end.assert_awaited_once()

    @patch(_PATCH_CONVAI)
    async def test_correct_handler_for_convai(self, MockHandler) -> None:
        """Verify ElevenLabsConvAIStreamHandler is chosen."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="elevenlabs_convai")

        ws = _make_ws_mock([_telnyx_stream_started(), _telnyx_stream_stopped()])

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="",
            elevenlabs_key="el_key",
        )

        call_kwargs = MockHandler.call_args.kwargs
        assert call_kwargs.get("elevenlabs_key") == "el_key"

    @patch(_PATCH_CONVAI)
    async def test_metrics_configured_for_telnyx(self, MockHandler) -> None:
        """Verify metrics are configured with Telnyx PCM format (16kHz, 2 bytes)."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="elevenlabs_convai")

        ws = _make_ws_mock([_telnyx_stream_started(), _telnyx_stream_stopped()])

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="",
            elevenlabs_key="el_key",
        )

        call_kwargs = MockHandler.call_args.kwargs
        metrics = call_kwargs.get("metrics")
        assert metrics is not None
        assert metrics._stt_sample_rate == 16000
        assert metrics._stt_bytes_per_sample == 2

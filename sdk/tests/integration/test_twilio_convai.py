"""Integration test: Twilio x ElevenLabs ConvAI provider mode.

Simulates an inbound Twilio call flowing through the WebSocket bridge
with a mocked ElevenLabs ConvAI handler. No real network calls.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.handlers.twilio_handler import twilio_stream_bridge, TwilioAudioSender

from tests.conftest import fake_mulaw_frame, make_agent

_PATCH_CONVAI = "patter.handlers.twilio_handler.ElevenLabsConvAIStreamHandler"


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


def _twilio_start_event(call_sid: str = "CA" + "b" * 32) -> dict:
    return {
        "event": "start",
        "streamSid": "MZ_convai",
        "start": {"callSid": call_sid, "customParameters": {}},
    }


def _twilio_media_event() -> dict:
    return {
        "event": "media",
        "media": {"payload": base64.b64encode(fake_mulaw_frame()).decode()},
    }


def _twilio_stop_event() -> dict:
    return {"event": "stop"}


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTwilioConvAI:
    """Twilio + ElevenLabs ConvAI: inbound call lifecycle."""

    @patch(_PATCH_CONVAI)
    async def test_inbound_call_lifecycle(self, MockHandler) -> None:
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_mark = AsyncMock()
        handler_instance.on_dtmf = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="elevenlabs_convai")

        ws = _make_ws_mock([
            _twilio_start_event(),
            _twilio_media_event(),
            _twilio_stop_event(),
        ])

        on_call_end = AsyncMock()

        await twilio_stream_bridge(
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
    async def test_correct_handler_instantiated(self, MockHandler) -> None:
        """Verify ElevenLabsConvAIStreamHandler is chosen for elevenlabs_convai."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="elevenlabs_convai")

        ws = _make_ws_mock([_twilio_start_event(), _twilio_stop_event()])

        await twilio_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="",
            elevenlabs_key="el_test",
        )

        MockHandler.assert_called_once()
        call_kwargs = MockHandler.call_args.kwargs
        assert call_kwargs.get("elevenlabs_key") == "el_test"

"""Integration test: Twilio x Pipeline provider mode.

Simulates an inbound Twilio call flowing through the WebSocket bridge
with a mocked Pipeline handler (STT + LLM + TTS). No real network calls.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.handlers.twilio_handler import twilio_stream_bridge, TwilioAudioSender

from tests.conftest import fake_mulaw_frame, make_agent

_PATCH_PIPELINE = "getpatter.handlers.twilio_handler.PipelineStreamHandler"


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


def _twilio_start_event(call_sid: str = "CA" + "c" * 32) -> dict:
    return {
        "event": "start",
        "streamSid": "MZ_pipeline",
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
class TestTwilioPipeline:
    """Twilio + Pipeline (STT + LLM + TTS): inbound call lifecycle."""

    @patch(_PATCH_PIPELINE)
    async def test_inbound_call_lifecycle(self, MockHandler) -> None:
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_mark = AsyncMock()
        handler_instance.on_dtmf = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="pipeline")

        ws = _make_ws_mock([
            _twilio_start_event(),
            _twilio_media_event(),
            _twilio_media_event(),
            _twilio_media_event(),
            _twilio_stop_event(),
        ])

        on_call_start = AsyncMock(return_value=None)
        on_call_end = AsyncMock()
        on_message = AsyncMock(return_value="I hear you.")

        await twilio_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            deepgram_key="dg_test",
            elevenlabs_key="el_test",
            on_call_start=on_call_start,
            on_call_end=on_call_end,
            on_message=on_message,
        )

        ws.accept.assert_awaited_once()
        MockHandler.assert_called_once()
        handler_instance._run_with_scope.assert_awaited_once()
        assert handler_instance.on_audio_received.await_count == 3
        handler_instance.cleanup.assert_awaited_once()

    @patch(_PATCH_PIPELINE)
    async def test_pipeline_handler_gets_correct_keys(self, MockHandler) -> None:
        """Verify PipelineStreamHandler receives deepgram and elevenlabs keys."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="pipeline")

        ws = _make_ws_mock([_twilio_start_event(), _twilio_stop_event()])

        await twilio_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            deepgram_key="dg_test",
            elevenlabs_key="el_test",
        )

        call_kwargs = MockHandler.call_args.kwargs
        assert call_kwargs.get("deepgram_key") == "dg_test"
        assert call_kwargs.get("elevenlabs_key") == "el_test"
        assert call_kwargs.get("for_twilio") is True

    @patch(_PATCH_PIPELINE)
    async def test_pipeline_on_message_passed(self, MockHandler) -> None:
        """Verify on_message handler is forwarded to PipelineStreamHandler."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="pipeline")
        on_msg = AsyncMock(return_value="response")

        ws = _make_ws_mock([_twilio_start_event(), _twilio_stop_event()])

        await twilio_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            deepgram_key="dg_test",
            elevenlabs_key="el_test",
            on_message=on_msg,
        )

        call_kwargs = MockHandler.call_args.kwargs
        assert call_kwargs.get("on_message") is on_msg

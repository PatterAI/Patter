"""Integration test: Telnyx x Pipeline provider mode.

Simulates an inbound Telnyx call flowing through the WebSocket bridge
with a mocked Pipeline handler (STT + LLM + TTS). No real network calls.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.handlers.telnyx_handler import telnyx_stream_bridge, TelnyxAudioSender

from tests.conftest import fake_pcm_frame, make_agent

_PATCH_PIPELINE = "patter.handlers.telnyx_handler.PipelineStreamHandler"


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


def _telnyx_stream_started(call_control_id: str = "v3:pipeline-id") -> dict:
    # Telnyx media-stream wire format (BUG #17/#18).
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
class TestTelnyxPipeline:
    """Telnyx + Pipeline (STT + LLM + TTS): inbound call lifecycle."""

    @patch(_PATCH_PIPELINE)
    async def test_inbound_call_lifecycle(self, MockHandler) -> None:
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="pipeline")

        ws = _make_ws_mock([
            _telnyx_stream_started(),
            _telnyx_media_event(),
            _telnyx_media_event(),
            _telnyx_stream_stopped(),
        ])

        on_call_start = AsyncMock(return_value=None)
        on_call_end = AsyncMock()
        on_message = AsyncMock(return_value="Got it.")

        await telnyx_stream_bridge(
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
        handler_instance.start.assert_awaited_once()
        assert handler_instance.on_audio_received.await_count == 2
        handler_instance.cleanup.assert_awaited_once()
        on_call_start.assert_awaited_once()
        on_call_end.assert_awaited_once()

    @patch(_PATCH_PIPELINE)
    async def test_pipeline_not_for_twilio(self, MockHandler) -> None:
        """Telnyx pipeline sets for_twilio=False."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="pipeline")

        ws = _make_ws_mock([_telnyx_stream_started(), _telnyx_stream_stopped()])

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            deepgram_key="dg_test",
            elevenlabs_key="el_test",
        )

        call_kwargs = MockHandler.call_args.kwargs
        assert call_kwargs.get("for_twilio") is False

    @patch(_PATCH_PIPELINE)
    async def test_pipeline_keys_forwarded(self, MockHandler) -> None:
        """Verify deepgram and elevenlabs keys reach the handler."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="pipeline")

        ws = _make_ws_mock([_telnyx_stream_started(), _telnyx_stream_stopped()])

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            deepgram_key="dg_key",
            elevenlabs_key="el_key",
        )

        call_kwargs = MockHandler.call_args.kwargs
        assert call_kwargs.get("deepgram_key") == "dg_key"
        assert call_kwargs.get("elevenlabs_key") == "el_key"

    @patch(_PATCH_PIPELINE)
    async def test_cleanup_on_disconnect(self, MockHandler) -> None:
        """Handler cleanup is called even when the stream ends abruptly."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="pipeline")

        call_count = 0
        async def _disconnect_after_start():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return json.dumps(_telnyx_stream_started())
            raise Exception("Connection lost")

        ws = AsyncMock()
        ws.query_params = {"caller": "+1", "callee": "+2"}
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=_disconnect_after_start)

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            deepgram_key="dg_test",
            elevenlabs_key="el_test",
        )

        handler_instance.cleanup.assert_awaited_once()

"""Integration test: Telnyx x OpenAI Realtime provider mode.

Simulates an inbound Telnyx call flowing through the WebSocket bridge
with a mocked OpenAI Realtime handler. No real network calls.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.telephony.telnyx import telnyx_stream_bridge, TelnyxAudioSender

from tests.conftest import fake_pcm_frame, make_agent

# telnyx_handler imports these from stream_handler at module level
_PATCH_RT = "getpatter.telephony.telnyx.OpenAIRealtimeStreamHandler"
_PATCH_CONVAI = "getpatter.telephony.telnyx.ElevenLabsConvAIStreamHandler"
_PATCH_PIPELINE = "getpatter.telephony.telnyx.PipelineStreamHandler"


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


def _telnyx_stream_started(call_control_id: str = "v3:test-id") -> dict:
    # Telnyx media-stream wire format (BUG #17/#18).
    return {
        "event": "start",
        "start": {
            "call_control_id": call_control_id,
            "from": "+15551111111",
            "to": "+15552222222",
        },
    }


def _telnyx_media_event(audio: bytes = b"") -> dict:
    if not audio:
        audio = fake_pcm_frame()
    # Wire format: ``{"event":"media","media":{"payload":b64}}`` — BUG #18.
    return {
        "event": "media",
        "media": {"payload": base64.b64encode(audio).decode()},
    }


def _telnyx_stream_stopped() -> dict:
    return {"event": "stop"}


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTelnyxRealtime:
    """Telnyx + OpenAI Realtime: inbound call lifecycle."""

    @patch(_PATCH_RT)
    async def test_inbound_call_lifecycle(self, MockHandler) -> None:
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="openai_realtime")
        audio_frame = fake_pcm_frame()

        ws = _make_ws_mock([
            _telnyx_stream_started(),
            _telnyx_media_event(audio_frame),
            _telnyx_media_event(audio_frame),
            _telnyx_stream_stopped(),
        ])

        on_call_start = AsyncMock(return_value=None)
        on_call_end = AsyncMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )

        ws.accept.assert_awaited_once()
        MockHandler.assert_called_once()
        handler_instance.start.assert_awaited_once()
        assert handler_instance.on_audio_received.await_count == 2
        handler_instance.cleanup.assert_awaited_once()
        on_call_start.assert_awaited_once()
        on_call_end.assert_awaited_once()

    @patch(_PATCH_RT)
    async def test_audio_format_g711_ulaw(self, MockHandler) -> None:
        """Telnyx Call Control streams are PCMU 8 kHz bidirectional (BUG #19).

        The Realtime handler therefore runs on ``g711_ulaw`` so both legs are
        pass-through — transcoding PCM16 would misinterpret the bytes and
        break turn detection.
        """
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="openai_realtime")

        ws = _make_ws_mock([_telnyx_stream_started(), _telnyx_stream_stopped()])

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
        )

        call_kwargs = MockHandler.call_args.kwargs
        assert call_kwargs.get("audio_format") == "g711_ulaw"

    @patch(_PATCH_RT)
    async def test_empty_audio_chunk_skipped(self, MockHandler) -> None:
        """Media events with empty audio chunks are ignored."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="openai_realtime")

        empty_media = {
            "event": "media",
            "media": {"payload": ""},
        }

        ws = _make_ws_mock([
            _telnyx_stream_started(),
            empty_media,
            _telnyx_stream_stopped(),
        ])

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
        )

        handler_instance.on_audio_received.assert_not_awaited()

    @patch(_PATCH_RT)
    async def test_call_overrides_applied(self, MockHandler) -> None:
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TelnyxAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="openai_realtime", voice="alloy")

        ws = _make_ws_mock([_telnyx_stream_started(), _telnyx_stream_stopped()])
        on_call_start = AsyncMock(return_value={"voice": "echo"})

        await telnyx_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            on_call_start=on_call_start,
        )

        passed_agent = MockHandler.call_args.kwargs.get("agent")
        assert passed_agent.voice == "echo"

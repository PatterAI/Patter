"""Integration test: Twilio x OpenAI Realtime provider mode.

Simulates an inbound Twilio call flowing through the WebSocket bridge
with a mocked OpenAI Realtime adapter. No real network calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.handlers.twilio_handler import twilio_stream_bridge, TwilioAudioSender
from getpatter.models import Agent

from tests.conftest import fake_mulaw_frame, make_agent

# The bridge function references these via `from getpatter.handlers.stream_handler import ...`
# so we must patch them in twilio_handler's namespace.
_PATCH_RT = "getpatter.handlers.twilio_handler.OpenAIRealtimeStreamHandler"
_PATCH_CONVAI = "getpatter.handlers.twilio_handler.ElevenLabsConvAIStreamHandler"
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


def _twilio_start_event(call_sid: str = "CA" + "a" * 32, stream_sid: str = "MZ_test") -> dict:
    return {
        "event": "start",
        "streamSid": stream_sid,
        "start": {"callSid": call_sid, "customParameters": {}},
    }


def _twilio_media_event(audio: bytes = b"") -> dict:
    if not audio:
        audio = fake_mulaw_frame()
    return {
        "event": "media",
        "media": {"payload": base64.b64encode(audio).decode()},
    }


def _twilio_stop_event() -> dict:
    return {"event": "stop"}


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTwilioRealtime:
    """Twilio + OpenAI Realtime: inbound call lifecycle."""

    @patch(_PATCH_RT)
    async def test_inbound_call_lifecycle(self, MockHandler) -> None:
        """Start -> media -> stop: handler is created, receives audio, cleans up."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_mark = AsyncMock()
        handler_instance.on_dtmf = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="openai_realtime")
        call_sid = "CA" + "a" * 32
        audio_frame = fake_mulaw_frame()

        ws = _make_ws_mock([
            _twilio_start_event(call_sid=call_sid),
            _twilio_media_event(audio_frame),
            _twilio_media_event(audio_frame),
            _twilio_stop_event(),
        ])

        on_call_start = AsyncMock(return_value=None)
        on_call_end = AsyncMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )

        ws.accept.assert_awaited_once()
        MockHandler.assert_called_once()
        handler_instance._run_with_scope.assert_awaited_once()
        assert handler_instance.on_audio_received.await_count == 2
        handler_instance.cleanup.assert_awaited_once()
        on_call_start.assert_awaited_once()
        on_call_end.assert_awaited_once()

    @patch(_PATCH_RT)
    async def test_call_overrides_applied(self, MockHandler) -> None:
        """on_call_start returning overrides changes the agent config."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="openai_realtime", voice="alloy")

        ws = _make_ws_mock([_twilio_start_event(), _twilio_stop_event()])
        on_call_start = AsyncMock(return_value={"voice": "echo", "language": "it"})

        await twilio_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
            on_call_start=on_call_start,
        )

        MockHandler.assert_called_once()
        call_kwargs = MockHandler.call_args
        passed_agent = call_kwargs.kwargs.get("agent") or call_kwargs[1].get("agent")
        assert passed_agent.voice == "echo"
        assert passed_agent.language == "it"

    @patch(_PATCH_RT)
    async def test_dtmf_forwarded(self, MockHandler) -> None:
        """DTMF events are forwarded to the handler."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.on_dtmf = AsyncMock()
        handler_instance.on_mark = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="openai_realtime")

        ws = _make_ws_mock([
            _twilio_start_event(),
            {"event": "dtmf", "dtmf": {"digit": "5"}},
            _twilio_stop_event(),
        ])

        await twilio_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
        )

        handler_instance.on_dtmf.assert_awaited_once_with("5")

    @patch(_PATCH_RT)
    async def test_oversized_message_dropped(self, MockHandler) -> None:
        """Messages exceeding _MAX_WS_MESSAGE_BYTES are dropped."""
        handler_instance = AsyncMock()
        handler_instance.start = AsyncMock()
        handler_instance.cleanup = AsyncMock()
        handler_instance.on_audio_received = AsyncMock()
        handler_instance.audio_sender = MagicMock(spec=TwilioAudioSender)
        MockHandler.return_value = handler_instance

        agent = make_agent(provider="openai_realtime")

        events = [
            json.dumps(_twilio_start_event()),
            "x" * (2 * 1024 * 1024),  # oversized
            json.dumps(_twilio_stop_event()),
        ]

        ws = AsyncMock()
        ws.query_params = {"caller": "+1", "callee": "+2"}
        ws.accept = AsyncMock()

        idx = 0
        async def _recv():
            nonlocal idx
            if idx < len(events):
                data = events[idx]
                idx += 1
                return data
            raise Exception("done")
        ws.receive_text = AsyncMock(side_effect=_recv)

        await twilio_stream_bridge(
            websocket=ws,
            agent=agent,
            openai_key="sk-test",
        )

        handler_instance.on_audio_received.assert_not_awaited()

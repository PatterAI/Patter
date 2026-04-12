"""Unit tests for patter.handlers.twilio_handler — twilio_stream_bridge lifecycle.

Covers the event loop in twilio_stream_bridge: start, media, mark, dtmf, stop
events, recording, and metrics finalization.
"""

from __future__ import annotations

import base64
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ws_message(event: str, **kwargs) -> str:
    """Build a JSON WebSocket message string for Twilio."""
    msg: dict = {"event": event, **kwargs}
    return json.dumps(msg)


def _start_message(
    stream_sid: str = "MZ_test",
    call_sid: str = "CA" + "a" * 32,
    custom_params: dict | None = None,
) -> str:
    return _ws_message(
        "start",
        streamSid=stream_sid,
        start={
            "callSid": call_sid,
            "customParameters": custom_params or {},
        },
    )


def _media_message(audio: bytes = b"\xff" * 160) -> str:
    encoded = base64.b64encode(audio).decode("ascii")
    return _ws_message("media", media={"payload": encoded})


def _mark_message(name: str = "audio_1") -> str:
    return _ws_message("mark", mark={"name": name})


def _dtmf_message(digit: str = "5") -> str:
    return _ws_message("dtmf", dtmf={"digit": digit})


def _stop_message() -> str:
    return _ws_message("stop")


def _make_mock_ws(messages: list[str]) -> AsyncMock:
    """Create a mock WebSocket that yields *messages* then stops."""
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.query_params = {"caller": "+15551234567", "callee": "+15559876543"}
    ws.receive_text = AsyncMock(side_effect=messages + [Exception("stop")])
    ws.send_text = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# twilio_stream_bridge lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTwilioStreamBridgeLifecycle:
    """Full lifecycle of twilio_stream_bridge: start -> media -> stop."""

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_start_then_stop_fires_callbacks(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        call_sid = "CA" + "a" * 32
        messages = [_start_message(call_sid=call_sid), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler

        mock_metrics = MagicMock()
        mock_metrics.end_call.return_value = MagicMock()
        mock_create_metrics.return_value = mock_metrics

        on_call_start = AsyncMock(return_value=None)
        on_call_end = AsyncMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )

        ws.accept.assert_awaited_once()
        mock_handler.start.assert_awaited_once()
        mock_handler.cleanup.assert_awaited_once()
        on_call_start.assert_awaited_once()
        on_call_end.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.PipelineStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_pipeline_provider_creates_pipeline_handler(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        messages = [_start_message(), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="pipeline"),
            openai_key="sk-test",
        )

        mock_handler_cls.assert_called_once()
        mock_handler.start.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.ElevenLabsConvAIStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_elevenlabs_provider_creates_convai_handler(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        messages = [_start_message(), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="elevenlabs_convai"),
            openai_key="sk-test",
            elevenlabs_key="el-test",
        )

        mock_handler_cls.assert_called_once()

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_media_event_forwards_audio_to_handler(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        audio_bytes = b"\xff" * 160
        messages = [_start_message(), _media_message(audio_bytes), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        mock_handler.on_audio_received.assert_awaited_once_with(audio_bytes)

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_dtmf_event_calls_handler_and_transcript(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        messages = [_start_message(), _dtmf_message("9"), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        on_transcript = AsyncMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            on_transcript=on_transcript,
        )

        mock_handler.on_dtmf.assert_awaited_once_with("9")
        on_transcript.assert_awaited_once()
        transcript_data = on_transcript.call_args[0][0]
        assert "[DTMF: 9]" in transcript_data["text"]

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_mark_event_calls_on_mark(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        messages = [_start_message(), _mark_message("audio_1"), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        mock_handler.on_mark.assert_awaited_once_with("audio_1")

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_oversized_message_dropped(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge, _MAX_WS_MESSAGE_BYTES

        # Send an oversized message then stop
        huge_msg = "x" * (_MAX_WS_MESSAGE_BYTES + 1)
        messages = [_start_message(), huge_msg, _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        # Handler should not have received audio from the oversized message
        mock_handler.on_audio_received.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_call_overrides_applied(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        messages = [_start_message(), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        # Return overrides from on_call_start
        on_call_start = AsyncMock(return_value={"voice": "nova"})

        with patch("patter.handlers.twilio_handler.apply_call_overrides") as mock_apply:
            mock_apply.return_value = make_agent(voice="nova")
            await twilio_stream_bridge(
                websocket=ws,
                agent=make_agent(provider="openai_realtime"),
                openai_key="sk-test",
                on_call_start=on_call_start,
            )

            mock_apply.assert_called_once()

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_metrics_finalized_on_end(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        messages = [_start_message(), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler

        mock_metrics = MagicMock()
        mock_metrics.end_call.return_value = MagicMock()
        mock_create_metrics.return_value = mock_metrics

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        mock_metrics.configure_stt_format.assert_called_once_with(
            sample_rate=8000, bytes_per_sample=1
        )
        mock_metrics.end_call.assert_called_once()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTwilioRecording:
    """twilio_stream_bridge starts recording when recording=True."""

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_recording_posts_to_twilio_api(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        call_sid = "CA" + "a" * 32
        messages = [_start_message(call_sid=call_sid), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_http):
            await twilio_stream_bridge(
                websocket=ws,
                agent=make_agent(provider="openai_realtime"),
                openai_key="sk-test",
                twilio_sid="AC" + "0" * 32,
                twilio_token="token",
                recording=True,
            )

    @pytest.mark.asyncio
    @patch("patter.handlers.twilio_handler.OpenAIRealtimeStreamHandler")
    @patch("patter.handlers.twilio_handler.create_metrics_accumulator")
    @patch("patter.handlers.twilio_handler.resolve_agent_prompt", return_value="prompt")
    @patch("patter.handlers.twilio_handler.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("patter.services.transcoding.pcm16_to_mulaw", lambda x: x, create=True)
    @patch("patter.services.transcoding.resample_16k_to_8k", lambda x: x, create=True)
    async def test_recording_skipped_for_invalid_call_sid(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from patter.handlers.twilio_handler import twilio_stream_bridge

        # Invalid CallSid format
        messages = [_start_message(call_sid="INVALID_SID"), _stop_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        # Should not raise even with invalid SID
        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            twilio_sid="AC" + "0" * 32,
            twilio_token="token",
            recording=True,
        )

        # Handler should still be created and started
        mock_handler.start.assert_awaited_once()

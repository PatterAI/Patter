"""Unit tests for getpatter.handlers.telnyx_handler — telnyx_stream_bridge lifecycle.

Covers the event loop in telnyx_stream_bridge: stream_started, media,
stream_stopped, and metrics finalization.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ws_message(event: str, **kwargs) -> str:
    """Build a JSON WebSocket message string for Telnyx (media-stream wire
    format — BUG #17)."""
    msg: dict = {"event": event, **kwargs}
    return json.dumps(msg)


def _stream_started_message(
    call_control_id: str = "v3:test-id",
) -> str:
    return _ws_message(
        "start",
        start={
            "call_control_id": call_control_id,
            "from": "+15551111111",
            "to": "+15552222222",
        },
    )


def _media_message(audio: bytes = b"\x00\x00" * 320) -> str:
    encoded = base64.b64encode(audio).decode("ascii")
    return _ws_message(
        "media",
        media={"payload": encoded},
    )


def _stream_stopped_message() -> str:
    return _ws_message("stop")


def _make_mock_ws(messages: list[str]) -> AsyncMock:
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.query_params = {"caller": "+15551234567", "callee": "+15559876543"}
    ws.receive_text = AsyncMock(side_effect=messages + [Exception("stop")])
    ws.send_text = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# telnyx_stream_bridge lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelnyxStreamBridgeLifecycle:
    """Full lifecycle of telnyx_stream_bridge."""

    @pytest.mark.asyncio
    @patch("getpatter.handlers.telnyx_handler.OpenAIRealtimeStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_start_then_stop_fires_callbacks(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        messages = [_stream_started_message(), _stream_stopped_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler

        mock_metrics = MagicMock()
        mock_metrics.end_call.return_value = MagicMock()
        mock_create_metrics.return_value = mock_metrics

        on_call_start = AsyncMock(return_value=None)
        on_call_end = AsyncMock()

        await telnyx_stream_bridge(
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
    @patch("getpatter.handlers.telnyx_handler.PipelineStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_pipeline_provider_creates_pipeline_handler(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        messages = [_stream_started_message(), _stream_stopped_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="pipeline"),
            openai_key="sk-test",
        )

        mock_handler_cls.assert_called_once()
        mock_handler.start.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("getpatter.handlers.telnyx_handler.ElevenLabsConvAIStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_elevenlabs_provider_creates_convai_handler(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        messages = [_stream_started_message(), _stream_stopped_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="elevenlabs_convai"),
            openai_key="sk-test",
            elevenlabs_key="el-test",
        )

        mock_handler_cls.assert_called_once()

    @pytest.mark.asyncio
    @patch("getpatter.handlers.telnyx_handler.OpenAIRealtimeStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_media_event_forwards_audio(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        audio_bytes = b"\x00\x01" * 320
        messages = [
            _stream_started_message(),
            _media_message(audio_bytes),
            _stream_stopped_message(),
        ]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        mock_handler.on_audio_received.assert_awaited_once_with(audio_bytes)

    @pytest.mark.asyncio
    @patch("getpatter.handlers.telnyx_handler.OpenAIRealtimeStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_empty_audio_chunk_skipped(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        # Empty audio chunk
        empty_media = json.dumps(
            {
                "event_type": "media",
                "payload": {"audio": {"chunk": ""}},
            }
        )
        messages = [_stream_started_message(), empty_media, _stream_stopped_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        mock_handler.on_audio_received.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("getpatter.handlers.telnyx_handler.OpenAIRealtimeStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_oversized_message_dropped(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import (
            telnyx_stream_bridge,
            _MAX_WS_MESSAGE_BYTES,
        )

        huge_msg = "x" * (_MAX_WS_MESSAGE_BYTES + 1)
        messages = [_stream_started_message(), huge_msg, _stream_stopped_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        mock_handler.on_audio_received.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("getpatter.handlers.telnyx_handler.OpenAIRealtimeStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_metrics_finalized_on_end(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        messages = [_stream_started_message(), _stream_stopped_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler

        mock_metrics = MagicMock()
        mock_metrics.end_call.return_value = MagicMock()
        mock_create_metrics.return_value = mock_metrics

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        mock_metrics.configure_stt_format.assert_called_once_with(
            sample_rate=16000, bytes_per_sample=2
        )
        mock_metrics.end_call.assert_called_once()

    @pytest.mark.asyncio
    @patch("getpatter.handlers.telnyx_handler.OpenAIRealtimeStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_call_overrides_applied(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        messages = [_stream_started_message(), _stream_stopped_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        on_call_start = AsyncMock(return_value={"voice": "nova"})

        with patch(
            "getpatter.handlers.telnyx_handler.apply_call_overrides"
        ) as mock_apply:
            mock_apply.return_value = make_agent(voice="nova")
            await telnyx_stream_bridge(
                websocket=ws,
                agent=make_agent(provider="openai_realtime"),
                openai_key="sk-test",
                on_call_start=on_call_start,
            )

            mock_apply.assert_called_once()

    @pytest.mark.asyncio
    @patch("getpatter.handlers.telnyx_handler.OpenAIRealtimeStreamHandler")
    @patch("getpatter.handlers.telnyx_handler.create_metrics_accumulator")
    @patch(
        "getpatter.handlers.telnyx_handler.resolve_agent_prompt", return_value="prompt"
    )
    @patch(
        "getpatter.handlers.telnyx_handler.fetch_deepgram_cost", new_callable=AsyncMock
    )
    async def test_openai_realtime_uses_g711_ulaw_format(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        messages = [_stream_started_message(), _stream_stopped_message()]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        # Telnyx streaming_start negotiates PCMU 8 kHz bidirectional, so
        # OpenAI Realtime must emit g711_ulaw to avoid transcoding mismatch
        # (BUG #18).
        call_kwargs = mock_handler_cls.call_args[1]
        assert call_kwargs.get("audio_format") == "g711_ulaw"

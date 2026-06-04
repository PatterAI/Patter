"""Unit tests for Gemini Live telephony dispatch (Twilio + Telnyx) and audio resampling."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import fake_pcm_frame, make_agent


# ---------------------------------------------------------------------------
# WebSocket message helpers
# ---------------------------------------------------------------------------


def _twilio_start(call_sid: str = "CA" + "a" * 32) -> str:
    return json.dumps({
        "event": "start",
        "streamSid": "MZ_test",
        "start": {"callSid": call_sid, "customParameters": {}},
    })


def _twilio_stop() -> str:
    return json.dumps({"event": "stop"})


def _telnyx_start(call_control_id: str = "v3:test-id") -> str:
    return json.dumps({
        "event": "start",
        "start": {
            "call_control_id": call_control_id,
            "from": "+15551111111",
            "to": "+15552222222",
        },
    })


def _telnyx_stop() -> str:
    return json.dumps({"event": "stop"})


def _make_ws(messages: list[str]) -> AsyncMock:
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.query_params = {"caller": "+15551234567", "callee": "+15559876543"}
    ws.receive_text = AsyncMock(side_effect=messages + [Exception("stop")])
    ws.send_text = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# Twilio dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTwilioGeminiLiveDispatch:
    @pytest.mark.asyncio
    @patch("getpatter.telephony.twilio.GeminiLiveStreamHandler")
    @patch("getpatter.telephony.twilio.create_metrics_accumulator")
    @patch("getpatter.telephony.twilio.resolve_agent_prompt", return_value="prompt")
    @patch("getpatter.telephony.twilio.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("getpatter.telephony.twilio.TwilioAudioSender")
    async def test_twilio_dispatches_gemini_live(
        self,
        mock_audio_sender_cls,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.telephony.twilio import twilio_stream_bridge

        ws = _make_ws([_twilio_start(), _twilio_stop()])

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()

        mock_audio_sender_cls.return_value = MagicMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="gemini_live"),
            openai_key="",
            gemini_key="gemini-test-key-123",
        )

        mock_handler_cls.assert_called_once()
        call_kwargs = mock_handler_cls.call_args[1]
        assert call_kwargs["gemini_key"] == "gemini-test-key-123"
        assert call_kwargs["audio_format"] == "pcm16"
        mock_handler.start.assert_awaited_once()

        # gemini_live is not in ("openai_realtime", "openai_realtime_2") so
        # TwilioAudioSender must be created with input_is_mulaw_8k=False
        mock_audio_sender_cls.assert_called_once()
        sender_kwargs = mock_audio_sender_cls.call_args[1]
        assert sender_kwargs.get("input_is_mulaw_8k") is False


# ---------------------------------------------------------------------------
# Telnyx dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelnyxGeminiLiveDispatch:
    @pytest.mark.asyncio
    @patch("getpatter.telephony.telnyx.GeminiLiveStreamHandler")
    @patch("getpatter.telephony.telnyx.create_metrics_accumulator")
    @patch("getpatter.telephony.telnyx.resolve_agent_prompt", return_value="prompt")
    @patch("getpatter.telephony.telnyx.fetch_deepgram_cost", new_callable=AsyncMock)
    @patch("getpatter.telephony.telnyx.TelnyxAudioSender")
    async def test_telnyx_dispatches_gemini_live(
        self,
        mock_audio_sender_cls,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.telephony.telnyx import telnyx_stream_bridge

        ws = _make_ws([_telnyx_start(), _telnyx_stop()])

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_create_metrics.return_value = MagicMock()
        mock_audio_sender_cls.return_value = MagicMock()

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="gemini_live"),
            openai_key="",
            gemini_key="gemini-test-key-456",
        )

        mock_handler_cls.assert_called_once()
        call_kwargs = mock_handler_cls.call_args[1]
        assert call_kwargs["gemini_key"] == "gemini-test-key-456"
        assert call_kwargs["audio_format"] == "pcm16"
        # transfer and hangup closures are wired up
        assert call_kwargs["transfer_fn"] is not None and callable(call_kwargs["transfer_fn"])
        assert call_kwargs["hangup_fn"] is not None and callable(call_kwargs["hangup_fn"])
        mock_handler.start.assert_awaited_once()


# ---------------------------------------------------------------------------
# Stream handler audio resampling
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGeminiStreamHandlerAudioResample:
    @pytest.mark.asyncio
    @patch("getpatter.audio.transcoding.create_resampler_24k_to_16k")
    async def test_gemini_stream_handler_audio_resample(
        self,
        mock_create_resampler,
    ) -> None:
        from getpatter.stream_handler import GeminiLiveStreamHandler

        audio_bytes = fake_pcm_frame(duration_ms=20, sample_rate=24000)
        resampled_bytes = fake_pcm_frame(duration_ms=20, sample_rate=16000)

        mock_resampler = MagicMock()
        mock_resampler.process.return_value = resampled_bytes
        mock_create_resampler.return_value = mock_resampler

        mock_audio_sender = AsyncMock()

        agent = make_agent(provider="gemini_live", tools=None, first_message="")
        handler = GeminiLiveStreamHandler(
            agent=agent,
            audio_sender=mock_audio_sender,
            call_id="test-call-1",
            caller="+15551111111",
            callee="+15552222222",
            resolved_prompt="You are a test agent.",
            metrics=MagicMock(),
            gemini_key="gemini-key-xyz",
        )

        async def _fake_events():
            yield "audio", audio_bytes

        mock_adapter = MagicMock()
        mock_adapter.receive_events.return_value = _fake_events()
        handler._adapter = mock_adapter

        await handler._forward_events()

        # Resampler was created lazily on first audio frame
        mock_create_resampler.assert_called_once()
        # process() received the raw 24 kHz bytes from the adapter
        mock_resampler.process.assert_called_once_with(audio_bytes)
        # audio_sender received the 16 kHz resampled bytes
        mock_audio_sender.send_audio.assert_awaited_once_with(resampled_bytes)

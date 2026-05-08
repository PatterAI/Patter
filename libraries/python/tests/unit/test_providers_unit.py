"""Unit tests for provider adapters — construction, repr, and static methods.

Tests provider adapters without making real network connections.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# OpenAIRealtimeAdapter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenAIRealtimeAdapter:
    """OpenAIRealtimeAdapter construction and basic behavior."""

    def test_init_stores_config(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(
            api_key="sk-test",
            model="gpt-4o-mini-realtime-preview",
            voice="nova",
            instructions="Be helpful",
            language="en",
            audio_format="g711_ulaw",
        )
        assert adapter.api_key == "sk-test"
        assert adapter.model == "gpt-4o-mini-realtime-preview"
        assert adapter.voice == "nova"
        assert adapter.instructions == "Be helpful"
        assert adapter.language == "en"
        assert adapter.audio_format == "g711_ulaw"
        assert adapter._ws is None
        assert adapter._running is False

    def test_init_defaults(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        assert adapter.model == "gpt-realtime-mini"
        assert adapter.voice == "alloy"
        assert adapter.audio_format == "g711_ulaw"
        assert adapter.tools is None

    def test_repr(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(
            api_key="sk-test",
            model="gpt-4o-mini-realtime-preview",
            voice="alloy",
        )
        r = repr(adapter)
        assert "OpenAIRealtimeAdapter" in r
        assert "gpt-4o-mini-realtime-preview" in r
        assert "alloy" in r

    def test_pcm16_format(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test", audio_format="pcm16")
        assert adapter.audio_format == "pcm16"

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_no_ws(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        await adapter.send_audio(b"\x00\x01\x02\x03")

    @pytest.mark.asyncio
    async def test_send_text_noop_when_no_ws(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        await adapter.send_text("hello")

    @pytest.mark.asyncio
    async def test_send_function_result_noop_when_no_ws(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        await adapter.send_function_result("call-1", '{"result": "ok"}')

    @pytest.mark.asyncio
    async def test_close_when_no_ws(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        await adapter.close()
        assert adapter._running is False
        assert adapter._ws is None

    @pytest.mark.asyncio
    async def test_close_when_ws_exists(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        adapter._ws = AsyncMock()
        adapter._running = True
        await adapter.close()
        assert adapter._running is False

    @pytest.mark.asyncio
    async def test_cancel_response_noop_when_no_ws(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        await adapter.cancel_response()

    def test_url_constant(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        assert "openai.com" in OpenAIRealtimeAdapter.OPENAI_REALTIME_URL

    def test_tools_stored(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        tools = [
            {"name": "get_weather", "description": "Get weather", "parameters": {}}
        ]
        adapter = OpenAIRealtimeAdapter(api_key="sk-test", tools=tools)
        assert adapter.tools == tools

    def test_gpt_realtime_2_model_enum(self) -> None:
        """gpt-realtime-2 is exposed as a model identifier."""
        from getpatter.providers.openai_realtime import OpenAIRealtimeModel

        assert OpenAIRealtimeModel.GPT_REALTIME_2.value == "gpt-realtime-2"
        # And the adapter accepts it as the model arg.
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(
            api_key="sk-test",
            model=OpenAIRealtimeModel.GPT_REALTIME_2,
        )
        assert adapter.model == "gpt-realtime-2"

    def test_gpt_realtime_whisper_transcription_enum(self) -> None:
        """gpt-realtime-whisper is exposed as a transcription model."""
        from getpatter.providers.openai_realtime import OpenAITranscriptionModel

        assert (
            OpenAITranscriptionModel.GPT_REALTIME_WHISPER.value
            == "gpt-realtime-whisper"
        )

    def test_reasoning_effort_default_unset(self) -> None:
        """``reasoning_effort`` defaults to None so the wire field stays absent."""
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        assert adapter.reasoning_effort is None

    def test_reasoning_effort_stored(self) -> None:
        """Constructor accepts ``reasoning_effort`` for gpt-realtime-2 sessions."""
        from getpatter.providers.openai_realtime import (
            OpenAIRealtimeAdapter,
            OpenAIRealtimeModel,
        )

        adapter = OpenAIRealtimeAdapter(
            api_key="sk-test",
            model=OpenAIRealtimeModel.GPT_REALTIME_2,
            reasoning_effort="low",
        )
        assert adapter.reasoning_effort == "low"

    @pytest.mark.asyncio
    async def test_reasoning_effort_injected_into_session_update(self) -> None:
        """When set, ``reasoning.effort`` is sent in the ``session.update`` payload."""
        import json

        from getpatter.providers.openai_realtime import (
            OpenAIRealtimeAdapter,
            OpenAIRealtimeModel,
        )

        adapter = OpenAIRealtimeAdapter(
            api_key="sk-test",
            model=OpenAIRealtimeModel.GPT_REALTIME_2,
            reasoning_effort="low",
        )
        sent_payloads: list[dict] = []

        ws = AsyncMock()
        # session.created → session.updated handshake
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "session.created"}),
                json.dumps({"type": "session.updated"}),
            ]
        )

        async def _capture(payload: str) -> None:
            sent_payloads.append(json.loads(payload))

        ws.send = AsyncMock(side_effect=_capture)
        ws.close = AsyncMock()

        with patch(
            "getpatter.providers.openai_realtime.websockets.connect",
            new=AsyncMock(return_value=ws),
        ):
            await adapter.connect()

        update = next(p for p in sent_payloads if p.get("type") == "session.update")
        assert update["session"].get("reasoning") == {"effort": "low"}

    @pytest.mark.asyncio
    async def test_reasoning_effort_omitted_when_unset(self) -> None:
        """When unset, the ``reasoning`` key is absent from the session payload."""
        import json

        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        sent_payloads: list[dict] = []

        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "session.created"}),
                json.dumps({"type": "session.updated"}),
            ]
        )

        async def _capture(payload: str) -> None:
            sent_payloads.append(json.loads(payload))

        ws.send = AsyncMock(side_effect=_capture)
        ws.close = AsyncMock()

        with patch(
            "getpatter.providers.openai_realtime.websockets.connect",
            new=AsyncMock(return_value=ws),
        ):
            await adapter.connect()

        update = next(p for p in sent_payloads if p.get("type") == "session.update")
        assert "reasoning" not in update["session"]

    @pytest.mark.asyncio
    @pytest.mark.mocked
    async def test_cancel_response_caps_audio_end_ms_to_wallclock(self) -> None:
        """Regression: barge-in truncate must not credit unplayed audio.

        When OpenAI streams audio at multiple-x real-time and the consumer
        clears the playout buffer (e.g. ``audio_sender.send_clear`` on
        barge-in), the user only ever heard ~wall-clock-ms of speech. If we
        pass the byte-derived ``audio_end_ms`` to ``conversation.item.truncate``
        OpenAI keeps the full generated transcript, and the model replays /
        resumes from it on the next turn — re-greetings and mid-sentence
        fragments. Cap by wall-clock instead.
        """
        import json

        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test", audio_format="g711_ulaw")

        sent: list[dict] = []
        ws = AsyncMock()

        async def _capture(payload: str) -> None:
            sent.append(json.loads(payload))

        ws.send = AsyncMock(side_effect=_capture)
        adapter._ws = ws
        # Simulate state after first chunk arrived ~30 ms ago, but the byte
        # counter says we generated 2 s of audio (5x real-time arrival).
        adapter._current_response_item_id = "item-1"
        adapter._current_response_audio_ms = 2000
        import time as _time

        adapter._current_response_first_audio_at = _time.monotonic() - 0.03

        await adapter.cancel_response()

        truncate = next(
            p for p in sent if p.get("type") == "conversation.item.truncate"
        )
        # audio_end_ms must be bounded by wall-clock (~30 ms), not the raw
        # 2000 ms generated counter. Allow generous slack for scheduler.
        assert truncate["audio_end_ms"] <= 200, (
            f"audio_end_ms should be bounded by wall-clock playback, "
            f"got {truncate['audio_end_ms']} ms (generated counter was 2000 ms)"
        )
        assert truncate["item_id"] == "item-1"
        # response.cancel is sent after truncate
        kinds = [p.get("type") for p in sent]
        assert kinds.index("conversation.item.truncate") < kinds.index(
            "response.cancel"
        )
        # Per-response state is reset so the next response.create starts clean
        assert adapter._current_response_item_id is None
        assert adapter._current_response_audio_ms == 0
        assert adapter._current_response_first_audio_at is None

    @pytest.mark.asyncio
    @pytest.mark.mocked
    async def test_cancel_response_falls_back_to_generated_when_no_first_audio(
        self,
    ) -> None:
        """If no audio chunks arrived yet (cancel before any delta), fall back
        to the byte-derived counter — the wall-clock cap kicks in only when a
        chunk has actually been received."""
        import json

        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test", audio_format="g711_ulaw")

        sent: list[dict] = []
        ws = AsyncMock()

        async def _capture(payload: str) -> None:
            sent.append(json.loads(payload))

        ws.send = AsyncMock(side_effect=_capture)
        adapter._ws = ws
        adapter._current_response_item_id = "item-1"
        adapter._current_response_audio_ms = 0  # no audio yet
        adapter._current_response_first_audio_at = None

        await adapter.cancel_response()
        truncate = next(
            p for p in sent if p.get("type") == "conversation.item.truncate"
        )
        assert truncate["audio_end_ms"] == 0

    @pytest.mark.asyncio
    async def test_realtime_engine_forwards_reasoning_and_transcription_to_adapter(
        self,
    ) -> None:
        """Regression: ``engines.openai.Realtime(reasoning_effort=...,
        input_audio_transcription_model=...)`` must reach
        ``OpenAIRealtimeAdapter`` via the stream-handler. Previously these
        only worked when constructing the adapter directly; the high-level
        engine wrapper silently dropped them.
        """
        import os

        from getpatter import Patter
        from getpatter.engines import openai as eng_openai
        from getpatter.stream_handler import OpenAIRealtimeStreamHandler

        os.environ.setdefault("OPENAI_API_KEY", "sk-test-engine-forward")

        phone = Patter()
        agent = phone.agent(
            system_prompt="You are helpful.",
            engine=eng_openai.Realtime(
                api_key="sk-test-engine-forward",
                model="gpt-realtime-2",
                reasoning_effort="low",
                input_audio_transcription_model="gpt-realtime-whisper",
            ),
        )

        # Engine fields are surfaced on the Agent so the handler can forward
        # them — verifies the unpack path before the connect() forwarding.
        assert agent.openai_realtime_reasoning_effort == "low"
        assert (
            agent.openai_realtime_input_audio_transcription_model
            == "gpt-realtime-whisper"
        )

        handler = OpenAIRealtimeStreamHandler(
            agent=agent,
            audio_sender=AsyncMock(),
            call_id="CA0000000000000000000000000000a042",
            caller="+15555550100",
            callee="+15555550101",
            resolved_prompt="You are helpful.",
            metrics=None,
            openai_key="sk-test-engine-forward",
            audio_format="g711_ulaw",
        )

        captured: dict = {}

        class _FakeAdapter:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            async def connect(self) -> None:
                return None

        with patch(
            "getpatter.providers.openai_realtime.OpenAIRealtimeAdapter",
            _FakeAdapter,
        ):
            await handler.start()

        assert captured["reasoning_effort"] == "low"
        assert captured["input_audio_transcription_model"] == "gpt-realtime-whisper"
        # And the existing fields keep flowing through.
        assert captured["api_key"] == "sk-test-engine-forward"
        assert captured["model"] == "gpt-realtime-2"
        assert captured["audio_format"] == "g711_ulaw"


@pytest.mark.unit
class TestElevenLabsConvAIAdapter:
    """ElevenLabsConvAIAdapter construction and basic behavior."""

    def test_init_stores_config(self) -> None:
        from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(
            api_key="el-test",
            agent_id="agent-123",
            voice_id="voice-456",
            model_id="eleven_flash_v2_5",
            language="en",
            first_message="Hello!",
        )
        assert adapter.api_key == "el-test"
        assert adapter.agent_id == "agent-123"
        assert adapter.voice_id == "voice-456"
        assert adapter.model_id == "eleven_flash_v2_5"
        assert adapter.language == "en"
        assert adapter.first_message == "Hello!"
        assert adapter._ws is None
        assert adapter._running is False

    def test_init_defaults(self) -> None:
        from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test", agent_id="agent-test")
        assert adapter.agent_id == "agent-test"
        assert adapter.voice_id == "EXAVITQu4vr4xnSDxMaL"
        assert adapter.model_id == "eleven_flash_v2_5"
        assert adapter.language == "it"
        assert adapter.first_message == ""

    def test_init_requires_agent_id(self) -> None:
        import pytest

        from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        with pytest.raises(ValueError, match="agent_id"):
            ElevenLabsConvAIAdapter(api_key="el-test", agent_id="")

    def test_repr(self) -> None:
        from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test", agent_id="agent-1")
        r = repr(adapter)
        assert "ElevenLabsConvAIAdapter" in r
        assert "agent-1" in r

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_no_ws(self) -> None:
        from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test", agent_id="agent-test")
        await adapter.send_audio(b"\x00\x01\x02\x03")

    @pytest.mark.asyncio
    async def test_close_when_no_ws(self) -> None:
        from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test", agent_id="agent-test")
        await adapter.close()
        assert adapter._running is False
        assert adapter._ws is None

    @pytest.mark.asyncio
    async def test_close_when_ws_exists(self) -> None:
        from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test", agent_id="agent-test")
        adapter._ws = AsyncMock()
        adapter._running = True
        await adapter.close()
        assert adapter._running is False

    def test_url_constant(self) -> None:
        from getpatter.providers.elevenlabs_convai import ELEVENLABS_CONVAI_URL

        assert "elevenlabs.io" in ELEVENLABS_CONVAI_URL


@pytest.mark.unit
class TestTelnyxAdapter:
    """TelnyxAdapter construction and repr."""

    def test_init_stores_config(self) -> None:
        from getpatter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key-test", connection_id="conn-1")
        assert adapter.api_key == "key-test"
        assert adapter.connection_id == "conn-1"

    def test_init_defaults(self) -> None:
        from getpatter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key-test")
        assert adapter.connection_id == ""

    def test_repr(self) -> None:
        from getpatter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key-test", connection_id="conn-1")
        r = repr(adapter)
        assert "TelnyxAdapter" in r
        assert "conn-1" in r

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        from getpatter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key-test")
        await adapter.close()


@pytest.mark.unit
class TestBaseClasses:
    """Base provider classes and dataclasses."""

    def test_transcript_dataclass(self) -> None:
        from getpatter.providers.base import Transcript

        t = Transcript(text="hello", is_final=True, confidence=0.95)
        assert t.text == "hello"
        assert t.is_final is True
        assert t.confidence == 0.95

    def test_transcript_default_confidence(self) -> None:
        from getpatter.providers.base import Transcript

        t = Transcript(text="hi", is_final=False)
        assert t.confidence == 0.0

    def test_call_info_dataclass(self) -> None:
        from getpatter.providers.base import CallInfo

        ci = CallInfo(call_id="c1", caller="+1555", callee="+1666", direction="inbound")
        assert ci.call_id == "c1"
        assert ci.direction == "inbound"


@pytest.mark.unit
class TestPricing:
    """Pricing module functions."""

    def test_merge_pricing_no_overrides(self) -> None:
        from getpatter.pricing import DEFAULT_PRICING, merge_pricing

        result = merge_pricing(None)
        assert "deepgram" in result
        assert result is not DEFAULT_PRICING

    def test_merge_pricing_with_overrides(self) -> None:
        from getpatter.pricing import merge_pricing

        result = merge_pricing({"deepgram": {"price": 0.005}})
        assert result["deepgram"]["price"] == 0.005
        assert result["deepgram"]["unit"] == "minute"

    def test_merge_pricing_new_provider(self) -> None:
        from getpatter.pricing import merge_pricing

        result = merge_pricing({"custom_stt": {"unit": "minute", "price": 0.01}})
        assert result["custom_stt"]["price"] == 0.01

    def test_calculate_stt_cost(self) -> None:
        from getpatter.pricing import DEFAULT_PRICING, calculate_stt_cost, merge_pricing

        pricing = merge_pricing(None)
        cost = calculate_stt_cost("deepgram", 60.0, pricing)
        assert cost == pytest.approx(DEFAULT_PRICING["deepgram"]["price"])

    def test_calculate_stt_cost_unknown_provider(self) -> None:
        from getpatter.pricing import calculate_stt_cost, merge_pricing

        pricing = merge_pricing(None)
        cost = calculate_stt_cost("unknown", 60.0, pricing)
        assert cost == 0.0

    def test_calculate_tts_cost(self) -> None:
        from getpatter.pricing import DEFAULT_PRICING, calculate_tts_cost, merge_pricing

        pricing = merge_pricing(None)
        cost = calculate_tts_cost("elevenlabs", 1000, pricing)
        assert cost == pytest.approx(DEFAULT_PRICING["elevenlabs"]["price"])

    def test_calculate_tts_cost_unknown_provider(self) -> None:
        from getpatter.pricing import calculate_tts_cost, merge_pricing

        pricing = merge_pricing(None)
        cost = calculate_tts_cost("unknown", 1000, pricing)
        assert cost == 0.0

    def test_calculate_realtime_cost(self) -> None:
        from getpatter.pricing import calculate_realtime_cost, merge_pricing

        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {"audio_tokens": 100, "text_tokens": 50},
            "output_token_details": {"audio_tokens": 200, "text_tokens": 30},
        }
        cost = calculate_realtime_cost(usage, pricing)
        assert cost > 0.0

    def test_calculate_realtime_cost_empty_usage(self) -> None:
        from getpatter.pricing import calculate_realtime_cost, merge_pricing

        pricing = merge_pricing(None)
        cost = calculate_realtime_cost({}, pricing)
        assert cost == 0.0

    def test_calculate_telephony_cost(self) -> None:
        from getpatter.pricing import calculate_telephony_cost, merge_pricing

        pricing = merge_pricing(None)
        cost = calculate_telephony_cost("twilio", 60.0, pricing)
        assert cost > 0.0

    def test_calculate_telephony_cost_unknown(self) -> None:
        from getpatter.pricing import calculate_telephony_cost, merge_pricing

        pricing = merge_pricing(None)
        cost = calculate_telephony_cost("unknown", 60.0, pricing)
        assert cost == 0.0

    def test_pricing_version_exists(self) -> None:
        from getpatter.pricing import PRICING_VERSION

        assert PRICING_VERSION


@pytest.mark.unit
class TestOpenAITranscribeSTT:
    """OpenAITranscribeSTT — first-class wrapper around gpt-4o-transcribe."""

    def test_default_model_is_gpt4o_transcribe(self) -> None:
        from getpatter import OpenAITranscribeSTT

        stt = OpenAITranscribeSTT(api_key="sk-test")
        assert stt.model == "gpt-4o-transcribe"

    def test_accepts_mini_variant(self) -> None:
        from getpatter import OpenAITranscribeSTT

        stt = OpenAITranscribeSTT(api_key="sk-test", model="gpt-4o-mini-transcribe")
        assert stt.model == "gpt-4o-mini-transcribe"

    def test_rejects_whisper_1(self) -> None:
        from getpatter import OpenAITranscribeSTT

        with pytest.raises(ValueError, match="unsupported model"):
            OpenAITranscribeSTT(api_key="sk-test", model="whisper-1")


@pytest.mark.unit
class TestElevenLabsTTSModelLiteral:
    """ElevenLabsTTS accepts the new ``eleven_v3`` model literal."""

    def test_eleven_v3_constructs(self) -> None:
        from getpatter.providers.elevenlabs_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="x", model_id="eleven_v3")
        assert tts.model_id == "eleven_v3"

    def test_default_remains_flash(self) -> None:
        from getpatter.providers.elevenlabs_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="x")
        assert tts.model_id == "eleven_flash_v2_5"

"""Unit tests for provider adapters — construction, repr, and static methods.

Tests provider adapters without making real network connections.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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

        tools = [{"name": "get_weather", "description": "Get weather", "parameters": {}}]
        adapter = OpenAIRealtimeAdapter(api_key="sk-test", tools=tools)
        assert adapter.tools == tools


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

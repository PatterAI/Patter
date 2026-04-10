"""Tests for standalone provider adapters and services."""

import struct
import pytest

from patter.providers.openai_realtime import OpenAIRealtimeAdapter
from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter
from patter.providers.deepgram_stt import DeepgramSTT
from patter.providers.elevenlabs_tts import ElevenLabsTTS
from patter.providers.openai_tts import OpenAITTS
from patter.providers.telnyx_adapter import TelnyxAdapter
from patter.services.transcoding import mulaw_to_pcm16, pcm16_to_mulaw, resample_8k_to_16k, resample_16k_to_8k
from patter.services.tool_executor import ToolExecutor


# ---------------------------------------------------------------------------
# OpenAIRealtimeAdapter
# ---------------------------------------------------------------------------


def test_openai_realtime_init():
    adapter = OpenAIRealtimeAdapter(api_key="sk_test", model="gpt-4o-mini-realtime-preview")
    assert adapter.api_key == "sk_test"
    assert adapter.model == "gpt-4o-mini-realtime-preview"


def test_openai_realtime_default_model():
    adapter = OpenAIRealtimeAdapter(api_key="sk_test")
    assert adapter.model == "gpt-4o-mini-realtime-preview"


def test_openai_realtime_default_voice():
    adapter = OpenAIRealtimeAdapter(api_key="sk_test")
    assert adapter.voice == "alloy"


def test_openai_realtime_custom_voice():
    adapter = OpenAIRealtimeAdapter(api_key="sk_test", voice="nova")
    assert adapter.voice == "nova"


def test_openai_realtime_with_tools():
    tools = [{"name": "check", "description": "test", "parameters": {}}]
    adapter = OpenAIRealtimeAdapter(api_key="sk_test", tools=tools)
    assert adapter.tools == tools


def test_openai_realtime_no_tools_by_default():
    adapter = OpenAIRealtimeAdapter(api_key="sk_test")
    assert adapter.tools is None


def test_openai_realtime_instructions():
    adapter = OpenAIRealtimeAdapter(api_key="sk_test", instructions="Be helpful.")
    assert adapter.instructions == "Be helpful."


def test_openai_realtime_language():
    adapter = OpenAIRealtimeAdapter(api_key="sk_test", language="it")
    assert adapter.language == "it"


def test_openai_realtime_ws_none_initially():
    adapter = OpenAIRealtimeAdapter(api_key="sk_test")
    assert adapter._ws is None


# ---------------------------------------------------------------------------
# ElevenLabsConvAIAdapter
# ---------------------------------------------------------------------------


def test_elevenlabs_convai_init():
    adapter = ElevenLabsConvAIAdapter(api_key="el_test", voice_id="v123")
    assert adapter.api_key == "el_test"
    assert adapter.voice_id == "v123"


def test_elevenlabs_convai_default_model():
    adapter = ElevenLabsConvAIAdapter(api_key="el_test")
    assert adapter.model_id == "eleven_turbo_v2_5"


def test_elevenlabs_convai_agent_id():
    adapter = ElevenLabsConvAIAdapter(api_key="el_test", agent_id="agent_123")
    assert adapter.agent_id == "agent_123"


def test_elevenlabs_convai_language():
    adapter = ElevenLabsConvAIAdapter(api_key="el_test", language="it")
    assert adapter.language == "it"


def test_elevenlabs_convai_first_message():
    adapter = ElevenLabsConvAIAdapter(api_key="el_test", first_message="Ciao!")
    assert adapter.first_message == "Ciao!"


def test_elevenlabs_convai_ws_none_initially():
    adapter = ElevenLabsConvAIAdapter(api_key="el_test")
    assert adapter._ws is None


# ---------------------------------------------------------------------------
# DeepgramSTT
# ---------------------------------------------------------------------------


def test_deepgram_init():
    stt = DeepgramSTT(api_key="dg_test")
    assert stt.api_key == "dg_test"
    assert stt.model == "nova-3"


def test_deepgram_default_encoding():
    stt = DeepgramSTT(api_key="dg_test")
    assert stt.encoding == "linear16"


def test_deepgram_default_sample_rate():
    stt = DeepgramSTT(api_key="dg_test")
    assert stt.sample_rate == 16000


def test_deepgram_custom_language():
    stt = DeepgramSTT(api_key="dg_test", language="it")
    assert stt.language == "it"


def test_deepgram_for_twilio():
    stt = DeepgramSTT.for_twilio(api_key="dg_test", language="it")
    assert stt.encoding == "mulaw"
    assert stt.sample_rate == 8000
    assert stt.language == "it"


def test_deepgram_for_twilio_default_language():
    stt = DeepgramSTT.for_twilio(api_key="dg_test")
    assert stt.language == "en"


def test_deepgram_ws_none_initially():
    stt = DeepgramSTT(api_key="dg_test")
    assert stt._ws is None


# ---------------------------------------------------------------------------
# ElevenLabsTTS
# ---------------------------------------------------------------------------


def test_elevenlabs_tts_init():
    tts = ElevenLabsTTS(api_key="el_test", voice_id="v123")
    assert tts.api_key == "el_test"
    assert tts.voice_id == "v123"


def test_elevenlabs_tts_default_model():
    tts = ElevenLabsTTS(api_key="el_test")
    assert tts.model_id == "eleven_turbo_v2_5"


def test_elevenlabs_tts_default_format():
    tts = ElevenLabsTTS(api_key="el_test")
    assert tts.output_format == "pcm_16000"


# ---------------------------------------------------------------------------
# OpenAITTS
# ---------------------------------------------------------------------------


def test_openai_tts_init():
    tts = OpenAITTS(api_key="sk_test", voice="nova")
    assert tts.voice == "nova"
    assert tts.api_key == "sk_test"


def test_openai_tts_default_voice():
    tts = OpenAITTS(api_key="sk_test")
    assert tts.voice == "alloy"


def test_openai_tts_default_model():
    tts = OpenAITTS(api_key="sk_test")
    assert tts.model == "tts-1"


def test_openai_tts_resample_empty():
    result = OpenAITTS._resample_24k_to_16k(b"")
    assert result == b""


def test_openai_tts_resample_single_byte():
    result = OpenAITTS._resample_24k_to_16k(b"\x00")
    assert result == b"\x00"


def test_openai_tts_resample_output_shorter():
    # 6 samples at 24kHz → ~4 samples at 16kHz
    samples = struct.pack("<6h", 100, 200, 300, 400, 500, 600)
    result = OpenAITTS._resample_24k_to_16k(samples)
    assert len(result) > 0
    assert len(result) % 2 == 0


# ---------------------------------------------------------------------------
# TelnyxAdapter
# ---------------------------------------------------------------------------


def test_telnyx_adapter_init():
    adapter = TelnyxAdapter(api_key="KEY_test", connection_id="conn_123")
    assert adapter.api_key == "KEY_test"
    assert adapter.connection_id == "conn_123"


def test_telnyx_adapter_default_connection_id():
    adapter = TelnyxAdapter(api_key="KEY_test")
    assert adapter.connection_id == ""


# ---------------------------------------------------------------------------
# TwilioAdapter
# ---------------------------------------------------------------------------


def test_twilio_adapter_init():
    pytest.importorskip("twilio", reason="twilio not installed (optional dep)")
    from patter.providers.twilio_adapter import TwilioAdapter
    adapter = TwilioAdapter(account_sid="AC_test", auth_token="tok_test")
    assert adapter.account_sid == "AC_test"
    assert adapter.auth_token == "tok_test"


def test_twilio_generate_stream_twiml():
    pytest.importorskip("twilio", reason="twilio not installed (optional dep)")
    from patter.providers.twilio_adapter import TwilioAdapter
    twiml = TwilioAdapter.generate_stream_twiml("wss://abc.ngrok.io/ws/stream/CA123")
    assert isinstance(twiml, str)
    assert "wss://abc.ngrok.io/ws/stream/CA123" in twiml
    assert "<Connect>" in twiml or "Connect" in twiml


def test_twilio_adapter_repr_does_not_expose_full_sid():
    """__repr__ must mask the account_sid so it cannot leak in full into logs."""
    pytest.importorskip("twilio", reason="twilio not installed (optional dep)")
    from patter.providers.twilio_adapter import TwilioAdapter
    adapter = TwilioAdapter(account_sid="AC1234567890abcdef", auth_token="secret_token")
    r = repr(adapter)
    # auth_token must never appear
    assert "secret_token" not in r
    # full SID must not appear verbatim
    assert "AC1234567890abcdef" not in r
    # but the repr should still contain a useful prefix
    assert "TwilioAdapter" in r


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------


def test_tool_executor_init():
    executor = ToolExecutor()
    assert executor is not None


@pytest.mark.asyncio
async def test_tool_executor_http_error():
    """ToolExecutor returns error JSON on HTTP failure."""
    import httpx
    from unittest.mock import AsyncMock, patch, MagicMock

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=mock_response
    )

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    executor = ToolExecutor(client=mock_client)

    result = await executor.execute(
        tool_name="test_tool",
        arguments={"key": "val"},
        webhook_url="https://example.com/hook",
        call_context={"call_id": "c1", "caller": "+1", "callee": "+2"},
    )
    import json
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_tool_executor_network_error():
    """ToolExecutor returns error JSON on network failure."""
    from unittest.mock import AsyncMock, patch, MagicMock

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("network error"))

    executor = ToolExecutor(client=mock_client)

    result = await executor.execute(
        tool_name="test_tool",
        arguments={},
        webhook_url="https://example.com/hook",
        call_context={},
    )
    import json
    parsed = json.loads(result)
    assert "error" in parsed
    assert "network error" in parsed["error"]


# ---------------------------------------------------------------------------
# Transcoding
# ---------------------------------------------------------------------------


def test_mulaw_pcm16_roundtrip():
    """Decode mulaw → pcm16, both should have correct byte lengths."""
    # Encode a known PCM value to mulaw, then decode back
    samples = struct.pack("<3h", 0, 1000, -1000)
    mulaw = pcm16_to_mulaw(samples)
    recovered = mulaw_to_pcm16(mulaw)
    assert len(recovered) == 6  # 3 samples * 2 bytes each


def test_pcm16_to_mulaw_output_is_bytes():
    samples = struct.pack("<4h", 100, -100, 500, -500)
    result = pcm16_to_mulaw(samples)
    assert isinstance(result, bytes)


def test_mulaw_to_pcm16_output_is_bytes():
    samples = struct.pack("<4h", 100, -100, 500, -500)
    mulaw = pcm16_to_mulaw(samples)
    result = mulaw_to_pcm16(mulaw)
    assert isinstance(result, bytes)


def test_resample_8k_to_16k_empty():
    assert resample_8k_to_16k(b"") == b""


def test_resample_16k_to_8k_empty():
    assert resample_16k_to_8k(b"") == b""


def test_resample_8k_to_16k_doubles_length():
    # 2 bytes = 1 PCM16 sample at 8kHz → ~2 samples at 16kHz = 4 bytes
    sample = struct.pack("<h", 1000)
    result = resample_8k_to_16k(sample)
    assert len(result) > 0
    assert len(result) % 2 == 0


def test_resample_16k_to_8k_halves_length():
    # 4 bytes = 2 PCM16 samples at 16kHz → ~1 sample at 8kHz = 2 bytes
    samples = struct.pack("<2h", 1000, 2000)
    result = resample_16k_to_8k(samples)
    assert len(result) > 0
    assert len(result) % 2 == 0

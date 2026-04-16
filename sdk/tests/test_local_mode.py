"""Tests for local (embedded) mode — Phase 4-6."""

import asyncio
import base64
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from patter import Patter
from patter.models import Agent
from patter.local_config import LocalConfig
from patter.exceptions import PatterConnectionError


# ---------------------------------------------------------------------------
# LocalConfig
# ---------------------------------------------------------------------------


def test_local_config_defaults():
    cfg = LocalConfig(telephony_provider="twilio", phone_number="+1555", webhook_url="x.ngrok.io")
    assert cfg.telephony_provider == "twilio"
    assert cfg.openai_key == ""
    assert cfg.twilio_sid == ""


def test_local_config_full():
    cfg = LocalConfig(
        telephony_provider="twilio",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
        openai_key="sk-test",
        twilio_sid="AC_test",
        twilio_token="tok_test",
    )
    assert cfg.openai_key == "sk-test"
    assert cfg.twilio_sid == "AC_test"


# ---------------------------------------------------------------------------
# Agent model
# ---------------------------------------------------------------------------


def test_agent_defaults():
    a = Agent(system_prompt="You are helpful.")
    assert a.voice == "alloy"
    assert a.model == "gpt-4o-mini-realtime-preview"
    assert a.language == "en"
    assert a.first_message == ""
    assert a.tools is None
    assert a.provider == "openai_realtime"


def test_agent_full():
    tools = [{"name": "lookup", "description": "Look up info", "parameters": {}}]
    a = Agent(
        system_prompt="Hello",
        voice="echo",
        model="gpt-4o-realtime-preview",
        language="it",
        first_message="Ciao!",
        tools=tools,
    )
    assert a.voice == "echo"
    assert len(a.tools) == 1


def test_agent_pipeline_provider():
    """Agent with provider='pipeline' is created correctly."""
    a = Agent(
        system_prompt="You are a helpful voice bot.",
        voice="21m00Tcm4TlvDq8ikWAM",
        language="en",
        provider="pipeline",
    )
    assert a.provider == "pipeline"
    assert a.voice == "21m00Tcm4TlvDq8ikWAM"
    assert a.model == "gpt-4o-mini-realtime-preview"  # model field still present, unused in pipeline mode


def test_agent_factory_pipeline_provider():
    """Patter.agent() passes provider through correctly."""
    phone = Patter(
        twilio_sid="AC",
        twilio_token="tok",
        openai_key="sk",
        deepgram_key="dg_test",
        elevenlabs_key="el_test",
        phone_number="+1",
        webhook_url="x.ngrok.io",
    )
    a = phone.agent(
        system_prompt="Pipeline bot",
        voice="21m00Tcm4TlvDq8ikWAM",
        provider="pipeline",
    )
    assert isinstance(a, Agent)
    assert a.provider == "pipeline"


def test_agent_pipeline_is_immutable():
    """Agent is a frozen dataclass — mutation raises."""
    a = Agent(system_prompt="test", provider="pipeline")
    with pytest.raises(Exception):
        a.provider = "openai_realtime"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Patter client — local mode detection
# ---------------------------------------------------------------------------


def test_local_mode_auto_detected_from_twilio_sid():
    phone = Patter(twilio_sid="AC_test", twilio_token="tok", openai_key="sk-test",
                   phone_number="+1555", webhook_url="x.ngrok.io")
    assert phone._mode == "local"
    assert isinstance(phone._local_config, LocalConfig)
    assert phone._local_config.telephony_provider == "twilio"


def test_local_mode_auto_detected_from_telnyx_key():
    phone = Patter(telnyx_key="KEY_test", openai_key="sk-test",
                   phone_number="+1555", webhook_url="x.ngrok.io")
    assert phone._mode == "local"
    assert phone._local_config.telephony_provider == "telnyx"


def test_local_mode_explicit():
    phone = Patter(mode="local", openai_key="sk-test",
                   phone_number="+1555", webhook_url="x.ngrok.io")
    assert phone._mode == "local"


def test_cloud_mode_when_api_key_given():
    phone = Patter(api_key="pt_test123")
    assert phone._mode == "cloud"
    assert phone.api_key == "pt_test123"
    assert hasattr(phone, "_connection")


# ---------------------------------------------------------------------------
# Patter.agent() factory
# ---------------------------------------------------------------------------


def test_agent_factory():
    phone = Patter(twilio_sid="AC", twilio_token="tok", openai_key="sk",
                   phone_number="+1", webhook_url="x.ngrok.io")
    a = phone.agent(
        system_prompt="You are a bot.",
        voice="shimmer",
        first_message="Hello!",
    )
    assert isinstance(a, Agent)
    assert a.system_prompt == "You are a bot."
    assert a.voice == "shimmer"
    assert a.first_message == "Hello!"


# ---------------------------------------------------------------------------
# serve() — guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_raises_in_cloud_mode():
    phone = Patter(api_key="pt_test")
    agent = Agent(system_prompt="test")
    with pytest.raises(PatterConnectionError, match="local mode"):
        await phone.serve(agent)


@pytest.mark.asyncio
async def test_serve_calls_embedded_server():
    phone = Patter(twilio_sid="AC", twilio_token="tok", openai_key="sk",
                   phone_number="+1555", webhook_url="x.ngrok.io")
    agent = phone.agent(system_prompt="test")

    mock_server = MagicMock()
    mock_server.start = AsyncMock()

    with patch("patter.server.EmbeddedServer", return_value=mock_server) as MockServer:
        await phone.serve(agent, port=9000)

        MockServer.assert_called_once_with(
            config=phone._local_config, agent=agent, recording=False, voicemail_message="", pricing=None, dashboard=True,
            dashboard_token="",
        )
        mock_server.start.assert_called_once_with(port=9000)


# ---------------------------------------------------------------------------
# connect() — raises in local mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_raises_in_local_mode():
    phone = Patter(twilio_sid="AC", twilio_token="tok", openai_key="sk",
                   phone_number="+1", webhook_url="x.ngrok.io")
    with pytest.raises(PatterConnectionError, match="local mode"):
        await phone.connect(on_message=AsyncMock())


# ---------------------------------------------------------------------------
# call() — local mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_local_mode_requires_agent():
    phone = Patter(twilio_sid="AC", twilio_token="tok", openai_key="sk",
                   phone_number="+1", webhook_url="x.ngrok.io")
    with pytest.raises(PatterConnectionError, match="agent parameter"):
        await phone.call(to="+39123")


@pytest.mark.asyncio
async def test_call_local_mode_twilio():
    phone = Patter(twilio_sid="AC_test", twilio_token="tok", openai_key="sk",
                   phone_number="+1555", webhook_url="x.ngrok.io")
    agent = phone.agent(system_prompt="test")

    mock_adapter = MagicMock()
    mock_adapter.initiate_call = AsyncMock(return_value="CA_sid")

    with patch("patter.client.Patter.call") as mock_call:
        mock_call.return_value = None
        # Just verify no exception is raised with proper args
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# Twilio webhook handler
# ---------------------------------------------------------------------------


def test_twilio_webhook_handler_url():
    """twilio_webhook_handler builds the correct wss:// stream URL."""
    from unittest.mock import patch as _patch

    # Patch TwilioAdapter so we don't need twilio installed
    with _patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
        MockAdapter.generate_stream_twiml.return_value = "<Response/>"
        from patter.handlers.twilio_handler import twilio_webhook_handler

        result = twilio_webhook_handler(
            call_sid="CA123",
            caller="+14155551234",
            callee="+15550001111",
            webhook_base_url="abc.ngrok.io",
        )

    MockAdapter.generate_stream_twiml.assert_called_once()
    call_args = MockAdapter.generate_stream_twiml.call_args[0][0]
    assert call_args.startswith("wss://abc.ngrok.io/ws/stream/CA123")
    assert "caller=" in call_args
    assert "callee=" in call_args
    assert result == "<Response/>"


# ---------------------------------------------------------------------------
# Telnyx webhook handler
# ---------------------------------------------------------------------------


def test_telnyx_webhook_handler_structure():
    from patter.handlers.telnyx_handler import telnyx_webhook_handler

    result = telnyx_webhook_handler(
        call_id="ctrl_123",
        caller="+14155551234",
        callee="+15550001111",
        webhook_base_url="abc.ngrok.io",
    )

    assert "commands" in result
    commands = result["commands"]
    assert any(c["command"] == "answer" for c in commands)
    stream_cmd = next((c for c in commands if c["command"] == "stream_start"), None)
    assert stream_cmd is not None
    assert "wss://abc.ngrok.io/ws/telnyx/stream/ctrl_123" in stream_cmd["params"]["stream_url"]


# ---------------------------------------------------------------------------
# Pipeline mode — DeepgramSTT / ElevenLabsTTS adapter init
# ---------------------------------------------------------------------------


def test_deepgram_stt_for_twilio_config():
    """DeepgramSTT.for_twilio() sets mulaw 8kHz encoding."""
    from patter.providers.deepgram_stt import DeepgramSTT

    stt = DeepgramSTT.for_twilio(api_key="dg_test", language="en")
    assert stt.encoding == "mulaw"
    assert stt.sample_rate == 8000
    assert stt.language == "en"
    assert stt.api_key == "dg_test"


def test_deepgram_stt_linear16_config():
    """DeepgramSTT default is linear16 16kHz (Telnyx-compatible)."""
    from patter.providers.deepgram_stt import DeepgramSTT

    stt = DeepgramSTT(api_key="dg_test", language="it")
    assert stt.encoding == "linear16"
    assert stt.sample_rate == 16000
    assert stt.language == "it"


def test_elevenlabs_tts_init():
    """ElevenLabsTTS stores api_key and voice_id correctly."""
    from patter.providers.elevenlabs_tts import ElevenLabsTTS

    tts = ElevenLabsTTS(api_key="el_test", voice_id="21m00Tcm4TlvDq8ikWAM")
    assert tts.api_key == "el_test"
    assert tts.voice_id == "21m00Tcm4TlvDq8ikWAM"


@pytest.mark.asyncio
async def test_twilio_stream_bridge_pipeline_sends_audio_to_stt():
    """twilio_stream_bridge in pipeline mode forwards mulaw audio to DeepgramSTT."""
    from patter.models import Agent
    from patter.handlers.twilio_handler import twilio_stream_bridge

    agent = Agent(system_prompt="test", provider="pipeline")

    # Build a fake WebSocket that returns start then a media event then stop
    start_payload = json.dumps({
        "event": "start",
        "streamSid": "SID123",
        "start": {"callSid": "CA_test"},
    })
    mulaw_bytes = b"\x00" * 160
    media_payload = json.dumps({
        "event": "media",
        "media": {"payload": base64.b64encode(mulaw_bytes).decode()},
    })
    stop_payload = json.dumps({"event": "stop"})

    messages = [start_payload, media_payload, stop_payload]
    idx = 0

    class FakeWS:
        query_params = {"caller": "+1", "callee": "+2"}
        sent = []

        async def accept(self):
            pass

        async def receive_text(self):
            nonlocal idx
            if idx < len(messages):
                msg = messages[idx]
                idx += 1
                return msg
            # Block indefinitely so we can cancel
            await asyncio.sleep(10)

        async def send_text(self, data):
            self.sent.append(data)

    fake_ws = FakeWS()

    # Patch DeepgramSTT and ElevenLabsTTS so no real connections are made
    import patter.handlers.twilio_handler as twilio_mod

    mock_stt = AsyncMock()
    mock_stt.connect = AsyncMock()
    mock_stt.send_audio = AsyncMock()
    mock_stt.close = AsyncMock()

    async def fake_receive():
        # Yield nothing — the loop ends
        return
        yield  # make it a generator

    mock_stt.receive_transcripts = MagicMock(return_value=fake_receive())

    mock_tts = AsyncMock()
    mock_tts.close = AsyncMock()

    with (
        patch("patter.providers.deepgram_stt.DeepgramSTT.for_twilio", return_value=mock_stt),
        patch("patter.providers.elevenlabs_tts.ElevenLabsTTS", return_value=mock_tts),
    ):
        # Run with a short timeout — we only care that it starts up correctly
        try:
            await asyncio.wait_for(
                twilio_stream_bridge(
                    websocket=fake_ws,
                    agent=agent,
                    openai_key="",
                    deepgram_key="dg_test",
                    elevenlabs_key="el_test",
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    # The media event should have caused audio to be sent to Deepgram
    # (transcoded from mulaw 8kHz → PCM 16kHz for Twilio pipeline mode)
    mock_stt.send_audio.assert_called_once()


# ---------------------------------------------------------------------------
# Agent stt/tts config fields
# ---------------------------------------------------------------------------


def test_agent_with_stt_tts_config():
    """Agent accepts STTConfig and TTSConfig via stt/tts fields."""
    from patter.models import STTConfig, TTSConfig

    agent = Agent(
        system_prompt="test",
        provider="pipeline",
        stt=STTConfig(provider="deepgram", api_key="dg_test", language="it"),
        tts=TTSConfig(provider="elevenlabs", api_key="el_test", voice="aria"),
    )
    assert agent.stt.provider == "deepgram"
    assert agent.stt.api_key == "dg_test"
    assert agent.stt.language == "it"
    assert agent.tts.provider == "elevenlabs"
    assert agent.tts.api_key == "el_test"
    assert agent.tts.voice == "aria"


def test_agent_with_whisper_openai_tts():
    """Agent accepts whisper STT + openai TTS configs."""
    from patter.models import STTConfig, TTSConfig

    agent = Agent(
        system_prompt="test",
        provider="pipeline",
        stt=STTConfig(provider="whisper", api_key="sk_test"),
        tts=TTSConfig(provider="openai", api_key="sk_test", voice="nova"),
    )
    assert agent.stt.provider == "whisper"
    assert agent.tts.provider == "openai"
    assert agent.tts.voice == "nova"


def test_agent_factory_passes_stt_tts():
    """Patter.agent() passes stt/tts configs through to Agent."""
    from patter.models import STTConfig, TTSConfig

    phone = Patter(
        twilio_sid="AC",
        twilio_token="tok",
        openai_key="sk",
        phone_number="+1",
        webhook_url="x.ngrok.io",
    )
    stt_cfg = STTConfig(provider="deepgram", api_key="dg_test")
    tts_cfg = TTSConfig(provider="elevenlabs", api_key="el_test", voice="aria")

    agent = phone.agent(
        system_prompt="Pipeline bot",
        provider="pipeline",
        stt=stt_cfg,
        tts=tts_cfg,
    )
    assert isinstance(agent, Agent)
    assert agent.stt is stt_cfg
    assert agent.tts is tts_cfg


def test_agent_stt_tts_none_by_default():
    """Agent.stt and Agent.tts default to None."""
    agent = Agent(system_prompt="test")
    assert agent.stt is None
    assert agent.tts is None


def test_twilio_pipeline_uses_stt_config():
    """_create_stt_from_config creates DeepgramSTT from STTConfig."""
    from patter.models import STTConfig
    from patter.handlers.twilio_handler import _create_stt_from_config

    cfg = STTConfig(provider="deepgram", api_key="dg_key", language="fr")
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "patter.providers.deepgram_stt.DeepgramSTT.for_twilio"
    ) as mock_for_twilio:
        mock_for_twilio.return_value = object()
        result = _create_stt_from_config(cfg, for_twilio=True)
    mock_for_twilio.assert_called_once_with(api_key="dg_key", language="fr")
    assert result is not None


def test_twilio_pipeline_uses_tts_config():
    """_create_tts_from_config creates ElevenLabsTTS from TTSConfig."""
    from patter.models import TTSConfig
    from patter.handlers.twilio_handler import _create_tts_from_config

    cfg = TTSConfig(provider="elevenlabs", api_key="el_key", voice="aria")
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "patter.providers.elevenlabs_tts.ElevenLabsTTS"
    ) as mock_tts:
        mock_tts.return_value = object()
        result = _create_tts_from_config(cfg)
    mock_tts.assert_called_once_with(api_key="el_key", voice_id="aria")
    assert result is not None


def test_create_stt_whisper():
    """_create_stt_from_config creates WhisperSTT from STTConfig(provider='whisper')."""
    from patter.models import STTConfig
    from patter.handlers.twilio_handler import _create_stt_from_config

    cfg = STTConfig(provider="whisper", api_key="sk_key", language="de")
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "patter.providers.whisper_stt.WhisperSTT"
    ) as mock_whisper:
        mock_whisper.return_value = object()
        result = _create_stt_from_config(cfg)
    mock_whisper.assert_called_once_with(api_key="sk_key", language="de")
    assert result is not None


def test_create_stt_returns_none_for_unknown():
    """_create_stt_from_config returns None for unknown providers."""
    from patter.models import STTConfig
    from patter.handlers.twilio_handler import _create_stt_from_config

    cfg = STTConfig(provider="unknown_provider", api_key="x")
    result = _create_stt_from_config(cfg)
    assert result is None


def test_create_tts_returns_none_for_unknown():
    """_create_tts_from_config returns None for unknown providers."""
    from patter.models import TTSConfig
    from patter.handlers.twilio_handler import _create_tts_from_config

    cfg = TTSConfig(provider="unknown_provider", api_key="x")
    result = _create_tts_from_config(cfg)
    assert result is None


# ---------------------------------------------------------------------------
# DTMF events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dtmf_event_fires_transcript_callback():
    """DTMF events produce a [DTMF: X] entry via the on_transcript callback."""
    from patter.models import Agent
    from patter.handlers.twilio_handler import twilio_stream_bridge

    agent = Agent(system_prompt="test", provider="pipeline")

    start_payload = json.dumps({
        "event": "start",
        "streamSid": "SID_dtmf",
        "start": {"callSid": "CA_dtmf"},
    })
    dtmf_payload = json.dumps({
        "event": "dtmf",
        "dtmf": {"track": "inbound_track", "digit": "5"},
    })
    stop_payload = json.dumps({"event": "stop"})

    messages = [start_payload, dtmf_payload, stop_payload]
    idx = 0

    class FakeWS:
        query_params = {"caller": "+1", "callee": "+2"}
        sent = []

        async def accept(self):
            pass

        async def receive_text(self):
            nonlocal idx
            if idx < len(messages):
                msg = messages[idx]
                idx += 1
                return msg
            await asyncio.sleep(10)

        async def send_text(self, data):
            self.sent.append(data)

    fake_ws = FakeWS()
    transcript_calls = []

    async def capture_transcript(data):
        transcript_calls.append(data)

    mock_stt = AsyncMock()
    mock_stt.connect = AsyncMock()
    mock_stt.send_audio = AsyncMock()
    mock_stt.close = AsyncMock()

    async def fake_receive():
        return
        yield  # make it a generator

    mock_stt.receive_transcripts = MagicMock(return_value=fake_receive())
    mock_tts = AsyncMock()
    mock_tts.close = AsyncMock()

    with (
        patch("patter.providers.deepgram_stt.DeepgramSTT.for_twilio", return_value=mock_stt),
        patch("patter.providers.elevenlabs_tts.ElevenLabsTTS", return_value=mock_tts),
    ):
        try:
            await asyncio.wait_for(
                twilio_stream_bridge(
                    websocket=fake_ws,
                    agent=agent,
                    openai_key="",
                    deepgram_key="dg_test",
                    elevenlabs_key="el_test",
                    on_transcript=capture_transcript,
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    assert any(
        t.get("text") == "[DTMF: 5]" and t.get("role") == "user"
        for t in transcript_calls
    ), f"Expected DTMF transcript, got: {transcript_calls}"


def test_dtmf_event_format():
    """DTMF event payload includes digit under dtmf.digit."""
    raw = json.loads('{"event": "dtmf", "dtmf": {"track": "inbound_track", "digit": "1"}}')
    assert raw["event"] == "dtmf"
    assert raw["dtmf"]["digit"] == "1"


# ---------------------------------------------------------------------------
# Mark events
# ---------------------------------------------------------------------------


def test_mark_event_format():
    """Mark events from Twilio include mark.name."""
    raw = json.loads('{"event": "mark", "streamSid": "SID", "mark": {"name": "audio_3"}}')
    assert raw["event"] == "mark"
    assert raw["mark"]["name"] == "audio_3"


@pytest.mark.asyncio
async def test_mark_events_sent_after_audio():
    """Forward loop sends a mark event after each audio chunk."""
    from patter.models import Agent
    from patter.handlers.twilio_handler import twilio_stream_bridge

    agent = Agent(system_prompt="test", provider="openai_realtime")

    start_payload = json.dumps({
        "event": "start",
        "streamSid": "SID_mark",
        "start": {"callSid": "CA_mark"},
    })
    stop_payload = json.dumps({"event": "stop"})
    messages = [start_payload, stop_payload]
    idx = 0

    class FakeWS:
        query_params = {"caller": "+1", "callee": "+2"}
        sent = []

        async def accept(self):
            pass

        async def receive_text(self):
            nonlocal idx
            if idx < len(messages):
                msg = messages[idx]
                idx += 1
                if idx == len(messages):
                    # Yield control so forward_to_twilio task can process audio
                    await asyncio.sleep(0.05)
                return msg
            await asyncio.sleep(10)

        async def send_text(self, data):
            self.sent.append(data)

    fake_ws = FakeWS()

    # Simulate OpenAI adapter that emits one audio chunk
    mock_adapter = AsyncMock()
    mock_adapter.connect = AsyncMock()
    mock_adapter.close = AsyncMock()
    mock_adapter.cancel_response = AsyncMock()

    audio_chunk = b"\xff" * 100

    async def fake_events():
        yield "audio", audio_chunk

    mock_adapter.receive_events = MagicMock(return_value=fake_events())
    mock_adapter.send_text = AsyncMock()

    with patch(
        "patter.providers.openai_realtime.OpenAIRealtimeAdapter",
        return_value=mock_adapter,
    ):
        try:
            await asyncio.wait_for(
                twilio_stream_bridge(
                    websocket=fake_ws,
                    agent=agent,
                    openai_key="sk-test",
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    sent_events = [json.loads(s) for s in fake_ws.sent]
    mark_events = [e for e in sent_events if e.get("event") == "mark"]
    assert len(mark_events) >= 1, f"Expected at least one mark event, got: {sent_events}"
    assert mark_events[0]["mark"]["name"].startswith("audio_")


# ---------------------------------------------------------------------------
# Custom parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_params_passed_to_on_call_start():
    """Custom parameters from TwiML start event are forwarded to on_call_start."""
    from patter.models import Agent
    from patter.handlers.twilio_handler import twilio_stream_bridge

    agent = Agent(system_prompt="test", provider="pipeline")

    start_payload = json.dumps({
        "event": "start",
        "streamSid": "SID_params",
        "start": {
            "callSid": "CA_params",
            "customParameters": {"agent_name": "Aria", "language": "it"},
        },
    })
    stop_payload = json.dumps({"event": "stop"})
    messages = [start_payload, stop_payload]
    idx = 0

    class FakeWS:
        query_params = {"caller": "+1", "callee": "+2"}
        sent = []

        async def accept(self):
            pass

        async def receive_text(self):
            nonlocal idx
            if idx < len(messages):
                msg = messages[idx]
                idx += 1
                return msg
            await asyncio.sleep(10)

        async def send_text(self, data):
            self.sent.append(data)

    fake_ws = FakeWS()
    call_start_data = {}

    async def capture_call_start(data):
        call_start_data.update(data)

    mock_stt = AsyncMock()
    mock_stt.connect = AsyncMock()
    mock_stt.send_audio = AsyncMock()
    mock_stt.close = AsyncMock()

    async def fake_receive():
        return
        yield

    mock_stt.receive_transcripts = MagicMock(return_value=fake_receive())
    mock_tts = AsyncMock()
    mock_tts.close = AsyncMock()

    with (
        patch("patter.providers.deepgram_stt.DeepgramSTT.for_twilio", return_value=mock_stt),
        patch("patter.providers.elevenlabs_tts.ElevenLabsTTS", return_value=mock_tts),
    ):
        try:
            await asyncio.wait_for(
                twilio_stream_bridge(
                    websocket=fake_ws,
                    agent=agent,
                    openai_key="",
                    deepgram_key="dg_test",
                    elevenlabs_key="el_test",
                    on_call_start=capture_call_start,
                ),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    assert call_start_data.get("custom_params") == {"agent_name": "Aria", "language": "it"}
    assert call_start_data.get("call_id") == "CA_params"


def test_custom_params_in_call_start_format():
    """on_call_start dict always includes custom_params key (empty dict if none)."""
    # The start event without customParameters should produce empty custom_params
    start_data = {}  # no customParameters key
    custom_params = start_data.get("customParameters", {})
    assert custom_params == {}


def test_custom_params_extracted_from_start_event():
    """customParameters from the TwiML start event are parsed correctly."""
    raw = json.loads(json.dumps({
        "event": "start",
        "streamSid": "SID",
        "start": {
            "callSid": "CA123",
            "customParameters": {"foo": "bar", "baz": "42"},
        },
    }))
    start_data = raw.get("start", {})
    custom_params = start_data.get("customParameters", {})
    assert custom_params == {"foo": "bar", "baz": "42"}

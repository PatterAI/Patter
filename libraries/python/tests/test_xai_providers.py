"""Tests for the xAI (Grok) voice providers: STT, TTS, and Realtime.

Protocol-level unit tests — URL/query construction, event parsing, session
config wire shape, and pricing entries. No network access: the WebSocket is
faked where message flow is exercised.
"""

import base64
import json

import pytest

from getpatter.pricing import (
    DEFAULT_PRICING,
    calculate_realtime_minute_cost,
    calculate_stt_cost,
    calculate_tts_cost,
    merge_pricing,
)
from getpatter.providers.xai_realtime import (
    XAIRealtimeAdapter,
    XAIRealtimeAudioFormat,
    XAIRealtimeModel,
)
from getpatter.providers.xai_stt import XAISTT, XAISTTEncoding, XAISTTSampleRate
from getpatter.providers.xai_tts import XAITTS, XAITTSCodec, XAIVoice


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------


class TestXAISTTUrl:
    def test_default_url(self):
        stt = XAISTT(api_key="xai-test")
        url = stt._build_url()
        assert url.startswith("wss://api.x.ai/v1/stt?")
        assert "sample_rate=16000" in url
        assert "encoding=pcm" in url
        assert "interim_results=true" in url
        assert "language=en" in url

    def test_keyterms_repeated(self):
        stt = XAISTT(api_key="xai-test", keyterms=["Patter", "Grok"])
        url = stt._build_url()
        assert url.count("keyterm=") == 2

    def test_no_language_omits_param(self):
        stt = XAISTT(api_key="xai-test", language=None)
        assert "language=" not in stt._build_url()

    def test_smart_turn_params(self):
        stt = XAISTT(api_key="xai-test", smart_turn=0.7, smart_turn_timeout_ms=3000)
        url = stt._build_url()
        assert "smart_turn=0.7" in url
        assert "smart_turn_timeout=3000" in url

    def test_smart_turn_timeout_requires_smart_turn(self):
        # Timeout without the threshold is silently omitted from the URL.
        stt = XAISTT(api_key="xai-test", smart_turn_timeout_ms=3000)
        assert "smart_turn" not in stt._build_url()

    def test_for_twilio_mulaw_8k(self):
        stt = XAISTT.for_twilio(api_key="xai-test")
        assert stt.encoding == XAISTTEncoding.MULAW
        assert stt.sample_rate == XAISTTSampleRate.HZ_8000

    def test_keyterm_limits_enforced(self):
        with pytest.raises(ValueError):
            XAISTT(api_key="xai-test", keyterms=["x" * 51])
        with pytest.raises(ValueError):
            XAISTT(api_key="xai-test", keyterms=["k"] * 101)

    def test_smart_turn_range_enforced(self):
        with pytest.raises(ValueError):
            XAISTT(api_key="xai-test", smart_turn=1.5)


class TestXAISTTParsing:
    def _stt(self) -> XAISTT:
        return XAISTT(api_key="xai-test")

    def test_partial_interim(self):
        t = self._stt()._parse_message(
            json.dumps(
                {
                    "type": "transcript.partial",
                    "text": "hello wor",
                    "is_final": False,
                    "speech_final": False,
                }
            )
        )
        assert t is not None
        assert t.text == "hello wor"
        assert t.is_final is False
        assert t.speech_final is False

    def test_partial_utterance_final(self):
        t = self._stt()._parse_message(
            json.dumps(
                {
                    "type": "transcript.partial",
                    "text": "hello world",
                    "is_final": True,
                    "speech_final": True,
                    "words": [{"text": "hello", "start": 0.1, "end": 0.4}],
                }
            )
        )
        assert t is not None
        assert t.is_final is True
        assert t.speech_final is True
        assert t.words[0]["text"] == "hello"

    def test_speech_final_implies_is_final(self):
        # Defensive: speech_final without is_final still counts as final.
        t = self._stt()._parse_message(
            json.dumps(
                {
                    "type": "transcript.partial",
                    "text": "done",
                    "is_final": False,
                    "speech_final": True,
                }
            )
        )
        assert t.is_final is True

    def test_end_of_turn_confidence_surfaced(self):
        t = self._stt()._parse_message(
            json.dumps(
                {
                    "type": "transcript.partial",
                    "text": "two of those",
                    "is_final": True,
                    "speech_final": True,
                    "end_of_turn_confidence": 0.983,
                }
            )
        )
        assert t.confidence == pytest.approx(0.983)

    def test_transcript_done(self):
        t = self._stt()._parse_message(
            json.dumps({"type": "transcript.done", "text": "full text", "duration": 3.2})
        )
        assert t is not None
        assert t.is_final is True
        assert t.speech_final is True

    def test_empty_text_skipped(self):
        assert (
            self._stt()._parse_message(
                json.dumps({"type": "transcript.partial", "text": "  "})
            )
            is None
        )

    def test_error_event_returns_none(self):
        assert (
            self._stt()._parse_message(
                json.dumps({"type": "error", "message": "boom"})
            )
            is None
        )

    def test_created_event_returns_none(self):
        assert (
            self._stt()._parse_message(json.dumps({"type": "transcript.created"}))
            is None
        )


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


class TestXAITTS:
    def test_default_ws_url(self):
        tts = XAITTS(api_key="xai-test")
        url = tts._build_ws_url()
        assert url.startswith("wss://api.x.ai/v1/tts?")
        assert "voice=eve" in url
        assert "language=en" in url
        assert "codec=pcm" in url
        assert "sample_rate=16000" in url

    def test_optional_params_in_url(self):
        tts = XAITTS(
            api_key="xai-test",
            voice=XAIVoice.ARA,
            codec=XAITTSCodec.MP3,
            sample_rate=24000,
            bit_rate=192000,
            speed=1.2,
            text_normalization=True,
        )
        url = tts._build_ws_url()
        assert "voice=ara" in url
        assert "codec=mp3" in url
        assert "bit_rate=192000" in url
        assert "speed=1.2" in url
        assert "text_normalization=true" in url

    def test_http_payload_shape(self):
        tts = XAITTS(api_key="xai-test", voice="ara", codec="mp3", sample_rate=44100, bit_rate=192000)
        payload = tts._build_http_payload("Ciao!")
        assert payload["text"] == "Ciao!"
        assert payload["voice_id"] == "ara"
        assert payload["language"] == "en"
        assert payload["output_format"] == {
            "codec": "mp3",
            "sample_rate": 44100,
            "bit_rate": 192000,
        }

    def test_for_twilio_is_mulaw_8k_passthrough(self):
        tts = XAITTS.for_twilio(api_key="xai-test")
        fmt = tts.source_audio_format()
        assert fmt.encoding == "mulaw"
        assert fmt.sample_rate == 8000

    def test_carrier_autodetect_flips_unpinned_format(self):
        tts = XAITTS(api_key="xai-test")
        tts.set_telephony_carrier("twilio")
        assert tts.codec == XAITTSCodec.MULAW
        assert int(tts.sample_rate) == 8000

    def test_carrier_autodetect_respects_explicit_format(self):
        tts = XAITTS.for_twilio(api_key="xai-test")
        tts.codec = XAITTSCodec.PCM  # simulate user pinning
        tts.set_telephony_carrier("telnyx")
        assert tts.codec == XAITTSCodec.PCM

    def test_speed_range_enforced(self):
        with pytest.raises(ValueError):
            XAITTS(api_key="xai-test", speed=2.0)

    def test_text_length_cap(self):
        tts = XAITTS(api_key="xai-test")

        async def _consume():
            async for _ in tts.synthesize("x" * 15_001):
                pass

        import asyncio

        with pytest.raises(ValueError):
            asyncio.run(_consume())

    def test_voice_roster_has_26_voices_with_eve_default(self):
        assert len(XAIVoice) == 26
        assert XAIVoice.EVE == "eve"
        tts = XAITTS(api_key="xai-test")
        assert tts.voice == "eve"


# ---------------------------------------------------------------------------
# Realtime
# ---------------------------------------------------------------------------


class TestXAIRealtimeSessionConfig:
    def test_default_session_config(self):
        adapter = XAIRealtimeAdapter(api_key="xai-test")
        config = adapter._build_session_config()
        assert config["voice"] == "eve"
        assert config["turn_detection"] == {"type": "server_vad"}
        assert config["audio"]["input"]["format"] == {"type": "audio/pcmu"}
        assert config["audio"]["output"]["format"] == {"type": "audio/pcmu"}
        assert "reasoning" not in config

    def test_pcm_format_includes_rate(self):
        adapter = XAIRealtimeAdapter(
            api_key="xai-test",
            audio_format=XAIRealtimeAudioFormat.AUDIO_PCM,
            sample_rate=16000,
        )
        config = adapter._build_session_config()
        assert config["audio"]["input"]["format"] == {
            "type": "audio/pcm",
            "rate": 16000,
        }

    def test_transcription_biasing_and_replace(self):
        adapter = XAIRealtimeAdapter(
            api_key="xai-test",
            language_hint="it",
            keyterms=["Patter"],
            replace={"Acme Mobile": "Acme Mobull"},
            reasoning_effort="none",
            output_speed=1.1,
        )
        config = adapter._build_session_config()
        transcription = config["audio"]["input"]["transcription"]
        assert transcription["language_hint"] == "it"
        assert transcription["keyterms"] == ["Patter"]
        assert config["audio"]["output"]["speed"] == 1.1
        assert config["replace"] == {"Acme Mobile": "Acme Mobull"}
        assert config["reasoning"] == {"effort": "none"}

    def test_function_tools_wrapped_server_tools_passthrough(self):
        adapter = XAIRealtimeAdapter(
            api_key="xai-test",
            tools=[
                {"name": "lookup", "description": "d", "parameters": {"type": "object"}},
                {"type": "web_search"},
                {"type": "file_search", "vector_store_ids": ["c1"]},
            ],
        )
        tools = adapter._build_session_config()["tools"]
        assert tools[0]["type"] == "function"
        assert tools[0]["name"] == "lookup"
        assert tools[1] == {"type": "web_search"}
        assert tools[2]["vector_store_ids"] == ["c1"]

    def test_url_carries_model(self):
        adapter = XAIRealtimeAdapter(
            api_key="xai-test", model=XAIRealtimeModel.GROK_VOICE_THINK_FAST_1_0
        )
        assert (
            adapter._build_url()
            == "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-1.0"
        )

    def test_output_speed_range_enforced(self):
        with pytest.raises(ValueError):
            XAIRealtimeAdapter(api_key="xai-test", output_speed=0.5)


class TestXAIRealtimeEvents:
    @pytest.mark.asyncio
    async def test_receive_events_maps_ga_event_names(self):
        adapter = XAIRealtimeAdapter(api_key="xai-test")
        audio_b64 = base64.b64encode(b"\x00" * 160).decode()
        frames = [
            json.dumps({"type": "response.output_audio.delta", "delta": audio_b64}),
            json.dumps({"type": "response.output_audio_transcript.delta", "delta": "Hi"}),
            json.dumps({"type": "input_audio_buffer.speech_started"}),
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "hello",
                }
            ),
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "call_id": "c1",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ),
            json.dumps({"type": "response.done", "response": {"id": "r1"}}),
        ]

        class FakeWS:
            def __init__(self, messages):
                self._messages = list(messages)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._messages:
                    raise StopAsyncIteration
                return self._messages.pop(0)

        adapter._ws = FakeWS(frames)
        events = [event async for event in adapter.receive_events()]
        types = [e[0] for e in events]
        assert types == [
            "audio",
            "transcript_output",
            "speech_started",
            "transcript_input",
            "function_call",
            "response_done",
        ]
        assert events[0][1] == b"\x00" * 160
        assert events[4][1]["name"] == "lookup"

    @pytest.mark.asyncio
    async def test_send_text_counts_billable_messages(self):
        adapter = XAIRealtimeAdapter(api_key="xai-test")

        class FakeSendWS:
            def __init__(self):
                self.sent = []

            async def send(self, data):
                self.sent.append(json.loads(data))

        ws = FakeSendWS()
        adapter._ws = ws
        await adapter.send_text("ciao")
        assert adapter._text_input_messages == 1
        assert ws.sent[0]["type"] == "conversation.item.create"
        assert ws.sent[1]["type"] == "response.create"

    @pytest.mark.asyncio
    async def test_send_function_result_shape(self):
        adapter = XAIRealtimeAdapter(api_key="xai-test")

        class FakeSendWS:
            def __init__(self):
                self.sent = []

            async def send(self, data):
                self.sent.append(json.loads(data))

        ws = FakeSendWS()
        adapter._ws = ws
        await adapter.send_function_result("c1", '{"ok": true}')
        item = ws.sent[0]["item"]
        assert item["type"] == "function_call_output"
        assert item["call_id"] == "c1"

    @pytest.mark.asyncio
    async def test_cancel_response_noop_without_inflight_item(self):
        adapter = XAIRealtimeAdapter(api_key="xai-test")

        class FakeSendWS:
            def __init__(self):
                self.sent = []

            async def send(self, data):
                self.sent.append(json.loads(data))

        ws = FakeSendWS()
        adapter._ws = ws
        await adapter.cancel_response()
        assert ws.sent == []


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class TestXAIPricing:
    def test_default_entries_present(self):
        assert DEFAULT_PRICING["xai_stt"]["unit"] == "minute"
        # $0.20/hr streaming rate.
        assert DEFAULT_PRICING["xai_stt"]["price"] == pytest.approx(0.20 / 60)
        # $15/1M chars = $0.015/1k.
        assert DEFAULT_PRICING["xai_tts"]["price"] == pytest.approx(0.015)
        assert DEFAULT_PRICING["xai_realtime"]["price"] == pytest.approx(0.05)
        assert DEFAULT_PRICING["xai_realtime"]["text_input_per_message"] == pytest.approx(
            0.004
        )

    def test_stt_cost_one_hour(self):
        pricing = merge_pricing(None)
        assert calculate_stt_cost("xai_stt", 3600, pricing) == pytest.approx(0.20)

    def test_tts_cost_one_million_chars(self):
        pricing = merge_pricing(None)
        assert calculate_tts_cost("xai_tts", 1_000_000, pricing) == pytest.approx(15.0)

    def test_realtime_minute_cost(self):
        pricing = merge_pricing(None)
        # 10 minutes + 3 text messages: 10*0.05 + 3*0.004 = 0.512
        assert calculate_realtime_minute_cost(
            "xai_realtime", 600, pricing, text_input_messages=3
        ) == pytest.approx(0.512)

    def test_realtime_minute_cost_unknown_provider(self):
        pricing = merge_pricing(None)
        assert calculate_realtime_minute_cost("nope", 600, pricing) == 0.0

    def test_provider_keys_match_pricing(self):
        from getpatter.stt.xai import STT as XAISTTShim
        from getpatter.tts.xai import TTS as XAITTSShim

        assert XAISTTShim.provider_key in DEFAULT_PRICING
        assert XAITTSShim.provider_key in DEFAULT_PRICING
        assert XAISTT.provider_key == "xai_stt"
        assert XAITTS.provider_key == "xai_tts"


class TestXAIEnvFallback:
    def test_stt_env_fallback(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "xai-env")
        from getpatter.stt.xai import STT

        assert STT().api_key == "xai-env"

    def test_stt_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        from getpatter.stt.xai import STT

        with pytest.raises(ValueError, match="XAI_API_KEY"):
            STT()

    def test_tts_env_fallback(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "xai-env")
        from getpatter.tts.xai import TTS

        assert TTS().api_key == "xai-env"

    def test_flat_aliases_exported(self):
        import getpatter

        assert getpatter.XAISTT is not None
        assert getpatter.XAITTS is not None
        assert getpatter.XAIRealtimeAdapter is XAIRealtimeAdapter

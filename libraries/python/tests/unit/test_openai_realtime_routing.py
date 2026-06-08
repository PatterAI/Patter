"""Regression guard for issue #154 — Twilio + OpenAI Realtime garbled audio.

The ``openai_realtime`` provider mode routes through the GA adapter
(:class:`~getpatter.providers.openai_realtime_2.OpenAIRealtime2Adapter`), which
sends the nested ``audio.{input,output}.format = {"type": "audio/pcm",
"rate": 24000}`` session shape and transcodes PCM24->mulaw8 internally, so the
carrier always receives valid mulaw. OpenAI deprecated the Beta Realtime API:
its flat ``output_audio_format: g711_ulaw`` shape is ignored by GA models, which
fall back to PCM16 @ 24 kHz — the v1-beta path then forwarded those bytes to
Twilio framed as 8 kHz mulaw, producing static + broken STT.

Parity test: ``libraries/typescript/tests/openai-realtime-routing.test.ts``.
"""

import base64
import inspect
import math
import struct

import pytest

from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter
from getpatter.providers.openai_realtime_2 import OpenAIRealtime2Adapter


@pytest.mark.unit
class TestOpenAIRealtimeRouting:
    def test_ga_adapter_subclasses_v1_adapter(self) -> None:
        # The stream handler gates Realtime features (barge-in, send_text,
        # cancel_response, …) behind isinstance(adapter, OpenAIRealtimeAdapter).
        # The GA adapter MUST stay a subclass so routing openai_realtime through
        # it keeps those gates live — parity with the TS `instanceof` checks.
        assert issubclass(OpenAIRealtime2Adapter, OpenAIRealtimeAdapter)

    def test_stream_handler_routes_openai_realtime_through_ga_adapter(self) -> None:
        # Both engines resolve to the GA adapter in OpenAIRealtimeStreamHandler.
        # Guard the unified routing so it can't silently regress to the v1-beta
        # path (which sent the deprecated flat g711_ulaw shape -> garbled audio).
        # Python has no standalone adapter factory like the TS ``buildAIAdapter``
        # (the class is chosen inside ``start()``), so this is a lightweight
        # reference check — robust to local renames/formatting, and trips only if
        # the GA adapter stops being used by ``start()`` at all. The behavioural
        # coverage (subclass + GA session shape + transcode) lives in the other
        # tests; the TS parity test asserts the routing via ``instanceof``.
        from getpatter import stream_handler

        src = inspect.getsource(stream_handler.OpenAIRealtimeStreamHandler.start)
        assert "OpenAIRealtime2Adapter" in src

    def test_ga_session_config_uses_nested_pcm_format_not_flat_g711(self) -> None:
        adapter = OpenAIRealtime2Adapter(api_key="sk-test")
        config = adapter._build_ga_session_config()
        assert config["type"] == "realtime"
        assert config["audio"]["output"]["format"] == {
            "type": "audio/pcm",
            "rate": 24000,
        }
        assert config["audio"]["input"]["format"] == {
            "type": "audio/pcm",
            "rate": 24000,
        }
        # The bug was the flat field reaching a GA model — it must NOT appear.
        assert "output_audio_format" not in config
        assert "input_audio_format" not in config


@pytest.mark.unit
class TestOpenAIRealtimeOutboundTranscode:
    def test_pcm24_to_mulaw8_downsamples_to_eighth_byte_rate(self) -> None:
        adapter = OpenAIRealtime2Adapter(api_key="sk-test")
        # 1 s of a 440 Hz tone at 24 kHz, PCM16-LE — the format GA returns.
        n = 24000
        pcm24 = struct.pack(
            f"<{n}h",
            *[int(16383 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(n)],
        )
        mulaw = adapter._transcode_outbound_pcm24_to_mulaw8(
            base64.b64encode(pcm24).decode("ascii")
        )
        assert len(mulaw) > 0
        # mulaw 8 kHz (1 byte/sample) is far smaller than the PCM16 24 kHz input.
        assert len(mulaw) < len(pcm24) // 2
        # 24 kHz -> 8 kHz ≈ 8000 mulaw bytes for 1 s; allow ±10% for warm-up.
        assert abs(len(mulaw) - n // 3) < (n // 3) * 0.1

"""Unit tests for the xAI Grok Voice Agent realtime engine.

These assert the adapter's endpoint / provider key / defaults, the
``session.update`` body produced by ``_build_ga_session_config`` (no WebSocket is
opened, so no boundary is mocked), the engine-marker → provider unpack, and the
three pricing entries. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import pytest

from getpatter.client import Patter
from getpatter.engines.xai import XaiRealtime
from getpatter.pricing import (
    calculate_realtime_cost,
    calculate_stt_cost,
    calculate_tts_cost,
    merge_pricing,
)
from getpatter.providers.xai_realtime import XaiRealtimeAdapter


def _adapter(**kwargs) -> XaiRealtimeAdapter:
    return XaiRealtimeAdapter(api_key="xai-test-key", **kwargs)


# ---------------------------------------------------------------------------
# Endpoint / provider key / defaults
# ---------------------------------------------------------------------------


def test_endpoint_is_xai_and_provider_key_matches() -> None:
    a = _adapter()
    assert a.OPENAI_REALTIME_URL == "wss://api.x.ai/v1/realtime"
    assert a.provider_key == "xai_realtime"


def test_defaults_are_grok_voice_latest_and_eve() -> None:
    a = _adapter()
    assert a.model == "grok-voice-latest"
    assert a.voice == "eve"
    # xAI-valid transcription model (NOT OpenAI's whisper-1).
    assert a.input_audio_transcription_model == "grok-transcribe"


def test_explicit_model_and_voice_win_over_defaults() -> None:
    a = _adapter(model="grok-voice-think-fast-1.0", voice="ara")
    assert a.model == "grok-voice-think-fast-1.0"
    assert a.voice == "ara"


# ---------------------------------------------------------------------------
# session.update grafting — keys PRESENT when set
# ---------------------------------------------------------------------------


def test_session_update_grafts_xai_keys_when_set() -> None:
    a = _adapter(
        reasoning_effort="none",
        vad_threshold=0.6,
        prefix_padding_ms=400,
        idle_timeout_ms=8000,
        language_hint="es-MX",
        keyterms=("Grok", "xAI"),
        speed=1.1,
        replace={"Acme Mobile": "Acme Mobull"},
        resumption=True,
        server_tools=({"type": "web_search"},),
    )
    cfg = a._build_ga_session_config()
    inp = cfg["audio"]["input"]
    assert cfg["reasoning"] == {"effort": "none"}
    assert inp["turn_detection"]["threshold"] == 0.6
    assert inp["turn_detection"]["prefix_padding_ms"] == 400
    assert inp["turn_detection"]["idle_timeout_ms"] == 8000
    assert inp["transcription"]["language_hint"] == "es-MX"
    assert inp["transcription"]["keyterms"] == ["Grok", "xAI"]
    assert cfg["audio"]["output"]["speed"] == 1.1
    assert cfg["replace"] == {"Acme Mobile": "Acme Mobull"}
    assert cfg["resumption"] == {"enabled": True}
    assert {"type": "web_search"} in cfg["tools"]
    # Audio path is inherited from the GA adapter (pcm24 both directions).
    assert inp["format"] == {"type": "audio/pcm", "rate": 24000}


# ---------------------------------------------------------------------------
# session.update grafting — keys OMITTED when unset
# ---------------------------------------------------------------------------


def test_session_update_omits_xai_keys_when_unset() -> None:
    cfg = _adapter()._build_ga_session_config()
    inp = cfg["audio"]["input"]
    assert "reasoning" not in cfg
    assert "replace" not in cfg
    assert "resumption" not in cfg
    assert "speed" not in cfg["audio"]["output"]
    assert "idle_timeout_ms" not in inp["turn_detection"]
    assert "language_hint" not in inp["transcription"]
    assert "keyterms" not in inp["transcription"]
    # The OpenAI-only ``language`` key never leaks into the xAI session.
    assert "language" not in inp["transcription"]


def test_server_tools_append_to_agent_function_tools() -> None:
    # Function tools (from the base builder) + xAI server tools coexist.
    a = _adapter(
        tools=[
            {
                "name": "lookup",
                "description": "look up",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        server_tools=({"type": "web_search"}, {"type": "x_search"}),
    )
    tools = a._build_ga_session_config()["tools"]
    types = [t.get("type") for t in tools]
    assert "function" in types
    assert "web_search" in types
    assert "x_search" in types


# ---------------------------------------------------------------------------
# Engine-marker unpack in the client
# ---------------------------------------------------------------------------


def test_engine_marker_unpacks_to_xai_realtime() -> None:
    marker = XaiRealtime(api_key="xai-test-key", reasoning_effort="none", speed=1.2)
    kind, fields = Patter._unpack_engine(marker)
    assert kind == "xai_realtime"
    assert fields["voice"] == "eve"
    assert fields["model"] == "grok-voice-latest"
    assert fields["reasoning_effort"] == "none"
    assert fields["speed"] == 1.2


def test_engine_marker_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key"):
        XaiRealtime()


# ---------------------------------------------------------------------------
# Pricing math
# ---------------------------------------------------------------------------


def test_pricing_entries_for_all_three_providers() -> None:
    pricing = merge_pricing(None)
    # STT streaming: $0.20/hr → $0.20/60 per minute.
    assert calculate_stt_cost("xai", 60.0, pricing) == pytest.approx(0.20 / 60)
    # TTS: $15/1M chars = $0.015/1k chars.
    assert calculate_tts_cost("xai_tts", 1000, pricing) == pytest.approx(0.015)
    # Realtime: $0.05/min via the MINUTE duration path.
    assert calculate_realtime_cost(
        {}, pricing, provider="xai_realtime", duration_seconds=120.0
    ) == pytest.approx(0.10)


def test_realtime_minute_path_zero_without_duration() -> None:
    pricing = merge_pricing(None)
    assert calculate_realtime_cost({}, pricing, provider="xai_realtime") == 0.0
    # Documented (not metered) per-message constant is on the entry.
    assert pricing["xai_realtime"]["text_input_per_message"] == 0.004


def test_openai_realtime_token_path_unchanged() -> None:
    # Backward-compat: the default token path for existing callers is untouched.
    pricing = merge_pricing(None)
    usage = {
        "input_token_details": {"audio_tokens": 1000, "text_tokens": 0},
        "output_token_details": {"audio_tokens": 500, "text_tokens": 0},
    }
    cost = calculate_realtime_cost(usage, pricing, model="gpt-realtime-mini")
    # 1000 * 1e-5 + 500 * 2e-5 = 0.02
    assert cost == pytest.approx(0.02)

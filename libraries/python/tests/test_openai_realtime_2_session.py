"""Unit tests for the GA (v2) session.update builder — noise reduction +
turn-detection tuning (POINT 1a / 1b).

These assert the dict produced by ``_build_ga_session_config`` directly: the
GA adapter nests both knobs under ``session.audio.input``. No WebSocket is
opened, so no external boundary is mocked and no ``mocked`` marker is needed.
"""

from __future__ import annotations

from getpatter import RealtimeTurnDetection
from getpatter.providers.openai_realtime_2 import OpenAIRealtime2Adapter


def _adapter(**kwargs) -> OpenAIRealtime2Adapter:
    return OpenAIRealtime2Adapter(api_key="sk-test", **kwargs)


def test_ga_noise_reduction_nested_under_audio_input() -> None:
    config = _adapter(noise_reduction="far_field")._build_ga_session_config()
    assert config["audio"]["input"]["input_audio_noise_reduction"] == {
        "type": "far_field"
    }


def test_ga_noise_reduction_omitted_when_unset() -> None:
    config = _adapter()._build_ga_session_config()
    assert "input_audio_noise_reduction" not in config["audio"]["input"]


def test_ga_server_vad_turn_detection_merges_over_defaults() -> None:
    td = RealtimeTurnDetection(
        type="server_vad", threshold=0.6, silence_duration_ms=500
    )
    config = _adapter(turn_detection=td)._build_ga_session_config()
    detection = config["audio"]["input"]["turn_detection"]
    assert detection["type"] == "server_vad"
    assert detection["threshold"] == 0.6
    assert detection["silence_duration_ms"] == 500
    # Unset field falls back to the adapter default (300).
    assert detection["prefix_padding_ms"] == 300
    # Client-gated safety values stay False — never exposed publicly.
    assert detection["create_response"] is False
    assert detection["interrupt_response"] is False


def test_ga_semantic_vad_omits_threshold_fields() -> None:
    td = RealtimeTurnDetection(type="semantic_vad", eagerness="low")
    config = _adapter(turn_detection=td)._build_ga_session_config()
    detection = config["audio"]["input"]["turn_detection"]
    assert detection["type"] == "semantic_vad"
    assert detection["eagerness"] == "low"
    assert detection["create_response"] is False
    assert detection["interrupt_response"] is False
    assert "threshold" not in detection
    assert "prefix_padding_ms" not in detection
    assert "silence_duration_ms" not in detection


def test_ga_defaults_unchanged_when_no_knobs_set() -> None:
    # Regression guard: byte-identical to the pre-change literal.
    adapter = _adapter()
    detection = adapter._build_ga_session_config()["audio"]["input"]["turn_detection"]
    assert detection == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": adapter.silence_duration_ms,
        "create_response": False,
        "interrupt_response": False,
    }

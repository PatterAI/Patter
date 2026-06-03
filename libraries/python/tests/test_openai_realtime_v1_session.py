"""Unit tests for the v1-beta session.update builder — noise reduction +
turn-detection tuning (POINT 1a / 1b).

The v1 shape puts ``input_audio_noise_reduction`` at the TOP LEVEL of
``session`` (NOT nested under ``audio.input`` as the GA shape does), and the
v1 turn_detection carries NO create_response / interrupt_response keys. No
WebSocket is opened, so no ``mocked`` marker is needed.
"""

from __future__ import annotations

from getpatter import RealtimeTurnDetection
from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter


def _adapter(**kwargs) -> OpenAIRealtimeAdapter:
    return OpenAIRealtimeAdapter(api_key="sk-test", **kwargs)


def test_v1_noise_reduction_at_top_level() -> None:
    config = _adapter(noise_reduction="far_field")._build_session_config()
    assert config["input_audio_noise_reduction"] == {"type": "far_field"}
    # NOT nested under audio.input — v1 is a flat shape with no `audio` key.
    assert "audio" not in config


def test_v1_noise_reduction_omitted_when_unset() -> None:
    config = _adapter()._build_session_config()
    assert "input_audio_noise_reduction" not in config


def test_v1_server_vad_threshold_respected() -> None:
    td = RealtimeTurnDetection(type="server_vad", threshold=0.7)
    config = _adapter(turn_detection=td)._build_session_config()
    detection = config["turn_detection"]
    assert detection["type"] == "server_vad"
    assert detection["threshold"] == 0.7
    assert detection["prefix_padding_ms"] == 300
    # v1 turn_detection never carries create/interrupt_response.
    assert "create_response" not in detection
    assert "interrupt_response" not in detection


def test_v1_semantic_vad_omits_threshold() -> None:
    td = RealtimeTurnDetection(type="semantic_vad", eagerness="low")
    config = _adapter(turn_detection=td)._build_session_config()
    detection = config["turn_detection"]
    assert detection["type"] == "semantic_vad"
    assert detection["eagerness"] == "low"
    assert "threshold" not in detection
    assert "prefix_padding_ms" not in detection
    assert "silence_duration_ms" not in detection


def test_v1_defaults_unchanged_when_no_knobs_set() -> None:
    adapter = _adapter()
    config = adapter._build_session_config()
    assert config["turn_detection"] == {
        "type": adapter.vad_type,
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": adapter.silence_duration_ms,
    }

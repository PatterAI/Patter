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
    assert config["audio"]["input"]["noise_reduction"] == {"type": "far_field"}


def test_ga_noise_reduction_omitted_when_unset() -> None:
    config = _adapter()._build_ga_session_config()
    assert "noise_reduction" not in config["audio"]["input"]


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
    # Server-managed default (issue #154): both gating keys True — the server
    # owns response creation AND the barge-in cancel signal.
    assert detection["create_response"] is True
    assert detection["interrupt_response"] is True


def test_ga_semantic_vad_omits_threshold_fields() -> None:
    td = RealtimeTurnDetection(type="semantic_vad", eagerness="low")
    config = _adapter(turn_detection=td)._build_ga_session_config()
    detection = config["audio"]["input"]["turn_detection"]
    assert detection["type"] == "semantic_vad"
    assert detection["eagerness"] == "low"
    assert detection["create_response"] is True
    assert detection["interrupt_response"] is True
    assert "threshold" not in detection
    assert "prefix_padding_ms" not in detection
    assert "silence_duration_ms" not in detection


def test_ga_defaults_unchanged_when_no_knobs_set() -> None:
    # Regression guard for the server-managed default literal.
    adapter = _adapter()
    detection = adapter._build_ga_session_config()["audio"]["input"]["turn_detection"]
    assert detection == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": adapter.silence_duration_ms,
        # Server-managed default (issue #154): the server auto-creates the
        # response on commit AND owns the barge-in cancel (interrupt_response).
        # The WebSocket client still sends conversation.item.truncate + clear
        # on speech_started, but not response.cancel.
        "create_response": True,
        "interrupt_response": True,
    }


def test_ga_opt_out_gate_disables_both_gating_keys() -> None:
    # LEGACY client-managed opt-out: gate_response_on_transcript=True flips
    # BOTH create_response and interrupt_response to False so the stream-handler
    # drives response.create and the full client-side barge-in path.
    adapter = _adapter(gate_response_on_transcript=True)
    detection = adapter._build_ga_session_config()["audio"]["input"]["turn_detection"]
    assert detection["create_response"] is False
    assert detection["interrupt_response"] is False


def test_v1_session_omits_response_gating_keys() -> None:
    # The v1 (non-GA) session shape carries NO create_response /
    # interrupt_response keys on the wire — OpenAI's true-defaults (both True,
    # i.e. server-managed) apply. This holds for both default and opt-out.
    from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

    for gate in (False, True):
        v1 = OpenAIRealtimeAdapter(api_key="sk-test", gate_response_on_transcript=gate)
        detection = v1._build_session_config()["turn_detection"]
        assert "create_response" not in detection
        assert "interrupt_response" not in detection

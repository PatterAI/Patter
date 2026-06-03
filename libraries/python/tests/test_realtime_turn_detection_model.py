"""Unit tests for the RealtimeTurnDetection frozen config + adapter enum guards.

POINT 1b — the new immutable turn-detection tuning object — and the
noise_reduction enum validation on the OpenAI Realtime adapter constructor
(POINT 1a, defense in depth). No external boundary is exercised, so no
``mocked`` marker is needed.
"""

from __future__ import annotations

import dataclasses

import pytest

from getpatter import RealtimeTurnDetection
from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter


def test_realtime_turn_detection_defaults_to_server_vad() -> None:
    td = RealtimeTurnDetection()
    assert td.type == "server_vad"
    assert td.threshold is None
    assert td.prefix_padding_ms is None
    assert td.silence_duration_ms is None
    assert td.eagerness is None


def test_realtime_turn_detection_is_frozen() -> None:
    td = RealtimeTurnDetection(threshold=0.6)
    with pytest.raises(dataclasses.FrozenInstanceError):
        td.threshold = 0.9  # type: ignore[misc]


def test_realtime_turn_detection_rejects_bad_type() -> None:
    with pytest.raises(ValueError, match="server_vad.*semantic_vad"):
        RealtimeTurnDetection(type="bad")


def test_realtime_turn_detection_rejects_bad_eagerness() -> None:
    with pytest.raises(ValueError, match="eagerness"):
        RealtimeTurnDetection(type="semantic_vad", eagerness="turbo")


def test_realtime_turn_detection_eagerness_requires_semantic_vad() -> None:
    with pytest.raises(ValueError, match="semantic_vad"):
        RealtimeTurnDetection(type="server_vad", eagerness="low")


def test_realtime_turn_detection_accepts_valid_semantic_vad() -> None:
    td = RealtimeTurnDetection(type="semantic_vad", eagerness="low")
    assert td.type == "semantic_vad"
    assert td.eagerness == "low"


def test_adapter_rejects_invalid_noise_reduction() -> None:
    with pytest.raises(ValueError, match="near_field.*far_field"):
        OpenAIRealtimeAdapter(api_key="sk-test", noise_reduction="mid_field")


def test_adapter_accepts_valid_noise_reduction() -> None:
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", noise_reduction="far_field")
    assert adapter.noise_reduction == "far_field"


def test_adapter_noise_reduction_defaults_to_none() -> None:
    adapter = OpenAIRealtimeAdapter(api_key="sk-test")
    assert adapter.noise_reduction is None
    assert adapter.turn_detection is None

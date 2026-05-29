"""Regression: pipeline-mode turns after the first emitted no metrics.

`anchor_user_speech_start()` re-opened a turn (set ``_turn_start``) but left the
``_turn_already_closed`` guard set from the previous turn's
``record_turn_complete``, so the next ``record_turn_complete`` short-circuited
to ``None`` — dropping per-turn latency/cost AND the live SSE transcript that
feeds the dashboard. Mirrors libraries/typescript/tests/unit/metrics-turn-rearm.test.ts.
"""

from __future__ import annotations

import pytest

from getpatter.services.metrics import CallMetricsAccumulator


def _make_acc() -> CallMetricsAccumulator:
    return CallMetricsAccumulator(
        call_id="rearm-test",
        provider_mode="pipeline",
        telephony_provider="twilio",
        stt_provider="deepgram",
        tts_provider="elevenlabs",
        llm_provider="custom",
    )


@pytest.mark.unit
def test_turn_recorded_after_vad_anchor_following_prior_turn():
    acc = _make_acc()

    # Turn 0 — opened with start_turn (the firstMessage path); clears the guard.
    acc.start_turn()
    acc.record_stt_complete("hello")
    assert acc.record_turn_complete("hi there") is not None

    # Turn 1 — opened via the real pipeline path: a legitimate VAD speech_start
    # anchors the turn (NOT start_turn). start_turn_if_idle is then a no-op
    # because _turn_start is already set. record_turn_complete must still return
    # a turn — pre-fix it returned None (guard never re-armed).
    acc.anchor_user_speech_start()
    acc.start_turn_if_idle()
    acc.record_stt_complete("how are you")
    assert acc.record_turn_complete("good, thanks") is not None


@pytest.mark.unit
def test_keeps_recording_across_several_anchor_opened_turns():
    acc = _make_acc()
    acc.start_turn()
    acc.record_stt_complete("q0")
    acc.record_turn_complete("a0")

    for i in range(1, 4):
        acc.anchor_user_speech_start()
        acc.start_turn_if_idle()
        acc.record_stt_complete(f"q{i}")
        assert acc.record_turn_complete(f"a{i}") is not None

    # turn 0 + 3 anchor-opened turns all recorded.
    assert len(acc.end_call().turns) == 4

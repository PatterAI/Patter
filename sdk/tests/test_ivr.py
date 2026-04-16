"""Tests for ``patter.services.ivr``.

Covers:
- ``IVRActivity.tools`` builds a valid OpenAI-style function tool that
  forwards to ``CallControl.send_dtmf``. (MOCK: no real call.)
- ``IVRActivity.on_user_transcribed`` triggers the loop-detected
  callback once the TF-IDF detector fires.
- ``TfidfLoopDetector`` fires on 3 consecutive duplicate prompts
  (authentic: real sklearn path, synthetic but realistic data).
- Silence debounce actually waits — uses real ``asyncio.sleep`` with
  a short ``max_silence_duration`` (1s in tests).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from patter.services.ivr import (
    DtmfEvent,
    IVRActivity,
    TfidfLoopDetector,
    format_dtmf,
)


# ── fixtures ───────────────────────────────────────────────────────────

def _make_call_control() -> AsyncMock:
    """MOCK CallControl: no real call; records send_dtmf calls."""
    cc = AsyncMock()
    cc.call_id = "test-call-id"
    cc.caller = "+15551234567"
    cc.callee = "+15559876543"
    cc.telephony_provider = "test"
    cc.send_dtmf = AsyncMock()
    return cc


# ── DtmfEvent ──────────────────────────────────────────────────────────

def test_dtmf_event_values_cover_keypad() -> None:
    expected = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
                "*", "#", "A", "B", "C", "D"}
    assert {e.value for e in DtmfEvent} == expected


def test_format_dtmf_joins_with_spaces() -> None:
    events = [DtmfEvent.ONE, DtmfEvent.TWO, DtmfEvent.POUND]
    assert format_dtmf(events) == "1 2 #"


# ── IVRActivity.tools ──────────────────────────────────────────────────

async def test_tools_contains_send_dtmf_spec() -> None:
    cc = _make_call_control()
    ivr = IVRActivity(cc, loop_detector=False)

    tools = ivr.tools
    assert len(tools) == 1

    tool = tools[0]
    assert tool["name"] == "send_dtmf_events"
    assert "description" in tool and tool["description"]
    assert tool["parameters"]["type"] == "object"
    assert "events" in tool["parameters"]["properties"]
    assert tool["parameters"]["required"] == ["events"]

    # Allowed values include the whole keypad.
    enum_vals = tool["parameters"]["properties"]["events"]["items"]["enum"]
    assert set(enum_vals) == {e.value for e in DtmfEvent}


async def test_tool_handler_forwards_to_send_dtmf() -> None:
    """MOCK: no real call; verifies CallControl is driven correctly."""
    cc = _make_call_control()
    ivr = IVRActivity(cc, loop_detector=False)

    handler = ivr.tools[0]["handler"]
    result = await handler(["1", "2", "3", "#"])

    cc.send_dtmf.assert_awaited_once_with("123#", delay_ms=300)
    assert "Successfully" in result


async def test_tool_handler_rejects_invalid_digit() -> None:
    cc = _make_call_control()
    ivr = IVRActivity(cc, loop_detector=False)

    handler = ivr.tools[0]["handler"]
    result = await handler(["1", "Z"])

    cc.send_dtmf.assert_not_awaited()
    assert "invalid" in result.lower()


async def test_tool_handler_surfaces_call_control_errors() -> None:
    cc = _make_call_control()
    cc.send_dtmf.side_effect = RuntimeError("carrier rejected")
    ivr = IVRActivity(cc, loop_detector=False)

    handler = ivr.tools[0]["handler"]
    result = await handler(["1"])

    assert "Failed" in result
    assert "carrier rejected" in result


# ── TfidfLoopDetector (authentic sklearn path) ─────────────────────────

def test_tfidf_detector_fires_on_three_consecutive_duplicates() -> None:
    """Authentic: 5 identical IVR prompts → loop detected on 3rd duplicate."""
    try:
        detector = TfidfLoopDetector()
    except ImportError:
        pytest.skip("scikit-learn not installed")

    prompt = "Press 1 for sales, 2 for support, or 3 for billing"

    # 1st chunk: no prior chunks -> cannot be a duplicate.
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is False

    # 2nd chunk: 1 duplicate.
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is False

    # 3rd chunk: 2 consecutive duplicates (below the default threshold of 3).
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is False

    # 4th chunk: 3 consecutive duplicates → fires.
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is True


def test_tfidf_detector_resets_counter_on_different_prompt() -> None:
    try:
        detector = TfidfLoopDetector()
    except ImportError:
        pytest.skip("scikit-learn not installed")

    detector.add_chunk("Please enter your account number followed by pound")
    assert detector.check_loop_detection() is False

    detector.add_chunk("Please enter your account number followed by pound")
    assert detector.check_loop_detection() is False

    # Break the loop with a completely unrelated chunk.
    detector.add_chunk(
        "We are experiencing longer than usual wait times, hold music follows"
    )
    assert detector.check_loop_detection() is False

    # Back to the original prompt — counter should have reset.
    detector.add_chunk("Please enter your account number followed by pound")
    assert detector.check_loop_detection() is False


def test_tfidf_detector_reset_clears_state() -> None:
    try:
        detector = TfidfLoopDetector(consecutive_threshold=2)
    except ImportError:
        pytest.skip("scikit-learn not installed")

    prompt = "For new customers press 1"
    # Each add-then-check increments the internal counter when similar;
    # with threshold=2 it fires on the 3rd add (2 consecutive duplicates).
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is False
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is False
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is True

    detector.reset()
    # After reset, even the original repeated prompt shouldn't fire until
    # enough new chunks accumulate.
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is False
    detector.add_chunk(prompt)
    assert detector.check_loop_detection() is False


def test_tfidf_detector_validates_args() -> None:
    try:
        with pytest.raises(ValueError):
            TfidfLoopDetector(window_size=0)
        with pytest.raises(ValueError):
            TfidfLoopDetector(similarity_threshold=1.5)
        with pytest.raises(ValueError):
            TfidfLoopDetector(consecutive_threshold=0)
    except ImportError:
        pytest.skip("scikit-learn not installed")


# ── IVRActivity transcript path ───────────────────────────────────────

async def test_on_user_transcribed_triggers_loop_callback() -> None:
    """Loop callback fires once the detector trips."""
    try:
        TfidfLoopDetector()  # probe sklearn availability
    except ImportError:
        pytest.skip("scikit-learn not installed")

    cc = _make_call_control()
    calls: list[int] = []

    async def on_loop() -> None:
        calls.append(1)

    ivr = IVRActivity(cc, loop_detector=True, on_loop_detected=on_loop)
    await ivr.start()

    prompt = "Press 1 for sales, 2 for support, or 3 for billing"
    for _ in range(4):
        await ivr.on_user_transcribed(prompt)

    assert len(calls) == 1
    await ivr.stop()


async def test_on_user_transcribed_ignored_before_start() -> None:
    cc = _make_call_control()
    called: list[int] = []

    async def on_loop() -> None:
        called.append(1)

    ivr = IVRActivity(cc, loop_detector=True, on_loop_detected=on_loop)
    # Do NOT call start()

    for _ in range(5):
        await ivr.on_user_transcribed("Press 1 for sales")

    assert called == []


async def test_on_user_transcribed_empty_text_is_noop() -> None:
    cc = _make_call_control()
    ivr = IVRActivity(cc, loop_detector=False)
    await ivr.start()
    await ivr.on_user_transcribed("")  # should not raise
    await ivr.stop()


# ── silence debounce (authentic timing, short delays) ─────────────────

async def test_silence_callback_fires_after_max_silence_duration() -> None:
    """Real asyncio.sleep with a 1s debounce — no mocking of time."""
    cc = _make_call_control()
    fired = asyncio.Event()

    async def on_silence() -> None:
        fired.set()

    ivr = IVRActivity(
        cc,
        max_silence_duration=0.1,
        loop_detector=False,
        on_silence=on_silence,
    )
    await ivr.start()

    ivr.note_user_state("listening")
    ivr.note_agent_state("idle")

    try:
        await asyncio.wait_for(fired.wait(), timeout=1.0)
    finally:
        await ivr.stop()


async def test_silence_callback_cancelled_when_user_speaks() -> None:
    cc = _make_call_control()
    fired: list[int] = []

    async def on_silence() -> None:
        fired.append(1)

    ivr = IVRActivity(
        cc,
        max_silence_duration=0.2,
        loop_detector=False,
        on_silence=on_silence,
    )
    await ivr.start()

    # Both sides silent — debounce scheduled.
    ivr.note_user_state("listening")
    ivr.note_agent_state("idle")

    # User starts speaking before the debounce fires.
    await asyncio.sleep(0.05)
    ivr.note_user_state("speaking")

    # Wait past the original debounce deadline.
    await asyncio.sleep(0.3)
    assert fired == []

    await ivr.stop()


async def test_silence_debounce_not_rescheduled_while_pending() -> None:
    """A second 'silent' update should not reset the timer."""
    cc = _make_call_control()
    fire_times: list[float] = []

    async def on_silence() -> None:
        loop = asyncio.get_running_loop()
        fire_times.append(loop.time())

    ivr = IVRActivity(
        cc,
        max_silence_duration=0.15,
        loop_detector=False,
        on_silence=on_silence,
    )
    await ivr.start()

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    ivr.note_user_state("listening")
    ivr.note_agent_state("idle")

    # Same-state nudges mid-flight — debounce should NOT restart.
    await asyncio.sleep(0.08)
    ivr.note_agent_state("idle")
    await asyncio.sleep(0.15)

    assert len(fire_times) == 1
    # Original deadline was ~0.15s from t0.
    elapsed = fire_times[0] - t0
    assert elapsed < 0.25, f"debounce fired late ({elapsed:.3f}s after start)"

    await ivr.stop()


async def test_stop_cancels_pending_silence_timer() -> None:
    cc = _make_call_control()
    fired: list[int] = []

    async def on_silence() -> None:
        fired.append(1)

    ivr = IVRActivity(
        cc,
        max_silence_duration=0.15,
        loop_detector=False,
        on_silence=on_silence,
    )
    await ivr.start()
    ivr.note_user_state("listening")
    ivr.note_agent_state("idle")
    await ivr.stop()

    # Wait past the would-be deadline.
    await asyncio.sleep(0.25)
    assert fired == []


# ── loop_detector=False bypasses sklearn requirement ──────────────────

async def test_loop_detector_disabled_skips_sklearn() -> None:
    """loop_detector=False must not touch sklearn or raise ImportError."""
    cc = _make_call_control()
    ivr = IVRActivity(cc, loop_detector=False)
    await ivr.start()
    await ivr.on_user_transcribed("anything")
    await ivr.stop()

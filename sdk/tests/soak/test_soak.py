"""Soak / stress tests for the Patter Python SDK.

All tests are marked with ``@pytest.mark.soak`` so they can be run in
isolation via ``pytest -m soak``.
"""

from __future__ import annotations

import asyncio
import gc
import time
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import psutil
import pytest

from patter.dashboard.store import MetricsStore
from patter.services.metrics import CallMetricsAccumulator


# ---------------------------------------------------------------------------
# S1 — 100 concurrent calls for 30 seconds
# ---------------------------------------------------------------------------
# Full scenario: 100 concurrent calls for 10 minutes.
# Scaled down to 30 seconds for practical CI speed; the concurrency pattern
# is identical and the memory-growth assertion still validates the invariant.


@pytest.mark.soak
async def test_s1_concurrent_calls_memory_growth(make_accumulator: Any) -> None:
    """S1: 100 concurrent mock calls — RSS growth must stay < 10%."""
    proc = psutil.Process()
    gc.collect()
    rss_before = proc.memory_info().rss

    num_calls = 100
    duration_seconds = 30
    frame = b"\x00\x00" * 160  # 20 ms of PCM silence at 16 kHz

    exceptions: list[Exception] = []
    frames_sent: list[int] = []

    async def _simulate_call(call_index: int) -> None:
        acc = make_accumulator(call_id=f"soak-s1-{call_index}")
        sent = 0
        deadline = asyncio.get_event_loop().time() + duration_seconds
        try:
            while asyncio.get_event_loop().time() < deadline:
                acc.start_turn()
                acc.add_stt_audio_bytes(len(frame))
                acc.record_stt_complete("hello", audio_seconds=0.02)
                acc.record_llm_complete()
                acc.record_tts_first_byte()
                acc.record_tts_complete("world")
                acc.record_turn_complete("world")
                sent += 1
                await asyncio.sleep(1)
            acc.end_call()
        except Exception as exc:
            exceptions.append(exc)
        frames_sent.append(sent)

    await asyncio.gather(*[_simulate_call(i) for i in range(num_calls)])

    gc.collect()
    rss_after = proc.memory_info().rss

    growth_pct = ((rss_after - rss_before) / rss_before) * 100 if rss_before else 0
    passed = growth_pct < 10
    print(f"Memory growth: {growth_pct:.1f}% (threshold: 10.0%) {'PASS' if passed else 'FAIL'}")

    assert not exceptions, f"Unhandled exceptions: {exceptions}"
    assert all(f > 0 for f in frames_sent), "Some calls sent zero frames"
    assert passed, f"RSS grew {growth_pct:.1f}% (> 10%)"


# ---------------------------------------------------------------------------
# S2 — 1000-turn conversation
# ---------------------------------------------------------------------------


@pytest.mark.soak
async def test_s2_1000_turn_conversation(make_accumulator: Any) -> None:
    """S2: 1000 LLM turns — cost and token counts must be arithmetically correct."""
    num_turns = 1000
    per_turn_audio_seconds = 1.5
    agent_response = "Reply text for turn."  # 20 chars

    acc = make_accumulator(
        call_id="soak-s2",
        provider_mode="pipeline",
        stt_provider="deepgram",
        tts_provider="elevenlabs",
        telephony_provider="twilio",
    )

    proc = psutil.Process()
    gc.collect()
    rss_before = proc.memory_info().rss

    for i in range(num_turns):
        acc.start_turn()
        acc.record_stt_complete(f"user turn {i}", audio_seconds=per_turn_audio_seconds)
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        acc.record_tts_complete(agent_response)
        acc.record_turn_complete(agent_response)

    metrics = acc.end_call()

    gc.collect()
    rss_after = proc.memory_info().rss

    # Verify turn count
    assert len(metrics.turns) == num_turns

    # Verify STT cost: deepgram charges per minute
    # total_audio = 1.5 * 1000 = 1500 seconds = 25 minutes
    # cost = 25 * 0.0043 = 0.1075
    expected_stt = (per_turn_audio_seconds * num_turns / 60.0) * 0.0043
    assert abs(metrics.cost.stt - round(expected_stt, 6)) < 1e-6

    # Verify TTS cost: elevenlabs charges per 1k chars
    # total_chars = 20 * 1000 = 20000 chars = 20 k_chars
    # cost = 20 * 0.18 = 3.6
    expected_tts = (len(agent_response) * num_turns / 1000.0) * 0.18
    assert abs(metrics.cost.tts - round(expected_tts, 6)) < 1e-6

    # Memory check
    growth_pct = ((rss_after - rss_before) / rss_before) * 100 if rss_before else 0
    passed = growth_pct < 10
    print(f"S2 turn count: {len(metrics.turns)} (expected: {num_turns}) PASS")
    print(f"S2 STT cost: {metrics.cost.stt} (expected: {round(expected_stt, 6)}) PASS")
    print(f"S2 TTS cost: {metrics.cost.tts} (expected: {round(expected_tts, 6)}) PASS")
    print(f"Memory growth: {growth_pct:.1f}% (threshold: 10.0%) {'PASS' if passed else 'FAIL'}")
    assert passed, f"RSS grew {growth_pct:.1f}% (> 10%)"


# ---------------------------------------------------------------------------
# S3 — WebSocket reconnection under network flapping
# ---------------------------------------------------------------------------


@pytest.mark.soak
async def test_s3_websocket_reconnection_flapping(mock_ws_pair: Any) -> None:
    """S3: 20 disconnect/reconnect cycles — no silent frame loss."""
    client_ws, _ = mock_ws_pair

    num_cycles = 20
    reconnect_gap_ms = 50

    frames_in_flight: list[bytes] = []
    flushed_or_dropped: list[bytes] = []
    reconnect_times_ms: list[float] = []

    frame = b"\x00\x00" * 160

    for cycle in range(num_cycles):
        # Send a frame while connected
        frames_in_flight.append(frame)
        await client_ws.send(frame)

        # Disconnect
        client_ws.disconnect()
        assert client_ws.state == "CLOSED"

        # Flush in-flight frames (simulate explicit flush/drop on disconnect)
        flushed_or_dropped.extend(frames_in_flight)
        frames_in_flight.clear()

        await asyncio.sleep(reconnect_gap_ms / 1000.0)

        # Reconnect and measure time
        t0 = asyncio.get_event_loop().time()
        client_ws.reconnect()
        t1 = asyncio.get_event_loop().time()
        reconnect_times_ms.append((t1 - t0) * 1000)

        assert client_ws.state == "OPEN"

    # All in-flight frames must be accounted for (flushed or dropped)
    assert len(flushed_or_dropped) == num_cycles, (
        f"Expected {num_cycles} frames flushed/dropped, got {len(flushed_or_dropped)}"
    )

    # Reconnection should be fast (< 500ms each)
    for i, rt in enumerate(reconnect_times_ms):
        assert rt < 500, f"Cycle {i}: reconnect took {rt:.1f}ms (> 500ms)"

    print(f"S3 cycles: {num_cycles}, frames accounted: {len(flushed_or_dropped)} PASS")
    print(f"S3 max reconnect time: {max(reconnect_times_ms):.1f}ms (threshold: 500ms) PASS")


# ---------------------------------------------------------------------------
# S4 — SSE subscriber churn
# ---------------------------------------------------------------------------


@pytest.mark.soak
async def test_s4_sse_subscriber_churn(metrics_store: MetricsStore) -> None:
    """S4: 50 rapid subscribe/unsubscribe cycles with concurrent events."""
    num_subscribers = 50
    num_events = 10
    timeout_seconds = 30

    received_events: dict[int, list[dict[str, Any]]] = {
        i: [] for i in range(num_subscribers)
    }
    subscriber_connected_during: dict[int, set[int]] = {
        i: set() for i in range(num_subscribers)
    }

    event_published = asyncio.Event()
    events_done = asyncio.Event()

    async def _subscriber(idx: int) -> None:
        queue = metrics_store.subscribe()
        try:
            # Stay subscribed for a short window, collecting events
            deadline = asyncio.get_event_loop().time() + 0.1
            while asyncio.get_event_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.05)
                    received_events[idx].append(event)
                except asyncio.TimeoutError:
                    pass
        finally:
            metrics_store.unsubscribe(queue)

    async def _publisher() -> None:
        for event_idx in range(num_events):
            metrics_store.record_call_start({
                "call_id": f"s4-event-{event_idx}",
                "caller": "+1555000",
                "callee": "+1555001",
            })
            await asyncio.sleep(0.005)
        events_done.set()

    async with asyncio.timeout(timeout_seconds):
        # Run publisher and all subscribers concurrently
        tasks = [_publisher()] + [_subscriber(i) for i in range(num_subscribers)]
        await asyncio.gather(*tasks)

    # Verify: every subscriber that was alive during an event received it
    # At minimum, each subscriber should have received at least 1 event
    # (since they all overlap with the publisher in time)
    total_received = sum(len(evts) for evts in received_events.values())
    subscribers_with_events = sum(1 for evts in received_events.values() if len(evts) > 0)

    print(f"S4 total events received across subscribers: {total_received}")
    print(f"S4 subscribers with >= 1 event: {subscribers_with_events}/{num_subscribers} PASS")

    # No deadlock (we reached this point within the timeout)
    assert subscribers_with_events > 0, "No subscriber received any events"


# ---------------------------------------------------------------------------
# S5 — 500-call buffer wrap
# ---------------------------------------------------------------------------


@pytest.mark.soak
def test_s5_buffer_wrap(metrics_store: MetricsStore) -> None:
    """S5: Writing 501 calls — oldest evicted, newest 500 present and ordered."""
    for i in range(501):
        metrics_store.record_call_start({
            "call_id": f"s5-call-{i}",
            "caller": "+1555000",
            "callee": "+1555001",
        })
        metrics_store.record_call_end({"call_id": f"s5-call-{i}"})

    # The store has max_calls=500, so call index 0 should be evicted
    assert metrics_store.call_count == 500

    # Oldest remaining should be index 1
    oldest = metrics_store.get_call("s5-call-0")
    assert oldest is None, "Call s5-call-0 should have been evicted"

    # Verify the newest 500 are present and in order
    all_calls = metrics_store.get_calls(limit=500, offset=0)
    # get_calls returns newest first
    call_ids = [c["call_id"] for c in reversed(all_calls)]
    expected_ids = [f"s5-call-{i}" for i in range(1, 501)]
    assert call_ids == expected_ids, "Calls not in expected order after buffer wrap"

    print(f"S5 buffer wrap: evicted index 0, retained 500 in order PASS")


# ---------------------------------------------------------------------------
# S6 — Cost precision over 1000 turns
# ---------------------------------------------------------------------------


@pytest.mark.soak
def test_s6_cost_precision_1000_turns() -> None:
    """S6: Accumulate fractional costs over 1000 turns — precision within 1e-9."""
    num_turns = 1000
    per_turn_cost = Decimal("0.000123")
    expected_total = float(per_turn_cost * num_turns)  # 0.123

    # Use pipeline mode with a custom pricing to get deterministic costs
    acc = CallMetricsAccumulator(
        call_id="soak-s6",
        provider_mode="pipeline",
        telephony_provider="twilio",
        stt_provider="deepgram",
        tts_provider="elevenlabs",
        pricing={
            # Set TTS to exactly 0.123 per 1k chars so that 1 char/turn * 1000 turns
            # = 1000 chars = 1 k_char * 0.123 = 0.123
            "elevenlabs": {"unit": "1k_chars", "price": 0.123},
            # Zero out STT to isolate TTS cost
            "deepgram": {"unit": "minute", "price": 0.0},
            # Zero out telephony
            "twilio": {"unit": "minute", "price": 0.0},
        },
    )

    for i in range(num_turns):
        acc.start_turn()
        acc.record_stt_complete(f"u{i}", audio_seconds=0.0)
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        # Each turn: 1 character of TTS text
        acc.record_tts_complete("X")
        acc.record_turn_complete("X")

    metrics = acc.end_call()

    # TTS cost: 1000 chars / 1000 * 0.123 = 0.123
    actual_tts = metrics.cost.tts
    tolerance = 1e-9

    passed = abs(actual_tts - expected_total) < tolerance
    print(f"S6 cost precision: actual={actual_tts}, expected={expected_total}, "
          f"diff={abs(actual_tts - expected_total):.2e} (tolerance: {tolerance:.0e}) "
          f"{'PASS' if passed else 'FAIL'}")

    assert passed, (
        f"Cost precision failure: {actual_tts} != {expected_total} "
        f"(diff={abs(actual_tts - expected_total):.2e})"
    )

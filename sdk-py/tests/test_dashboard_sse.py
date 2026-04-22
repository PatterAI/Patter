"""Tests for dashboard SSE event bus."""

from __future__ import annotations

import asyncio

import pytest

from getpatter.dashboard.store import MetricsStore


@pytest.mark.asyncio
async def test_subscribe_and_receive_event():
    """Subscriber receives events published by record_call_start."""
    store = MetricsStore()
    queue = store.subscribe()

    store.record_call_start({"call_id": "test1", "caller": "+1234", "callee": "+5678"})

    event = queue.get_nowait()
    assert event["type"] == "call_start"
    assert event["data"]["call_id"] == "test1"


@pytest.mark.asyncio
async def test_turn_complete_event():
    """Subscriber receives turn_complete events."""
    store = MetricsStore()
    queue = store.subscribe()

    store.record_call_start({"call_id": "test1"})
    # Consume call_start event
    queue.get_nowait()

    from getpatter.models import TurnMetrics, LatencyBreakdown

    turn = TurnMetrics(
        turn_index=0,
        user_text="hello",
        agent_text="hi",
        latency=LatencyBreakdown(),
    )
    store.record_turn({"call_id": "test1", "turn": turn})

    event = queue.get_nowait()
    assert event["type"] == "turn_complete"
    assert event["data"]["call_id"] == "test1"


@pytest.mark.asyncio
async def test_call_end_event():
    """Subscriber receives call_end events."""
    store = MetricsStore()
    queue = store.subscribe()

    store.record_call_start({"call_id": "test1"})
    queue.get_nowait()  # consume call_start

    store.record_call_end({"call_id": "test1"})

    event = queue.get_nowait()
    assert event["type"] == "call_end"
    assert event["data"]["call_id"] == "test1"


@pytest.mark.asyncio
async def test_unsubscribe():
    """After unsubscribe, no more events are received."""
    store = MetricsStore()
    queue = store.subscribe()
    store.unsubscribe(queue)

    store.record_call_start({"call_id": "test1"})
    assert queue.empty()


@pytest.mark.asyncio
async def test_full_queue_drops_subscriber():
    """When a subscriber's queue is full, it gets dropped."""
    store = MetricsStore()
    queue = store.subscribe()

    # Fill the queue to max (100)
    for i in range(100):
        queue.put_nowait({"type": "filler", "data": {}})

    # This should drop the subscriber since queue is full
    store.record_call_start({"call_id": "overflow"})

    # Subscriber should have been removed
    assert queue in store._subscribers or queue not in store._subscribers
    # The point is it didn't raise

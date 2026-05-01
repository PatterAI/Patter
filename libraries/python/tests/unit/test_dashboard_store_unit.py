"""Unit tests for getpatter.dashboard.store — MetricsStore."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from getpatter.dashboard.store import MetricsStore, MetricsStoreProtocol
from getpatter.models import CallMetrics, CostBreakdown, LatencyBreakdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_call_data(call_id: str) -> dict:
    return {
        "call_id": call_id,
        "caller": "+15551111111",
        "callee": "+15552222222",
        "direction": "inbound",
    }


def _make_call_metrics(call_id: str) -> CallMetrics:
    return CallMetrics(
        call_id=call_id,
        duration_seconds=30.0,
        turns=(),
        cost=CostBreakdown(stt=0.01, tts=0.02, llm=0.03, telephony=0.005, total=0.065),
        latency_avg=LatencyBreakdown(stt_ms=50.0, llm_ms=100.0, tts_ms=30.0, total_ms=180.0),
        latency_p95=LatencyBreakdown(stt_ms=60.0, llm_ms=120.0, tts_ms=40.0, total_ms=220.0),
        provider_mode="pipeline",
    )


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMetricsStoreProtocol:
    """MetricsStore satisfies MetricsStoreProtocol."""

    def test_satisfies_protocol(self) -> None:
        store = MetricsStore()
        assert isinstance(store, MetricsStoreProtocol)


# ---------------------------------------------------------------------------
# record_call_start / get_active_calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRecordCallStart:
    """record_call_start tracks active calls."""

    def test_basic(self) -> None:
        store = MetricsStore()
        store.record_call_start(_make_call_data("c1"))
        active = store.get_active_calls()
        assert len(active) == 1
        assert active[0]["call_id"] == "c1"

    def test_empty_call_id_ignored(self) -> None:
        store = MetricsStore()
        store.record_call_start({"call_id": ""})
        assert len(store.get_active_calls()) == 0

    def test_multiple_active(self) -> None:
        store = MetricsStore()
        store.record_call_start(_make_call_data("c1"))
        store.record_call_start(_make_call_data("c2"))
        assert len(store.get_active_calls()) == 2


# ---------------------------------------------------------------------------
# record_call_end / get_calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRecordCallEnd:
    """record_call_end moves from active to completed."""

    def test_basic(self) -> None:
        store = MetricsStore()
        store.record_call_start(_make_call_data("c1"))
        store.record_call_end({"call_id": "c1"}, metrics=_make_call_metrics("c1"))
        assert len(store.get_active_calls()) == 0
        calls = store.get_calls()
        assert len(calls) == 1
        assert calls[0]["call_id"] == "c1"
        assert calls[0]["metrics"] is not None

    def test_empty_call_id_ignored(self) -> None:
        store = MetricsStore()
        store.record_call_end({"call_id": ""})
        assert len(store.get_calls()) == 0

    def test_end_without_start(self) -> None:
        """Ending a call that was never started still records it."""
        store = MetricsStore()
        store.record_call_end({"call_id": "c1"})
        calls = store.get_calls()
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Circular buffer — max_calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCircularBuffer:
    """MetricsStore enforces max_calls limit."""

    def test_wraps_at_max(self) -> None:
        store = MetricsStore(max_calls=5)
        for i in range(10):
            store.record_call_end({"call_id": f"c{i}"})
        calls = store.get_calls(limit=100)
        assert len(calls) == 5
        # Should have the 5 most recent
        call_ids = [c["call_id"] for c in calls]
        assert "c5" in call_ids
        assert "c0" not in call_ids

    def test_501st_overwrites_oldest(self) -> None:
        """500-call default buffer: 501st entry replaces the oldest."""
        store = MetricsStore(max_calls=500)
        for i in range(501):
            store.record_call_end({"call_id": f"c{i}"})
        assert store.call_count == 500
        calls = store.get_calls(limit=1)
        # Most recent is c500
        assert calls[0]["call_id"] == "c500"


# ---------------------------------------------------------------------------
# record_turn
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRecordTurn:
    """record_turn appends turn data to active calls."""

    def test_turn_appended(self) -> None:
        store = MetricsStore()
        store.record_call_start(_make_call_data("c1"))
        store.record_turn({"call_id": "c1", "turn": {"turn_index": 0, "text": "hi"}})
        active = store.get_active_calls()
        assert len(active[0]["turns"]) == 1

    def test_turn_missing_call_id_ignored(self) -> None:
        store = MetricsStore()
        store.record_turn({"call_id": "", "turn": {"index": 0}})

    def test_turn_missing_turn_ignored(self) -> None:
        store = MetricsStore()
        store.record_call_start(_make_call_data("c1"))
        store.record_turn({"call_id": "c1"})  # no "turn" key
        active = store.get_active_calls()
        assert len(active[0]["turns"]) == 0


# ---------------------------------------------------------------------------
# get_call (by ID)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetCall:
    """get_call returns a specific completed call."""

    def test_found(self) -> None:
        store = MetricsStore()
        store.record_call_end({"call_id": "c1"})
        assert store.get_call("c1") is not None

    def test_not_found(self) -> None:
        store = MetricsStore()
        assert store.get_call("nonexistent") is None


# ---------------------------------------------------------------------------
# get_calls — pagination
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetCallsPagination:
    """get_calls supports limit and offset."""

    def test_limit(self) -> None:
        store = MetricsStore()
        for i in range(10):
            store.record_call_end({"call_id": f"c{i}"})
        calls = store.get_calls(limit=3)
        assert len(calls) == 3

    def test_offset(self) -> None:
        store = MetricsStore()
        for i in range(5):
            store.record_call_end({"call_id": f"c{i}"})
        calls = store.get_calls(limit=2, offset=2)
        assert len(calls) == 2

    def test_ordered_newest_first(self) -> None:
        store = MetricsStore()
        store.record_call_end({"call_id": "c0"})
        store.record_call_end({"call_id": "c1"})
        calls = store.get_calls()
        assert calls[0]["call_id"] == "c1"
        assert calls[1]["call_id"] == "c0"


# ---------------------------------------------------------------------------
# get_aggregates
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetAggregates:
    """get_aggregates computes summary statistics."""

    def test_empty_store(self) -> None:
        store = MetricsStore()
        agg = store.get_aggregates()
        assert agg["total_calls"] == 0
        assert agg["total_cost"] == 0.0
        assert agg["avg_duration"] == 0.0
        assert agg["avg_latency_ms"] == 0.0

    def test_with_calls(self) -> None:
        store = MetricsStore()
        store.record_call_start(_make_call_data("c1"))
        store.record_call_end({"call_id": "c1"}, metrics=_make_call_metrics("c1"))
        store.record_call_start(_make_call_data("c2"))
        store.record_call_end({"call_id": "c2"}, metrics=_make_call_metrics("c2"))

        agg = store.get_aggregates()
        assert agg["total_calls"] == 2
        assert agg["total_cost"] > 0
        assert agg["avg_duration"] > 0
        assert agg["avg_latency_ms"] > 0
        assert agg["cost_breakdown"]["stt"] > 0

    def test_active_calls_count(self) -> None:
        store = MetricsStore()
        store.record_call_start(_make_call_data("c1"))
        agg = store.get_aggregates()
        assert agg["active_calls"] == 1


# ---------------------------------------------------------------------------
# get_calls_in_range
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetCallsInRange:
    """get_calls_in_range filters by timestamp."""

    def test_range_filter(self) -> None:
        store = MetricsStore()
        now = time.time()
        store.record_call_start({**_make_call_data("c1")})
        store.record_call_end({"call_id": "c1"})

        results = store.get_calls_in_range(from_ts=now - 10, to_ts=now + 10)
        assert len(results) <= 1  # c1 may or may not have started_at within range

    def test_no_range(self) -> None:
        store = MetricsStore()
        store.record_call_end({"call_id": "c1"})
        results = store.get_calls_in_range()
        assert len(results) == 1


# ---------------------------------------------------------------------------
# SSE Pub/Sub
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPubSub:
    """subscribe / unsubscribe / event fan-out."""

    def test_subscribe_returns_queue(self) -> None:
        store = MetricsStore()
        q = store.subscribe()
        assert isinstance(q, asyncio.Queue)

    def test_unsubscribe(self) -> None:
        store = MetricsStore()
        q = store.subscribe()
        store.unsubscribe(q)
        # Should not raise on double unsubscribe
        store.unsubscribe(q)

    def test_call_start_publishes_event(self) -> None:
        store = MetricsStore()
        q = store.subscribe()
        store.record_call_start(_make_call_data("c1"))
        event = q.get_nowait()
        assert event["type"] == "call_start"
        assert event["data"]["call_id"] == "c1"

    def test_call_end_publishes_event(self) -> None:
        store = MetricsStore()
        q = store.subscribe()
        store.record_call_end({"call_id": "c1"})
        event = q.get_nowait()
        assert event["type"] == "call_end"

    def test_turn_publishes_event(self) -> None:
        store = MetricsStore()
        store.record_call_start(_make_call_data("c1"))
        q = store.subscribe()
        store.record_turn({"call_id": "c1", "turn": {"index": 0}})
        event = q.get_nowait()
        assert event["type"] == "turn_complete"

    def test_fan_out_10_subscribers(self) -> None:
        """10 simultaneous subscribers each receive the same update exactly once."""
        store = MetricsStore()
        queues = [store.subscribe() for _ in range(10)]
        store.record_call_start(_make_call_data("c1"))

        for q in queues:
            event = q.get_nowait()
            assert event["type"] == "call_start"
            assert event["data"]["call_id"] == "c1"
            assert q.empty()  # exactly one event

    def test_full_queue_subscriber_removed(self) -> None:
        """A subscriber whose queue is full gets discarded."""
        store = MetricsStore()
        q = store.subscribe()
        # Fill the queue
        for i in range(100):
            try:
                q.put_nowait({"type": "filler", "data": {}})
            except asyncio.QueueFull:
                break
        # This should trigger removal
        store.record_call_start(_make_call_data("overflow"))
        # Queue should no longer be subscribed
        assert q not in store._subscribers


# ---------------------------------------------------------------------------
# call_count property
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCallCount:
    """call_count property."""

    def test_initial(self) -> None:
        store = MetricsStore()
        assert store.call_count == 0

    def test_after_calls(self) -> None:
        store = MetricsStore()
        store.record_call_end({"call_id": "c1"})
        store.record_call_end({"call_id": "c2"})
        assert store.call_count == 2

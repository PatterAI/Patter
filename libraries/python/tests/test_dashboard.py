"""Tests for the local dashboard store, routes, and integration."""

import pytest

from getpatter.dashboard.store import MetricsStore
from getpatter.models import CallMetrics, CostBreakdown, LatencyBreakdown, TurnMetrics

_has_fastapi = False
try:
    import fastapi  # noqa: F401

    _has_fastapi = True
except ImportError:
    pass


def _make_metrics(
    call_id="call-1", duration=30.0, cost_total=0.05, latency_avg_ms=200.0
):
    return CallMetrics(
        call_id=call_id,
        duration_seconds=duration,
        turns=(
            TurnMetrics(
                turn_index=0,
                user_text="hello",
                agent_text="hi there",
                latency=LatencyBreakdown(
                    stt_ms=50, llm_ms=100, tts_ms=50, total_ms=200
                ),
            ),
        ),
        cost=CostBreakdown(
            stt=0.01, tts=0.01, llm=0.02, telephony=0.01, total=cost_total
        ),
        latency_avg=LatencyBreakdown(
            stt_ms=50, llm_ms=100, tts_ms=50, total_ms=latency_avg_ms
        ),
        latency_p95=LatencyBreakdown(stt_ms=60, llm_ms=120, tts_ms=55, total_ms=235),
        provider_mode="pipeline",
        stt_provider="deepgram",
        tts_provider="elevenlabs",
        llm_provider="custom",
        telephony_provider="twilio",
    )


class TestMetricsStore:
    def test_empty_store(self):
        store = MetricsStore()
        assert store.call_count == 0
        assert store.get_calls() == []
        assert store.get_active_calls() == []

    def test_record_call_start(self):
        store = MetricsStore()
        store.record_call_start(
            {
                "call_id": "c1",
                "caller": "+1234",
                "callee": "+5678",
                "direction": "inbound",
            }
        )
        active = store.get_active_calls()
        assert len(active) == 1
        assert active[0]["call_id"] == "c1"
        assert active[0]["caller"] == "+1234"

    def test_record_call_end(self):
        store = MetricsStore()
        store.record_call_start({"call_id": "c1", "caller": "+1", "callee": "+2"})
        metrics = _make_metrics("c1")
        store.record_call_end(
            {"call_id": "c1", "transcript": [], "metrics": metrics}, metrics=metrics
        )

        assert store.call_count == 1
        assert store.get_active_calls() == []  # No longer active
        calls = store.get_calls()
        assert len(calls) == 1
        assert calls[0]["call_id"] == "c1"
        assert calls[0]["metrics"]["cost"]["total"] == 0.05

    def test_get_call_by_id(self):
        store = MetricsStore()
        store.record_call_end({"call_id": "c1"})
        store.record_call_end({"call_id": "c2"})
        assert store.get_call("c1")["call_id"] == "c1"
        assert store.get_call("c2")["call_id"] == "c2"
        assert store.get_call("c999") is None

    def test_calls_ordered_newest_first(self):
        store = MetricsStore()
        store.record_call_end({"call_id": "c1"})
        store.record_call_end({"call_id": "c2"})
        store.record_call_end({"call_id": "c3"})
        calls = store.get_calls()
        assert [c["call_id"] for c in calls] == ["c3", "c2", "c1"]

    def test_max_calls_limit(self):
        store = MetricsStore(max_calls=3)
        for i in range(5):
            store.record_call_end({"call_id": f"c{i}"})
        assert store.call_count == 3
        calls = store.get_calls()
        assert [c["call_id"] for c in calls] == ["c4", "c3", "c2"]

    def test_pagination(self):
        store = MetricsStore()
        for i in range(10):
            store.record_call_end({"call_id": f"c{i}"})
        page = store.get_calls(limit=3, offset=2)
        assert len(page) == 3
        assert page[0]["call_id"] == "c7"  # newest first, skip 2

    def test_record_turn(self):
        store = MetricsStore()
        store.record_call_start({"call_id": "c1"})
        turn = TurnMetrics(
            turn_index=0,
            user_text="hi",
            agent_text="hello",
            latency=LatencyBreakdown(total_ms=100),
        )
        store.record_turn({"call_id": "c1", "turn": turn})
        active = store.get_active_calls()
        assert len(active[0]["turns"]) == 1

    def test_aggregates_empty(self):
        store = MetricsStore()
        agg = store.get_aggregates()
        assert agg["total_calls"] == 0
        assert agg["total_cost"] == 0.0
        assert agg["avg_duration"] == 0.0
        assert agg["avg_latency_ms"] == 0.0

    def test_aggregates_with_calls(self):
        store = MetricsStore()
        m1 = _make_metrics("c1", duration=60, cost_total=0.10, latency_avg_ms=200)
        m2 = _make_metrics("c2", duration=30, cost_total=0.05, latency_avg_ms=300)
        store.record_call_end({"call_id": "c1"}, metrics=m1)
        store.record_call_end({"call_id": "c2"}, metrics=m2)
        agg = store.get_aggregates()
        assert agg["total_calls"] == 2
        assert agg["total_cost"] == 0.15
        assert agg["avg_duration"] == 45.0
        assert agg["avg_latency_ms"] == 250.0

    def test_active_calls_count_in_aggregates(self):
        store = MetricsStore()
        store.record_call_start({"call_id": "c1"})
        store.record_call_start({"call_id": "c2"})
        agg = store.get_aggregates()
        assert agg["active_calls"] == 2

    def test_record_call_start_ignores_empty_id(self):
        store = MetricsStore()
        store.record_call_start({"call_id": ""})
        assert store.get_active_calls() == []

    def test_record_call_end_without_start(self):
        store = MetricsStore()
        store.record_call_end({"call_id": "orphan"})
        assert store.call_count == 1
        call = store.get_call("orphan")
        assert call is not None


class TestMetricsStoreDelete:
    """Soft-delete tests for the dashboard MetricsStore — parity with TS."""

    def _seed(
        self, store: MetricsStore, call_id: str, latency_ms=200.0, cost_total=0.01
    ):
        store.record_call_start(
            {
                "call_id": call_id,
                "caller": "+15551111111",
                "callee": "+15552222222",
                "direction": "inbound",
            }
        )
        store.record_call_end(
            {"call_id": call_id},
            _make_metrics(
                call_id=call_id,
                duration=30.0,
                cost_total=cost_total,
                latency_avg_ms=latency_ms,
            ),
        )

    def test_delete_hides_from_get_calls_and_call_count(self):
        store = MetricsStore()
        self._seed(store, "keep-1")
        self._seed(store, "drop-1")
        assert store.call_count == 2

        accepted = store.delete_calls(["drop-1"])
        assert accepted == ["drop-1"]
        assert store.call_count == 1
        assert [c["call_id"] for c in store.get_calls()] == ["keep-1"]
        assert store.get_call("drop-1") is None
        assert store.get_call("keep-1") is not None
        assert store.is_deleted("drop-1")
        assert not store.is_deleted("keep-1")

    def test_delete_shifts_aggregates_latency_and_cost(self):
        store = MetricsStore()
        self._seed(store, "fast", latency_ms=100.0, cost_total=0.01)
        self._seed(store, "slow", latency_ms=900.0, cost_total=0.05)
        before = store.get_aggregates()
        assert before["total_calls"] == 2
        assert before["avg_latency_ms"] == 500.0

        store.delete_calls(["slow"])
        after = store.get_aggregates()
        assert after["total_calls"] == 1
        assert after["avg_latency_ms"] == 100.0
        # 0.01 from "fast"; "slow"'s 0.05 must be gone from total_cost
        assert after["total_cost"] == 0.01

    def test_delete_filters_get_calls_in_range(self):
        store = MetricsStore()
        self._seed(store, "a")
        self._seed(store, "b")
        assert len(store.get_calls_in_range()) == 2
        store.delete_calls(["b"])
        remaining = store.get_calls_in_range()
        assert len(remaining) == 1
        assert remaining[0]["call_id"] == "a"

    def test_delete_refuses_active_calls(self):
        store = MetricsStore()
        store.record_call_start(
            {
                "call_id": "live-1",
                "caller": "+15551111111",
                "callee": "+15552222222",
                "direction": "inbound",
            }
        )
        accepted = store.delete_calls(["live-1"])
        assert accepted == []
        assert not store.is_deleted("live-1")
        assert len(store.get_active_calls()) == 1

    def test_delete_is_idempotent(self):
        store = MetricsStore()
        self._seed(store, "x")
        first = store.delete_calls(["x"])
        second = store.delete_calls(["x"])
        assert first == ["x"]
        assert second == []

    def test_delete_persists_to_log_root(self, tmp_path):
        store = MetricsStore()
        # Hydrate against an empty log root so the deleted-ids file path is wired.
        (tmp_path / "calls").mkdir()
        store.hydrate(str(tmp_path))
        self._seed(store, "doomed")
        store.delete_calls(["doomed"])

        deleted_file = tmp_path / ".deleted_call_ids.json"
        assert deleted_file.is_file()

        # A fresh store re-reads the deleted set on hydrate and never resurfaces
        # the call even if the on-disk metadata is intact.
        store2 = MetricsStore()
        store2.hydrate(str(tmp_path))
        assert store2.is_deleted("doomed")

    def test_delete_handles_empty_and_blank_ids(self):
        store = MetricsStore()
        self._seed(store, "real")
        assert store.delete_calls([]) == []
        assert store.delete_calls([""]) == []
        # Unknown ids ARE accepted so a future hydrate that resurrects them
        # stays hidden — matches TS behaviour.
        assert store.delete_calls(["unknown-id"]) == ["unknown-id"]
        assert store.call_count == 1


class TestDashboardRoutes:
    """Test that dashboard routes are mountable."""

    @pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed")
    def test_mount_dashboard_adds_routes(self):
        from fastapi import FastAPI
        from getpatter.dashboard.routes import mount_dashboard

        app = FastAPI()
        store = MetricsStore()
        mount_dashboard(app, store)

        route_paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/" in route_paths
        assert "/api/dashboard/calls" in route_paths
        assert "/api/dashboard/calls/{call_id}" in route_paths
        assert "/api/dashboard/active" in route_paths
        assert "/api/dashboard/aggregates" in route_paths


class TestDashboardHTML:
    """Test that the dashboard HTML template is valid."""

    def test_html_contains_key_elements(self):
        # The dashboard is now a Vite-bundled React SPA; the server-served
        # HTML is the SPA shell (title + #root mount point + inlined JS/CSS).
        # The /api/dashboard/* calls and the literal labels (Total Calls, ...)
        # are emitted by React at runtime, not present in the static HTML.
        from getpatter.dashboard.ui import DASHBOARD_HTML

        assert "Patter" in DASHBOARD_HTML
        assert "<title>Patter | Dashboard</title>" in DASHBOARD_HTML
        assert '<div id="root">' in DASHBOARD_HTML
        # Confirms the bundled SPA loaded (not the fallback HTML stub).
        assert len(DASHBOARD_HTML) > 10_000
        # The SPA's compiled JS references the dashboard API endpoints — they
        # live inside the inlined bundle even if not in the visible markup.
        assert "/api/dashboard/" in DASHBOARD_HTML


class TestServerDashboardIntegration:
    """Test that EmbeddedServer integrates the dashboard correctly."""

    def test_server_has_dashboard_param(self):
        import inspect
        from getpatter.server import EmbeddedServer

        sig = inspect.signature(EmbeddedServer.__init__)
        assert "dashboard" in sig.parameters
        assert sig.parameters["dashboard"].default is True

    def test_serve_has_dashboard_param(self):
        import inspect
        from getpatter.client import Patter

        sig = inspect.signature(Patter.serve)
        assert "dashboard" in sig.parameters
        assert sig.parameters["dashboard"].default is True

    def test_wrap_callbacks_returns_callables(self):
        from getpatter.server import EmbeddedServer
        from getpatter.local_config import LocalConfig
        from getpatter.models import Agent

        config = LocalConfig(
            telephony_provider="twilio",
            twilio_sid="AC" + "a" * 32,
            twilio_token="test",
            openai_key="sk-test",
            phone_number="+15550001234",
            webhook_url="test.ngrok.io",
        )
        agent = Agent(
            system_prompt="Test", voice="alloy", model="gpt-4o-mini-realtime-preview"
        )
        server = EmbeddedServer(config=config, agent=agent, dashboard=True)
        # Force store creation
        from getpatter.dashboard.store import MetricsStore

        server._metrics_store = MetricsStore()

        start_cb, end_cb, metrics_cb = server._wrap_callbacks()
        assert callable(start_cb)
        assert callable(end_cb)
        assert callable(metrics_cb)

"""Tests for B2B REST API routes."""

from __future__ import annotations

import json

import pytest

from getpatter.dashboard.store import MetricsStore
from getpatter.models import CallMetrics, CostBreakdown, LatencyBreakdown


def _make_app(token: str = ""):
    from fastapi import FastAPI
    from getpatter.api_routes import mount_api

    app = FastAPI()
    store = MetricsStore()
    mount_api(app, store, token=token)
    return app, store


@pytest.mark.asyncio
async def test_list_calls_empty():
    """GET /api/v1/calls returns empty list when no calls."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/calls")
        assert r.status_code == 200
        body = r.json()
        assert body["data"] == []
        assert body["pagination"]["total"] == 0


@pytest.mark.asyncio
async def test_list_calls_with_data():
    """GET /api/v1/calls returns call list with pagination."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    for i in range(3):
        store.record_call_start({"call_id": f"c{i}"})
        store.record_call_end({"call_id": f"c{i}"})

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/calls?limit=2&offset=0")
        body = r.json()
        assert len(body["data"]) == 2
        assert body["pagination"]["total"] == 3
        assert body["pagination"]["limit"] == 2


@pytest.mark.asyncio
async def test_call_detail_found():
    """GET /api/v1/calls/{call_id} returns call when it exists."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    store.record_call_start({"call_id": "abc", "caller": "+111"})
    metrics = CallMetrics(
        call_id="abc",
        duration_seconds=10.0,
        turns=(),
        cost=CostBreakdown(total=0.01),
        latency_avg=LatencyBreakdown(),
        latency_p95=LatencyBreakdown(),
        provider_mode="pipeline",
    )
    store.record_call_end({"call_id": "abc"}, metrics=metrics)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/calls/abc")
        assert r.status_code == 200
        assert r.json()["data"]["call_id"] == "abc"
        assert r.json()["data"]["metrics"]["cost"]["total"] == 0.01


@pytest.mark.asyncio
async def test_call_detail_not_found():
    """GET /api/v1/calls/{call_id} returns 404 for unknown call."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/calls/nonexistent")
        assert r.status_code == 404
        assert r.json()["error"] == "Call not found"


@pytest.mark.asyncio
async def test_active_calls():
    """GET /api/v1/calls/active returns currently active calls."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    store.record_call_start({"call_id": "live1"})
    store.record_call_start({"call_id": "live2"})

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/calls/active")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        ids = {c["call_id"] for c in body["data"]}
        assert ids == {"live1", "live2"}


@pytest.mark.asyncio
async def test_analytics_overview():
    """GET /api/v1/analytics/overview returns aggregate stats."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    store.record_call_start({"call_id": "x1"})
    metrics = CallMetrics(
        call_id="x1",
        duration_seconds=30.0,
        turns=(),
        cost=CostBreakdown(total=0.10, stt=0.02, tts=0.03, llm=0.04, telephony=0.01),
        latency_avg=LatencyBreakdown(total_ms=400.0),
        latency_p95=LatencyBreakdown(),
        provider_mode="pipeline",
    )
    store.record_call_end({"call_id": "x1"}, metrics=metrics)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/analytics/overview")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["total_calls"] == 1
        assert data["total_cost"] == 0.1


@pytest.mark.asyncio
async def test_analytics_costs():
    """GET /api/v1/analytics/costs returns cost breakdown."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    store.record_call_start({"call_id": "c1"})
    metrics = CallMetrics(
        call_id="c1",
        duration_seconds=60.0,
        turns=(),
        cost=CostBreakdown(total=0.05, stt=0.01, tts=0.02, llm=0.01, telephony=0.01),
        latency_avg=LatencyBreakdown(),
        latency_p95=LatencyBreakdown(),
        provider_mode="pipeline",
    )
    store.record_call_end({"call_id": "c1"}, metrics=metrics)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/analytics/costs")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["total_cost"] == 0.05
        assert data["breakdown"]["stt"] == 0.01
        assert data["calls_analyzed"] == 1


@pytest.mark.asyncio
async def test_api_auth_required():
    """API endpoints require auth when token is set."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app(token="mytoken")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/calls")
        assert r.status_code == 401

        r = await client.get(
            "/api/v1/calls",
            headers={"Authorization": "Bearer mytoken"},
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_limit_cap():
    """Limit is capped at 1000."""
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/calls?limit=5000")
        assert r.status_code == 200
        assert r.json()["pagination"]["limit"] == 1000


# --- Date filtering & route ordering tests ---


@pytest.mark.asyncio
async def test_analytics_costs_valid_date_params():
    """GET /api/v1/analytics/costs filters by valid from/to dates."""
    import time
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()

    # Record a call with a known start time and cost
    now = time.time()
    store.record_call_start({"call_id": "dated1", "started_at": now})
    metrics = CallMetrics(
        call_id="dated1",
        duration_seconds=15.0,
        turns=(),
        cost=CostBreakdown(total=0.07, stt=0.02, tts=0.02, llm=0.02, telephony=0.01),
        latency_avg=LatencyBreakdown(),
        latency_p95=LatencyBreakdown(),
        provider_mode="pipeline",
    )
    store.record_call_end({"call_id": "dated1"}, metrics=metrics)

    from_date = "2020-01-01T00:00:00"
    to_date = "2099-12-31T23:59:59"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            f"/api/v1/analytics/costs?from={from_date}&to={to_date}"
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["calls_analyzed"] == 1
        assert data["total_cost"] == 0.07
        assert data["period"]["from"] == from_date
        assert data["period"]["to"] == to_date


@pytest.mark.asyncio
async def test_analytics_costs_invalid_date_returns_400():
    """GET /api/v1/analytics/costs with a malformed date returns 400."""
    from httpx import AsyncClient, ASGITransport

    app, _store = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/analytics/costs?from=not-a-date")
        assert r.status_code == 400
        assert "Invalid" in r.json()["error"]


@pytest.mark.asyncio
async def test_active_calls_route_before_call_id():
    """/api/v1/calls/active must resolve before /api/v1/calls/{call_id}.

    If routes are ordered wrong, 'active' would be treated as a call_id and
    return a 404 instead of the active-calls list.
    """
    from httpx import AsyncClient, ASGITransport

    app, store = _make_app()
    store.record_call_start({"call_id": "live-x"})

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v1/calls/active")
        assert r.status_code == 200
        body = r.json()
        # Must return the active-calls shape, not the single-call shape
        assert "count" in body
        assert "data" in body
        assert isinstance(body["data"], list)
        assert body["count"] >= 1

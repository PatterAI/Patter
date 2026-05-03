"""Tests for dashboard data export."""

from __future__ import annotations

import json

import pytest

from getpatter.dashboard.export import calls_to_csv, calls_to_json


@pytest.fixture
def sample_calls():
    return [
        {
            "call_id": "c1",
            "caller": "+1234",
            "callee": "+5678",
            "direction": "inbound",
            "started_at": 1700000000,
            "ended_at": 1700000060,
            "metrics": {
                "duration_seconds": 60.0,
                "cost": {"total": 0.05, "stt": 0.01, "tts": 0.02, "llm": 0.01, "telephony": 0.01},
                "latency_avg": {"total_ms": 500.0},
                "turns": [{"turn_index": 0}, {"turn_index": 1}],
                "provider_mode": "pipeline",
            },
        },
        {
            "call_id": "c2",
            "caller": "+9999",
            "callee": "+8888",
            "direction": "outbound",
            "started_at": 1700000100,
            "ended_at": 1700000130,
            "metrics": None,
        },
    ]


def test_csv_output(sample_calls):
    """CSV export includes headers and correct data."""
    csv = calls_to_csv(sample_calls)
    lines = csv.strip().split("\n")
    assert len(lines) == 3  # header + 2 data rows
    assert "call_id" in lines[0]
    assert "c1" in lines[1]
    assert "c2" in lines[2]
    assert "0.05" in lines[1]  # total cost


def test_csv_empty():
    """CSV with no calls returns just headers."""
    csv = calls_to_csv([])
    lines = csv.strip().split("\n")
    assert len(lines) == 1
    assert "call_id" in lines[0]


def test_json_output(sample_calls):
    """JSON export returns valid JSON array."""
    result = calls_to_json(sample_calls)
    parsed = json.loads(result)
    assert len(parsed) == 2
    assert parsed[0]["call_id"] == "c1"
    assert parsed[1]["call_id"] == "c2"


def test_json_empty():
    """JSON export with no calls returns empty array."""
    result = calls_to_json([])
    assert json.loads(result) == []


@pytest.mark.asyncio
async def test_export_endpoint_csv():
    """Export endpoint returns CSV with correct content type."""
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from getpatter.dashboard.store import MetricsStore
    from getpatter.dashboard.routes import mount_dashboard

    app = FastAPI()
    store = MetricsStore()
    mount_dashboard(app, store)

    store.record_call_start({"call_id": "x1"})
    store.record_call_end({"call_id": "x1"})

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/dashboard/export/calls?format=csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "x1" in r.text


@pytest.mark.asyncio
async def test_export_endpoint_json():
    """Export endpoint returns JSON with correct content type."""
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from getpatter.dashboard.store import MetricsStore
    from getpatter.dashboard.routes import mount_dashboard

    app = FastAPI()
    store = MetricsStore()
    mount_dashboard(app, store)

    store.record_call_start({"call_id": "x1"})
    store.record_call_end({"call_id": "x1"})

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/dashboard/export/calls?format=json")
        assert r.status_code == 200
        data = json.loads(r.text)
        assert len(data) == 1
        assert data[0]["call_id"] == "x1"

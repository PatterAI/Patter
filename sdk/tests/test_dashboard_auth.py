"""Tests for dashboard authentication."""

from __future__ import annotations

import pytest

from patter.dashboard.store import MetricsStore


@pytest.fixture
def app_with_auth():
    """Create a FastAPI app with authenticated dashboard."""
    from fastapi import FastAPI
    from patter.dashboard.routes import mount_dashboard

    app = FastAPI()
    store = MetricsStore()
    mount_dashboard(app, store, token="secret123")
    return app


@pytest.fixture
def app_no_auth():
    """Create a FastAPI app without dashboard auth."""
    from fastapi import FastAPI
    from patter.dashboard.routes import mount_dashboard

    app = FastAPI()
    store = MetricsStore()
    mount_dashboard(app, store, token="")
    return app


@pytest.mark.asyncio
async def test_auth_valid_header(app_with_auth):
    """Valid bearer token in header allows access."""
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app_with_auth), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/dashboard/aggregates",
            headers={"Authorization": "Bearer secret123"},
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_auth_valid_query_param(app_with_auth):
    """Valid token as query param allows access."""
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app_with_auth), base_url="http://test"
    ) as client:
        r = await client.get("/api/dashboard/aggregates?token=secret123")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_auth_invalid_token(app_with_auth):
    """Invalid token returns 401."""
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app_with_auth), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/dashboard/aggregates",
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_auth_missing_token(app_with_auth):
    """No token returns 401."""
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app_with_auth), base_url="http://test"
    ) as client:
        r = await client.get("/api/dashboard/aggregates")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_no_auth_allows_all(app_no_auth):
    """When no token configured, all requests pass through."""
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app_no_auth), base_url="http://test"
    ) as client:
        r = await client.get("/api/dashboard/aggregates")
        assert r.status_code == 200

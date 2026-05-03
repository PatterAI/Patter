"""Unit tests for getpatter.dashboard.persistence.notify_dashboard.

Verifies the function is:
- a coroutine (async def)
- fire-and-forget: never raises on connection error
- non-blocking on the asyncio loop (completes within timeout when offline)
- correctly serializes nested dataclasses without a json round-trip
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.dashboard.persistence import _to_jsonable, notify_dashboard


# ---------------------------------------------------------------------------
# Signature / shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_notify_dashboard_is_async() -> None:
    """The function MUST be async so callers can ``await`` or
    ``asyncio.create_task`` it without blocking the loop."""
    assert inspect.iscoroutinefunction(notify_dashboard)


@pytest.mark.unit
def test_notify_dashboard_exported_from_top_level() -> None:
    """Public re-export from ``getpatter`` should still resolve to the same
    async coroutine after the signature change."""
    from getpatter import notify_dashboard as top_level

    assert inspect.iscoroutinefunction(top_level)
    assert top_level is notify_dashboard


# ---------------------------------------------------------------------------
# Fire-and-forget behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_dashboard_swallows_connection_error() -> None:
    """When the dashboard is offline the function must NOT raise — the live
    call path should never break because of an absent dashboard."""
    # Pick an unlikely-to-be-listening port so the connection refuses fast.
    await notify_dashboard({"call_id": "test"}, port=1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_dashboard_does_not_block_loop_when_offline() -> None:
    """If the dashboard is offline, the timeout must bound how long we
    block — should be <2s even on a hostile network."""
    start = time.monotonic()
    # Connection-refused on localhost typically returns instantly; this just
    # guards against a regression that drops the timeout.
    await notify_dashboard({"call_id": "blocked-test"}, port=1)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"notify_dashboard blocked for {elapsed:.2f}s"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_dashboard_uses_async_httpx_client() -> None:
    """The implementation must use ``httpx.AsyncClient`` (NOT the sync
    ``httpx.post``) so it doesn't block the asyncio loop."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))

    with patch("httpx.AsyncClient", return_value=mock_client) as mocked:
        await notify_dashboard({"call_id": "abc"}, port=8000)

    mocked.assert_called_once_with(timeout=1.0)
    mock_client.post.assert_awaited_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "http://127.0.0.1:8000/api/dashboard/ingest"
    assert kwargs["json"] == {"call_id": "abc"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_dashboard_swallows_post_exception() -> None:
    """Any exception raised by httpx (timeout, DNS, etc.) must be swallowed."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        # Must not raise.
        await notify_dashboard({"call_id": "err"}, port=8000)


# ---------------------------------------------------------------------------
# Serialization (no json round-trip)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Sample:
    foo: int
    bar: str


@pytest.mark.unit
def test_to_jsonable_flattens_nested_dataclasses() -> None:
    """Dataclasses inside the dict / list must be flattened with ``asdict``
    so ``httpx`` can JSON-encode them without a manual ``json.dumps`` pass."""
    payload = {
        "call_id": "x",
        "obj": _Sample(foo=1, bar="b"),
        "nested": {"inner": _Sample(foo=2, bar="c")},
        "list": [_Sample(foo=3, bar="d")],
    }
    out = _to_jsonable(payload)
    assert out == {
        "call_id": "x",
        "obj": {"foo": 1, "bar": "b"},
        "nested": {"inner": {"foo": 2, "bar": "c"}},
        "list": [{"foo": 3, "bar": "d"}],
    }


@pytest.mark.unit
def test_to_jsonable_passthrough_for_plain_types() -> None:
    """Plain JSON-compatible values should round-trip unchanged."""
    payload = {"a": 1, "b": "two", "c": [1, 2, 3], "d": None, "e": True}
    assert _to_jsonable(payload) == payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_dashboard_serializes_dataclass_payload() -> None:
    """End-to-end: a payload containing a nested dataclass must reach
    httpx as a plain dict (no ``json.loads(json.dumps(...))`` round-trip)."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))

    with patch("httpx.AsyncClient", return_value=mock_client):
        await notify_dashboard(
            {"call_id": "x", "metrics": _Sample(foo=42, bar="answer")},
            port=8000,
        )

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"] == {
        "call_id": "x",
        "metrics": {"foo": 42, "bar": "answer"},
    }


# ---------------------------------------------------------------------------
# Loop responsiveness — the headline reason for the change
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_does_not_block_caller() -> None:
    """Wrapping ``notify_dashboard`` with ``asyncio.create_task`` (the
    pattern used in server.py) must return immediately even if the
    dashboard is offline."""
    start = time.monotonic()
    task = asyncio.create_task(notify_dashboard({"call_id": "n"}, port=1))
    elapsed = time.monotonic() - start
    # Scheduling a task should be effectively instantaneous — far less
    # than the 1s timeout of the underlying httpx call.
    assert elapsed < 0.05, f"create_task blocked for {elapsed:.4f}s"
    # Clean up the background task so pytest doesn't warn about pending tasks.
    await task

"""Unit tests for the scheduler wrapper.

Use short real intervals (0.05-0.15 s) to exercise the real APScheduler
loop — no mocking of time libraries per Patter testing policy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

pytest.importorskip("apscheduler")

from patter.scheduler import (  # noqa: E402
    ScheduleHandle,
    schedule_interval,
    schedule_once,
    shutdown,
)


@pytest.fixture(autouse=True)
def _reset_scheduler():
    """Each test creates its own scheduler — APScheduler binds to the loop
    that instantiated it, so the singleton must be rebuilt every time when
    pytest-asyncio creates a fresh loop per function."""
    shutdown()
    yield
    shutdown()


@pytest.mark.asyncio
async def test_schedule_interval_fires_multiple_times():
    counter = {"n": 0}

    async def cb():
        counter["n"] += 1

    handle = schedule_interval(0.05, cb)
    assert isinstance(handle, ScheduleHandle)
    assert handle.pending

    await asyncio.sleep(0.25)
    handle.cancel()
    # Expect at least 3 fires in ~250ms at 50ms interval — be generous for CI.
    assert counter["n"] >= 2
    assert not handle.pending


@pytest.mark.asyncio
async def test_schedule_once_fires_once_and_stops():
    counter = {"n": 0}

    async def cb():
        counter["n"] += 1

    at = datetime.now() + timedelta(milliseconds=80)
    handle = schedule_once(at, cb)
    await asyncio.sleep(0.3)
    assert counter["n"] == 1
    assert not handle.pending


@pytest.mark.asyncio
async def test_cancel_before_fire_prevents_invocation():
    counter = {"n": 0}

    def cb():
        counter["n"] += 1

    at = datetime.now() + timedelta(milliseconds=200)
    handle = schedule_once(at, cb)
    handle.cancel()
    await asyncio.sleep(0.3)
    assert counter["n"] == 0


@pytest.mark.asyncio
async def test_sync_callback_is_supported():
    counter = {"n": 0}

    def cb():
        counter["n"] += 1

    handle = schedule_interval(0.05, cb)
    await asyncio.sleep(0.2)
    handle.cancel()
    assert counter["n"] >= 2


@pytest.mark.asyncio
async def test_exception_in_callback_does_not_cancel_job(caplog):
    counter = {"n": 0}

    async def cb():
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("boom")

    handle = schedule_interval(0.05, cb)
    await asyncio.sleep(0.25)
    handle.cancel()
    # Should still have been called more than once despite the first failing.
    assert counter["n"] >= 2


def test_shutdown_is_idempotent():
    shutdown()
    shutdown()  # must not raise

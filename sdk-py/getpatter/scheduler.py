"""Thin scheduling wrapper around APScheduler.

Provides a minimal, async-friendly surface::

    import getpatter

    handle = patter.schedule_cron("*/5 * * * *", my_async_callback)
    once = patter.schedule_once(when=datetime(...), callback=my_async_callback)

    handle.cancel()

Design choices:

* APScheduler is the de-facto Python scheduling lib (BSD 3-clause). We wrap it
  so callers do not couple to the specific vendor API — if we ever swap it,
  only :class:`ScheduleHandle` needs to keep its contract.
* Both sync and async callbacks are accepted. Async callbacks run on the
  scheduler's own loop.
* The scheduler is lazily created on first use — callers do not pay the cost
  if they never schedule anything.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Coroutine, Union

logger = logging.getLogger("getpatter.scheduler")

JobCallback = Callable[[], Union[None, Coroutine[Any, Any, None]]]


@dataclass
class ScheduleHandle:
    """Returned from every schedule call — used to cancel."""

    job_id: str
    _scheduler: Any  # AsyncIOScheduler

    def cancel(self) -> None:
        """Cancel a scheduled job; safe to call after it has already run."""
        try:
            self._scheduler.remove_job(self.job_id)
        except Exception as exc:  # pragma: no cover - APScheduler-specific
            logger.debug("ScheduleHandle.cancel: %s", exc)

    @property
    def pending(self) -> bool:
        """True if the job is still registered."""
        try:
            return self._scheduler.get_job(self.job_id) is not None
        except Exception:  # pragma: no cover
            return False


# Schedulers are scoped per event loop. A single module-level singleton would
# bind to the first loop it sees and then crash with "Event loop is closed"
# on any subsequent loop (e.g. a new pytest-asyncio test, or an app that
# recreates the loop on reload). Keying on id(loop) avoids that footgun while
# keeping the "shared scheduler per loop" ergonomics.
_schedulers_by_loop: dict[int, Any] = {}


def _get_scheduler() -> Any:
    """Lazily construct the APScheduler ``AsyncIOScheduler`` bound to the current loop."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    existing = _schedulers_by_loop.get(id(loop))
    if existing is not None:
        # If the loop was torn down under us (e.g. pytest-asyncio finished a
        # test), drop the stale entry and build a fresh scheduler.
        if getattr(loop, "is_closed", lambda: False)():
            _schedulers_by_loop.pop(id(loop), None)
        else:
            return existing

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Scheduling requires the 'apscheduler' package. "
            "Install with: pip install getpatter[scheduling]"
        ) from exc

    scheduler = AsyncIOScheduler(event_loop=loop)
    scheduler.start()
    _schedulers_by_loop[id(loop)] = scheduler
    return scheduler


def reset_for_tests() -> None:
    """Tear down every cached scheduler. Call from test teardown to avoid state bleed."""
    for sched in list(_schedulers_by_loop.values()):
        try:
            sched.shutdown(wait=False)
        except Exception:  # pragma: no cover
            pass
    _schedulers_by_loop.clear()


def _wrap_callback(cb: JobCallback) -> Callable[[], Any]:
    """Run sync and async callbacks uniformly on the scheduler loop."""
    if inspect.iscoroutinefunction(cb):
        async def runner() -> None:
            try:
                await cb()  # type: ignore[misc]
            except Exception:
                logger.exception("Scheduled async callback raised")
        return runner

    def sync_runner() -> None:
        try:
            result = cb()
            if inspect.iscoroutine(result):
                # Callback returned a coroutine despite not being flagged as
                # async — schedule it on the running loop.
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    asyncio.run(result)
        except Exception:
            logger.exception("Scheduled callback raised")

    return sync_runner


def schedule_cron(cron: str, callback: JobCallback) -> ScheduleHandle:
    """Schedule ``callback`` on a cron expression (5-field: m h dom mon dow).

    Example::

        schedule_cron("0 9 * * 1-5", send_morning_reminder)
    """
    try:
        from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Scheduling requires the 'apscheduler' package."
        ) from exc

    scheduler = _get_scheduler()
    trigger = CronTrigger.from_crontab(cron)
    job_id = f"cron-{uuid.uuid4().hex}"
    scheduler.add_job(_wrap_callback(callback), trigger=trigger, id=job_id)
    return ScheduleHandle(job_id=job_id, _scheduler=scheduler)


def schedule_once(at: datetime, callback: JobCallback) -> ScheduleHandle:
    """Schedule ``callback`` to run once at the given datetime."""
    try:
        from apscheduler.triggers.date import DateTrigger  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Scheduling requires the 'apscheduler' package."
        ) from exc

    scheduler = _get_scheduler()
    trigger = DateTrigger(run_date=at)
    job_id = f"once-{uuid.uuid4().hex}"
    scheduler.add_job(_wrap_callback(callback), trigger=trigger, id=job_id)
    return ScheduleHandle(job_id=job_id, _scheduler=scheduler)


def schedule_interval(seconds: float, callback: JobCallback) -> ScheduleHandle:
    """Schedule ``callback`` to run every ``seconds`` seconds.

    Convenience wrapper on top of APScheduler's ``IntervalTrigger`` — used by
    the test suite and by callers that don't want to write a cron spec.
    """
    try:
        from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Scheduling requires the 'apscheduler' package."
        ) from exc

    scheduler = _get_scheduler()
    trigger = IntervalTrigger(seconds=seconds)
    job_id = f"interval-{uuid.uuid4().hex}"
    scheduler.add_job(_wrap_callback(callback), trigger=trigger, id=job_id)
    return ScheduleHandle(job_id=job_id, _scheduler=scheduler)


def shutdown() -> None:
    """Tear down every active scheduler. Safe to call even if never initialised."""
    reset_for_tests()

"""Fire-and-forget anonymous telemetry client.

Design invariants (this guards live phone calls — see the rules):

* **Never blocks the call path.** ``record`` only appends to an in-memory buffer
  and, if an event loop is running, schedules a background flush. It never awaits
  a network call inline.
* **Never throws into user code.** Every public entry point swallows all errors
  and degrades to a ``debug`` log.
* **Identical behaviour offline.** A DNS failure / timeout / non-2xx is dropped
  silently; the SDK behaves the same whether the collector is reachable or not.
* **Bounded memory.** The buffer is a fixed-size ``deque`` — when full it drops
  the oldest event rather than growing or blocking the producer.
* **Best-effort flush on exit.** An ``atexit`` hook does a short, synchronous,
  best-effort send of whatever is still buffered (covers construct-but-never-serve
  scripts); ``aclose`` does the async flush on graceful shutdown.

Disabled instances are cheap no-ops: ``record`` returns immediately and no
network client is ever constructed.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import weakref
from collections import deque
from typing import Any

from getpatter.telemetry.consent import is_enabled
from getpatter.telemetry.env import is_truthy
from getpatter.telemetry.events import build_event

logger = logging.getLogger("getpatter.telemetry")

DEFAULT_ENDPOINT = "https://telemetry.getpatter.com/v1/ingest"

_TIMEOUT_S = 3.0
_FLUSH_TIMEOUT_S = 2.0
_ATEXIT_TIMEOUT_S = 0.25  # keep process-exit blocking minimal for short-lived runs
_BUFFER_MAX = 256

# Module-level registry so a single ``atexit`` hook flushes every live client
# without leaking one handler per instance. A ``WeakSet`` lets a client whose
# owning ``Patter`` was discarded be garbage-collected and auto-removed — a
# long-running process that creates and drops many ``Patter`` instances does not
# accumulate dead telemetry clients (or their HTTP connections).
_LIVE_CLIENTS: "weakref.WeakSet[TelemetryClient]" = weakref.WeakSet()
_ATEXIT_REGISTERED = False

# One-time "telemetry is on" notice, shown once per process.
_NOTICE_SHOWN = False


def _show_notice_once() -> None:
    global _NOTICE_SHOWN
    if _NOTICE_SHOWN:
        return
    _NOTICE_SHOWN = True
    logger.info(
        "Anonymous usage telemetry is on (no PII, no call content). Collected: "
        "a random anonymous install id, SDK version, language, OS family, runtime "
        "version, coarse feature flags, the composed stack (provider + model per "
        "layer), tool counts, integration category, and per-call duration, latency, "
        "cost, and error codes (no call content, no message text). "
        "Disable with PATTER_TELEMETRY_DISABLED=1, DO_NOT_TRACK=1, or telemetry=False. "
        "Details: https://docs.getpatter.com/telemetry"
    )


def _register_atexit() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    _ATEXIT_REGISTERED = True
    atexit.register(_atexit_flush_all)


def _atexit_flush_all() -> None:
    """Best-effort synchronous flush of every live client at interpreter exit."""
    for client in list(_LIVE_CLIENTS):
        try:
            client._flush_sync()
        except Exception:  # pragma: no cover — never let exit handlers raise
            pass


class TelemetryClient:
    """Buffers and ships anonymous usage events. Safe to construct unconditionally."""

    def __init__(
        self,
        *,
        sdk_version: str,
        flag: bool | None = None,
        endpoint: str | None = None,
    ) -> None:
        self._sdk_version = sdk_version
        self._enabled = is_enabled(flag)
        self._endpoint = (
            endpoint or os.getenv("PATTER_TELEMETRY_ENDPOINT") or DEFAULT_ENDPOINT
        )
        self._debug = is_truthy(os.getenv("PATTER_TELEMETRY_DEBUG"))
        self._buffer: deque[dict[str, Any]] = deque(maxlen=_BUFFER_MAX)
        self._flush_task: asyncio.Task[None] | None = None
        self._http: Any = None  # httpx.AsyncClient, lazily created
        self._closed = False

        if self._enabled and not self._debug:
            _show_notice_once()
            _register_atexit()
            _LIVE_CLIENTS.add(self)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- public API ----------------------------------------------------------

    def record(self, name: str, **dimensions: Any) -> None:
        """Enqueue an event. Fire-and-forget; never raises, never blocks."""
        if not self._enabled or self._closed:
            return
        try:
            event = build_event(
                name, sdk_version=self._sdk_version, dimensions=dimensions
            )
        except Exception:
            logger.debug("telemetry build_event failed", exc_info=True)
            return

        if self._debug:
            # Print-without-send: the highest-trust audit feature.
            try:
                sys.stderr.write("[patter telemetry] " + json.dumps(event) + "\n")
            except Exception:
                pass
            return

        try:
            self._buffer.append(event)  # maxlen deque drops oldest when full
            self._schedule_flush()
        except Exception:
            logger.debug("telemetry enqueue failed", exc_info=True)

    def flush_pending(self) -> None:
        """Schedule a flush of any buffered events.

        Events recorded before an event loop exists (e.g. at ``Patter(...)``
        construction) sit in the buffer until a loop is available. Call this once
        the server is running so those events ship promptly instead of waiting
        for process exit. Safe and cheap when disabled or when nothing is buffered.
        """
        if not self._enabled or self._debug:
            return
        try:
            self._schedule_flush()
        except Exception:
            logger.debug("telemetry flush_pending failed", exc_info=True)

    async def aclose(self) -> None:
        """Flush remaining events and release the HTTP client (graceful shutdown)."""
        if self._closed:
            return
        self._closed = True
        _LIVE_CLIENTS.discard(self)
        if not self._enabled or self._debug:
            return
        try:
            await asyncio.wait_for(self._flush(), timeout=_FLUSH_TIMEOUT_S)
        except Exception:
            logger.debug("telemetry aclose flush failed", exc_info=True)
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    # -- internals -----------------------------------------------------------

    def _schedule_flush(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop yet — events flush on the next loop tick or atexit.
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = loop.create_task(self._flush())
        self._flush_task.add_done_callback(self._on_flush_done)

    @staticmethod
    def _on_flush_done(task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("telemetry flush task raised", exc_info=exc)

    def _drain(self) -> list[dict[str, Any]]:
        events = list(self._buffer)
        self._buffer.clear()
        return events

    async def _flush(self) -> None:
        if not self._buffer:
            return
        events = self._drain()
        try:
            http = self._ensure_http()
            await http.post(self._endpoint, json=events)
            # Status is ignored — telemetry is best-effort and never load-bearing.
        except Exception:
            # Drop on any failure; do NOT requeue (avoids unbounded growth and
            # keeps offline behaviour identical to online).
            logger.debug("telemetry flush failed", exc_info=True)

    def _ensure_http(self) -> Any:
        if self._http is None:
            import httpx  # lazy — a missing/old httpx degrades to a no-op send

            self._http = httpx.AsyncClient(timeout=_TIMEOUT_S)
        return self._http

    def _flush_sync(self) -> None:
        """Synchronous best-effort flush used only by the ``atexit`` hook."""
        if not self._enabled or self._debug or not self._buffer:
            return
        events = self._drain()
        try:
            import httpx

            with httpx.Client(timeout=_ATEXIT_TIMEOUT_S) as client:
                client.post(self._endpoint, json=events)
        except Exception:
            logger.debug("telemetry atexit flush failed", exc_info=True)

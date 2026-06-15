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
import hashlib
import json
import logging
import os
import sys
import weakref
from collections import deque
from typing import Any

from getpatter.telemetry.consent import is_enabled
from getpatter.telemetry.env import is_truthy, sample_rate
from getpatter.telemetry.events import (
    EVENT_CALL_COMPLETED,
    EVENT_CALL_STARTED,
    build_event,
)
from getpatter.telemetry.install_id import run_id

logger = logging.getLogger("getpatter.telemetry")

DEFAULT_ENDPOINT = "https://telemetry.getpatter.com/v1/ingest"

_TIMEOUT_S = 3.0
_FLUSH_TIMEOUT_S = 2.0
# Bounded process-exit flush. 0.25 s was routinely too short for a cold TLS
# POST (the final batch of a short-lived script was lost); 1 s delivers it
# while keeping exit blocking small. Scripts that call disconnect() don't rely
# on this — disconnect() drains with the loop still alive.
_ATEXIT_TIMEOUT_S = 1.0
_BUFFER_MAX = 256
# The relay rejects batches larger than 64 events per request — a full buffer
# must ship as multiple POSTs or events 65..256 silently vanish server-side.
_MAX_EVENTS_PER_POST = 64

# The ONLY events the client-side sampling gate may drop. Everything else
# (first_run, config_incomplete, sdk_initialized, cli_command, feature_used,
# agent_configured) is always delivered — those are low-frequency activation /
# usage signals, never high-volume call traffic. An *error* call_completed is
# also force-kept (see ``_keep_event``) so error rates stay unbiased.
_SAMPLEABLE_EVENTS = frozenset({EVENT_CALL_STARTED, EVENT_CALL_COMPLETED})


def _run_keep_ratio() -> float:
    """A stable per-run value in ``[0, 1)`` derived from this process's run id.

    Hashing the run id (constant for the process) makes the keep/drop decision
    DETERMINISTIC PER RUN: the whole run consistently keeps or drops its
    sampleable call events rather than flipping a per-event coin — which would
    bias short runs (a 3-call run at rate=0.5 could keep 0 or 3 by luck). Any
    failure degrades to ``0.0`` so the caller keeps the event (fail safe).
    """
    try:
        digest = hashlib.sha256(run_id().encode("utf-8")).hexdigest()[:8]
        return int(digest, 16) / 0xFFFFFFFF
    except Exception:  # pragma: no cover — hashing a str never realistically fails
        return 0.0


# Module-level registry so a single ``atexit`` hook flushes every live client
# without leaking one handler per instance. A ``WeakSet`` lets a client whose
# owning ``Patter`` was discarded be garbage-collected and auto-removed — a
# long-running process that creates and drops many ``Patter`` instances does not
# accumulate dead telemetry clients (or their HTTP connections).
_LIVE_CLIENTS: "weakref.WeakSet[TelemetryClient]" = weakref.WeakSet()
_ATEXIT_REGISTERED = False

# Strong references to clients that still hold undelivered events. The registry
# above is deliberately weak so a discarded ``Patter``'s client can be collected —
# but a client constructed fire-and-forget (the CLI pattern:
# ``TelemetryClient(...).record(...)`` with no reference held) must not take its
# buffered events to the grave before a flush delivers them. A client is held
# strongly from its first buffered event until the buffer drains, so the lifetime
# is bounded by the next flush (async task, ``aclose``, or the atexit hook).
_PENDING_FLUSH: "set[TelemetryClient]" = set()

# One-time "telemetry is on" notice, shown once per process.
_NOTICE_SHOWN = False


def _show_notice_once() -> None:
    global _NOTICE_SHOWN
    if _NOTICE_SHOWN:
        return
    _NOTICE_SHOWN = True
    # Written straight to stderr, not through logging: the SDK attaches no
    # handler, so logging's WARNING-level lastResort would swallow an INFO
    # disclosure — and the opt-out notice must be visible by default (it is
    # what legitimises the opt-out model; Next.js/Astro print theirs too).
    # The TS SDK's console-backed logger already shows it — this keeps parity.
    try:
        sys.stderr.write(
            "[patter] Anonymous usage telemetry is on (no PII, no call content). "
            "Collected: a random anonymous install id, SDK version, language, OS "
            "family, runtime version, coarse feature flags, the composed stack "
            "(provider + model per layer), tool counts, integration category, and "
            "per-call duration, latency, cost, error codes, and a random per-call "
            "correlation id (no call content, "
            "no message text). Disable with PATTER_TELEMETRY_DISABLED=1, "
            "DO_NOT_TRACK=1, or telemetry=False. "
            "Details: https://docs.getpatter.com/telemetry\n"
        )
    except Exception:
        pass


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

        # Client-side sampling gate (computed ONCE at construction):
        #   * ``_sample_rate`` — the effective rate in ``[0, 1]`` from the env.
        #   * ``_keep_sampled`` — the deterministic per-run keep/drop decision for
        #     the high-frequency call events. Because the seed is the run id
        #     (constant for the process) the whole run consistently keeps or
        #     drops them — never a per-event coin flip. ``rate >= 1.0`` always
        #     keeps. See ``_run_keep_ratio`` and ``record``.
        self._sample_rate = sample_rate()
        self._keep_sampled = (
            self._sample_rate >= 1.0 or _run_keep_ratio() < self._sample_rate
        )

        if self._enabled and not self._debug:
            _show_notice_once()
            _register_atexit()
            _LIVE_CLIENTS.add(self)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- public API ----------------------------------------------------------

    def record(self, name: str, **dimensions: Any) -> None:
        """Enqueue an event. Fire-and-forget; never raises, never blocks.

        Applies the client-side sampling gate (see ``__init__``): only the
        high-frequency ``call_started`` / ``call_completed`` events may be
        dropped, and only when this run's deterministic decision says so. An
        error ``call_completed`` (``outcome == "error"`` or an ``error_code``
        present) is ALWAYS kept so error rates stay unbiased. A kept sampled
        event carries ``sample_rate`` so analytics can extrapolate (weight
        ``1/sample_rate``). All other events bypass the gate entirely.
        """
        if not self._enabled or self._closed:
            return

        # -- sampling gate (high-frequency call events only) -----------------
        if name in _SAMPLEABLE_EVENTS and self._sample_rate < 1.0:
            # Force-keep error call_completed events regardless of rate — error
            # rates would be biased if errors were sampled like clean calls.
            is_error = name == EVENT_CALL_COMPLETED and (
                dimensions.get("outcome") == "error" or "error_code" in dimensions
            )
            if not self._keep_sampled and not is_error:
                return  # sampled out for this run
            # Kept (by the run decision or because it's an error): stamp the rate
            # so analytics can extrapolate (weight = 1/rate). ``build_event``
            # keeps it (the Schema phase added sample_rate to the numeric
            # allowlist). Stamped only when rate < 1.0 to keep payloads lean.
            dimensions = {**dimensions, "sample_rate": self._sample_rate}

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
            _PENDING_FLUSH.add(self)  # survive GC until the buffer drains
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

    async def drain(self, timeout: float = _FLUSH_TIMEOUT_S) -> None:
        """Flush buffered events and wait (bounded) for delivery.

        Unlike :meth:`aclose` the client stays usable afterwards — for teardown
        paths that may serve again (``Patter.disconnect()``). Without the wait,
        a process exiting right after ``disconnect()`` closes the event loop and
        cancels the fire-and-forget flush task mid-POST — the final events of a
        short-lived script (typically ``call_completed``, the one carrying
        duration/cost/latency) were routinely lost.
        """
        if not self._enabled or self._debug or self._closed:
            return
        try:
            if self._flush_task is not None and not self._flush_task.done():
                await asyncio.wait_for(self._flush_task, timeout=timeout)
            if self._buffer:
                await asyncio.wait_for(self._flush(), timeout=timeout)
        except Exception:
            logger.debug("telemetry drain failed", exc_info=True)

    async def aclose(self) -> None:
        """Flush remaining events and release the HTTP client (graceful shutdown)."""
        if self._closed:
            return
        self._closed = True
        _LIVE_CLIENTS.discard(self)
        if not self._enabled or self._debug:
            _PENDING_FLUSH.discard(self)
            return
        try:
            # A flush task scheduled by ``record`` drains the buffer immediately,
            # so flushing again below would see nothing — await the in-flight
            # delivery first or shutdown kills its POST mid-air.
            if self._flush_task is not None and not self._flush_task.done():
                await asyncio.wait_for(self._flush_task, timeout=_FLUSH_TIMEOUT_S)
        except Exception:
            logger.debug("telemetry aclose in-flight flush failed", exc_info=True)
        try:
            await asyncio.wait_for(self._flush(), timeout=_FLUSH_TIMEOUT_S)
        except Exception:
            logger.debug("telemetry aclose flush failed", exc_info=True)
        _PENDING_FLUSH.discard(self)
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

    def _on_flush_done(self, task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("telemetry flush task raised", exc_info=exc)
        # Events recorded while the POST was in flight are sitting in the buffer
        # with no flush scheduled (``record`` saw the live task and skipped) —
        # chain another flush or they strand until aclose()/process exit.
        if self._buffer and not self._closed:
            self._schedule_flush()

    def _drain(self) -> list[dict[str, Any]]:
        events = list(self._buffer)
        self._buffer.clear()
        _PENDING_FLUSH.discard(self)  # nothing buffered — GC may reclaim us again
        return events

    async def _flush(self) -> None:
        if not self._buffer:
            return
        events = self._drain()
        try:
            http = self._ensure_http()
            # Ship in relay-sized chunks: a buffer larger than the relay's
            # per-request cap would otherwise be silently truncated server-side.
            for start in range(0, len(events), _MAX_EVENTS_PER_POST):
                await http.post(
                    self._endpoint, json=events[start : start + _MAX_EVENTS_PER_POST]
                )
            # Status is ignored — telemetry is best-effort and never load-bearing.
        except Exception:
            # Drop on any failure — including this flush's remaining chunks; do
            # NOT requeue (avoids unbounded growth and keeps offline behaviour
            # identical to online).
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
                for start in range(0, len(events), _MAX_EVENTS_PER_POST):
                    client.post(
                        self._endpoint,
                        json=events[start : start + _MAX_EVENTS_PER_POST],
                    )
        except Exception:
            logger.debug("telemetry atexit flush failed", exc_info=True)

"""In-memory metrics store for the local dashboard."""

from __future__ import annotations

__all__ = ["MetricsStore", "MetricsStoreProtocol"]

import asyncio
import threading
import time
from dataclasses import asdict
from typing import Any, Protocol, runtime_checkable

from getpatter.models import CallMetrics


@runtime_checkable
class MetricsStoreProtocol(Protocol):
    """Protocol for metrics storage backends.

    B2B customers can implement this protocol to plug in their own
    metrics backend (e.g. Prometheus, Datadog, a database) while
    keeping the same dashboard interface.
    """

    def record_call_start(self, data: dict[str, Any]) -> None: ...

    def record_call_end(
        self, data: dict[str, Any], metrics: CallMetrics | None = None
    ) -> None: ...

    def record_turn(self, data: dict[str, Any]) -> None: ...

    def record_call_initiated(self, data: dict[str, Any]) -> None: ...

    def update_call_status(self, call_id: str, status: str, **extra: Any) -> None: ...

    def get_calls(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]: ...

    def get_call(self, call_id: str) -> dict[str, Any] | None: ...

    def get_active_calls(self) -> list[dict[str, Any]]: ...

    def get_aggregates(self) -> dict[str, Any]: ...

    def subscribe(self) -> asyncio.Queue: ...

    def unsubscribe(self, queue: asyncio.Queue) -> None: ...


class MetricsStore:
    """Thread-safe in-memory store for call metrics.

    Keeps the last ``max_calls`` completed calls and tracks active calls.
    Designed as a singleton attached to the EmbeddedServer.

    Supports SSE event subscribers for real-time updates.
    """

    def __init__(self, max_calls: int = 500) -> None:
        self._lock = threading.Lock()
        self._max_calls = max_calls
        self._calls: list[dict[str, Any]] = []
        self._active_calls: dict[str, dict[str, Any]] = {}
        self._subscribers: set[asyncio.Queue] = set()

    # --- SSE event bus ---

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to real-time events. Returns an asyncio.Queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove an SSE subscriber."""
        with self._lock:
            self._subscribers.discard(queue)

    def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Push an event to all SSE subscribers (non-blocking).

        Must be called OUTSIDE self._lock to avoid deadlock with
        subscribe()/unsubscribe().
        """
        event = {"type": event_type, "data": data}
        # Snapshot to avoid iteration over a mutating set
        subscribers = list(self._subscribers)
        dead: list[asyncio.Queue] = []
        for q in subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        if dead:
            with self._lock:
                for q in dead:
                    self._subscribers.discard(q)

    # --- Recording ---

    def record_call_start(self, data: dict[str, Any]) -> None:
        call_id = data.get("call_id", "")
        if not call_id:
            return
        event_data = {
            "call_id": call_id,
            "caller": data.get("caller", ""),
            "callee": data.get("callee", ""),
            "direction": data.get("direction", "inbound"),
        }
        with self._lock:
            existing = self._active_calls.get(call_id)
            # If the call was pre-registered with ``record_call_initiated``
            # (e.g., outbound dial before media arrives), upgrade its status
            # to "in-progress" instead of overwriting the from/to metadata.
            # Only overwrite ``direction`` when the caller explicitly passed
            # one in ``data`` — otherwise we'd clobber the ``outbound`` set
            # by ``record_call_initiated`` with the default ``inbound``.
            if existing is not None:
                update_payload = {
                    "call_id": event_data["call_id"],
                    "caller": event_data["caller"],
                    "callee": event_data["callee"],
                }
                if "direction" in data:
                    update_payload["direction"] = data["direction"]
                existing.update(update_payload)
                existing["status"] = "in-progress"
                existing.setdefault("turns", [])
            else:
                self._active_calls[call_id] = {
                    **event_data,
                    "started_at": time.time(),
                    "status": "in-progress",
                    "turns": [],
                }
        # Publish outside lock to avoid deadlock with subscribe/unsubscribe
        self._publish("call_start", event_data)

    def record_call_initiated(self, data: dict[str, Any]) -> None:
        """Pre-register an outbound call before any webhook fires.

        Called from ``Patter.call()`` so that even calls that never reach the
        media channel (busy, no-answer, carrier-rejected) show up in the
        dashboard. See BUG #06.
        """
        call_id = data.get("call_id", "")
        if not call_id:
            return
        entry = {
            "call_id": call_id,
            "caller": data.get("caller", ""),
            "callee": data.get("callee", ""),
            "direction": data.get("direction", "outbound"),
            "started_at": time.time(),
            "status": "initiated",
            "turns": [],
        }
        with self._lock:
            # Don't clobber a pre-existing record (e.g. inbound already moved
            # to "in-progress"). First writer wins.
            self._active_calls.setdefault(call_id, entry)
        self._publish("call_initiated", {k: entry[k] for k in ("call_id", "caller", "callee", "direction", "status")})

    def update_call_status(self, call_id: str, status: str, **extra: Any) -> None:
        """Update the status of an active or completed call.

        Used by the Twilio ``statusCallback`` handler and by operators
        wiring custom provider webhooks. Known statuses:
        ``initiated``, ``ringing``, ``in-progress``, ``completed``,
        ``no-answer``, ``busy``, ``failed``, ``canceled``, ``webhook_error``.
        """
        if not call_id or not status:
            return
        terminal = status in {"completed", "no-answer", "busy", "failed", "canceled", "webhook_error"}
        with self._lock:
            active = self._active_calls.get(call_id)
            if active is not None:
                active["status"] = status
                if extra:
                    active.update(extra)
                if terminal:
                    # Move to completed list so the UI stops the live timer.
                    entry = {
                        "call_id": call_id,
                        "caller": active.get("caller", ""),
                        "callee": active.get("callee", ""),
                        "direction": active.get("direction", "outbound"),
                        "started_at": active.get("started_at", 0),
                        "ended_at": time.time(),
                        "status": status,
                        "metrics": None,
                        **{k: v for k, v in extra.items() if k not in {"status"}},
                    }
                    self._active_calls.pop(call_id, None)
                    self._calls.append(entry)
                    if len(self._calls) > self._max_calls:
                        self._calls = self._calls[-self._max_calls:]
            else:
                # Call already completed — patch the existing row if found.
                for call in reversed(self._calls):
                    if call.get("call_id") == call_id:
                        call["status"] = status
                        if extra:
                            call.update(extra)
                        break
        self._publish("call_status", {"call_id": call_id, "status": status, **extra})

    def record_turn(self, data: dict[str, Any]) -> None:
        call_id = data.get("call_id", "")
        turn = data.get("turn")
        if not call_id or turn is None:
            return
        turn_dict = asdict(turn) if hasattr(turn, "__dataclass_fields__") else turn
        with self._lock:
            active = self._active_calls.get(call_id)
            if active is not None:
                active["turns"].append(turn_dict)
        # Publish outside lock to avoid deadlock with subscribe/unsubscribe
        self._publish("turn_complete", {"call_id": call_id, "turn": turn_dict})

    def record_call_end(
        self, data: dict[str, Any], metrics: CallMetrics | None = None
    ) -> None:
        call_id = data.get("call_id", "")
        if not call_id:
            return
        with self._lock:
            active = self._active_calls.pop(call_id, None)
            entry: dict[str, Any] = {
                "call_id": call_id,
                "ended_at": time.time(),
                "transcript": data.get("transcript", []),
            }
            if active:
                entry["caller"] = active.get("caller", "")
                entry["callee"] = active.get("callee", "")
                entry["direction"] = active.get("direction", "inbound")
                entry["started_at"] = active.get("started_at", 0)
                # Preserve any explicit status (no-answer, busy, ...) set by
                # a statusCallback during the call. Fall back to "completed".
                entry["status"] = active.get("status", "completed") if active.get("status") != "in-progress" else "completed"
            else:
                entry.setdefault("status", "completed")
            if metrics is not None:
                entry["metrics"] = asdict(metrics)
            else:
                # No metrics payload (e.g. webhook-rejected inbound, or
                # outbound call that never hit media): synthesise a minimal
                # metrics shim so the UI can still display a frozen duration.
                started = entry.get("started_at") or 0
                ended = entry.get("ended_at") or time.time()
                entry["metrics"] = {
                    "duration_seconds": max(0.0, float(ended - started)),
                    "turns": [],
                    "cost": {"total": 0.0, "stt": 0.0, "tts": 0.0, "llm": 0.0, "telephony": 0.0},
                    "latency_avg": {"total_ms": 0.0},
                    "latency_p95": {"total_ms": 0.0},
                    "provider_mode": "",
                }
            self._calls.append(entry)
            if len(self._calls) > self._max_calls:
                self._calls = self._calls[-self._max_calls :]
            event_metrics = entry.get("metrics")
        # Publish outside lock to avoid deadlock with subscribe/unsubscribe
        self._publish("call_end", {
            "call_id": call_id,
            "metrics": event_metrics,
        })

    def get_calls(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            ordered = list(reversed(self._calls))
            return ordered[offset : offset + limit]

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            for call in reversed(self._calls):
                if call["call_id"] == call_id:
                    return call
            return None

    def get_active_calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._active_calls.values())

    def get_active(self, call_id: str) -> dict[str, Any] | None:
        """Return the active-call record for ``call_id`` if present.

        Mirrors the TypeScript ``MetricsStore.getActive`` accessor used by
        handlers that need to peek at the live call (e.g., to read the
        current ``direction`` without taking a write lock).
        """
        with self._lock:
            return self._active_calls.get(call_id)

    def get_aggregates(self) -> dict[str, Any]:
        with self._lock:
            total_calls = len(self._calls)
            if total_calls == 0:
                return {
                    "total_calls": 0,
                    "total_cost": 0.0,
                    "avg_duration": 0.0,
                    "avg_latency_ms": 0.0,
                    "cost_breakdown": {
                        "stt": 0.0, "tts": 0.0, "llm": 0.0, "telephony": 0.0,
                    },
                    "active_calls": len(self._active_calls),
                }

            total_cost = 0.0
            total_duration = 0.0
            total_latency = 0.0
            latency_count = 0
            cost_stt = 0.0
            cost_tts = 0.0
            cost_llm = 0.0
            cost_tel = 0.0

            for call in self._calls:
                m = call.get("metrics")
                if m is None:
                    continue
                cost = m.get("cost", {})
                total_cost += cost.get("total", 0.0)
                cost_stt += cost.get("stt", 0.0)
                cost_tts += cost.get("tts", 0.0)
                cost_llm += cost.get("llm", 0.0)
                cost_tel += cost.get("telephony", 0.0)
                total_duration += m.get("duration_seconds", 0.0)
                avg_lat = m.get("latency_avg", {})
                t_ms = avg_lat.get("total_ms", 0.0)
                if t_ms > 0:
                    total_latency += t_ms
                    latency_count += 1

            return {
                "total_calls": total_calls,
                "total_cost": round(total_cost, 6),
                "avg_duration": round(total_duration / total_calls, 2),
                "avg_latency_ms": round(total_latency / latency_count, 1)
                if latency_count > 0
                else 0.0,
                "cost_breakdown": {
                    "stt": round(cost_stt, 6),
                    "tts": round(cost_tts, 6),
                    "llm": round(cost_llm, 6),
                    "telephony": round(cost_tel, 6),
                },
                "active_calls": len(self._active_calls),
            }

    def get_calls_in_range(
        self, from_ts: float = 0.0, to_ts: float = 0.0
    ) -> list[dict[str, Any]]:
        """Return calls within a timestamp range (inclusive)."""
        with self._lock:
            result = []
            for call in self._calls:
                started = call.get("started_at", 0)
                if from_ts and started < from_ts:
                    continue
                if to_ts and started > to_ts:
                    continue
                result.append(call)
            return result

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._calls)

    def hydrate(self, log_root: str | None) -> int:
        """Rebuild the call list from on-disk metadata.json files.

        ``CallLogger`` persists per-call envelopes under
        ``<log_root>/calls/YYYY/MM/DD/<call_id>/metadata.json``. Calling
        ``hydrate(log_root)`` once at server startup replays those files
        into the in-memory store so the dashboard survives a restart (the
        durable persistence is the JSONL/JSON files; this store is just a
        cache on top).

        Idempotent: ``call_id``s already in the store are skipped. Errors
        per file are logged at debug level and swallowed so a single
        corrupt entry doesn't block hydration.

        Returns the number of calls newly added.
        """
        import json
        import logging
        from pathlib import Path

        if not log_root:
            return 0
        calls_root = Path(log_root) / "calls"
        if not calls_root.is_dir():
            return 0

        log = logging.getLogger("getpatter.dashboard.store")
        collected: list[dict[str, Any]] = []
        with self._lock:
            seen = {c.get("call_id") for c in self._calls if c.get("call_id")}

        for year in _numeric_subdirs(calls_root):
            for month in _numeric_subdirs(year):
                for day in _numeric_subdirs(month):
                    for call_dir in day.iterdir():
                        if not call_dir.is_dir():
                            continue
                        meta_path = call_dir / "metadata.json"
                        if not meta_path.is_file():
                            continue
                        try:
                            with open(meta_path, encoding="utf-8") as fh:
                                meta = json.load(fh)
                        except (OSError, json.JSONDecodeError) as exc:
                            log.debug(
                                "MetricsStore.hydrate: skipping %s: %s",
                                meta_path,
                                exc,
                            )
                            continue
                        call_id = meta.get("call_id") or call_dir.name
                        if not call_id or call_id in seen:
                            continue
                        record = _metadata_to_call_record(call_id, meta)
                        if record is None:
                            # Unparseable started_at → skip rather than insert
                            # as epoch 0 (which would corrupt sort order).
                            log.debug(
                                "MetricsStore.hydrate: skipping %s: "
                                "unparseable started_at",
                                meta_path,
                            )
                            continue
                        collected.append(record)
                        seen.add(call_id)

        # Stable order: oldest first (matches recordCallEnd's append order).
        collected.sort(key=lambda r: r.get("started_at") or 0)

        # Re-check call_id presence under the lock before each insert.
        # Defends against the rare case where hydrate() is invoked
        # concurrently with itself or with live recordCallEnd traffic.
        with self._lock:
            existing_ids = {
                c.get("call_id") for c in self._calls if c.get("call_id")
            }
            for rec in collected:
                if rec["call_id"] in existing_ids:
                    continue
                self._calls.append(rec)
                existing_ids.add(rec["call_id"])
                if len(self._calls) > self._max_calls:
                    self._calls = self._calls[-self._max_calls :]
        return len(collected)


def _numeric_subdirs(parent):
    """Yield direct subdirectories of ``parent`` whose name is all digits."""
    try:
        entries = list(parent.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir() and entry.name.isdigit():
            yield entry


def _metadata_to_call_record(
    call_id: str, meta: dict[str, Any]
) -> dict[str, Any] | None:
    """Translate a CallLogger metadata.json payload into a CallRecord dict.

    Returns ``None`` when ``started_at`` is missing or unparseable — the
    record would otherwise be silently inserted with ``started_at = 0``
    (Unix epoch), which corrupts every sort/range query that depends on it.
    """
    from datetime import datetime

    def _to_seconds(raw: Any) -> float | None:
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        return None

    started = _to_seconds(meta.get("started_at"))
    if started is None:
        return None
    ended = _to_seconds(meta.get("ended_at"))
    metrics = meta.get("metrics") if isinstance(meta.get("metrics"), dict) else None
    transcript = meta.get("transcript") if isinstance(meta.get("transcript"), list) else []
    record: dict[str, Any] = {
        "call_id": call_id,
        "caller": meta.get("caller") or "",
        "callee": meta.get("callee") or "",
        "direction": meta.get("direction") or "inbound",
        "started_at": started,
        "status": meta.get("status") or "completed",
        "metrics": metrics,
        "transcript": transcript,
    }
    if ended is not None:
        record["ended_at"] = ended
    return record

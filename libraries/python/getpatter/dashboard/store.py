"""In-memory metrics store for the local dashboard."""

from __future__ import annotations

__all__ = ["MetricsStore", "MetricsStoreProtocol"]

import asyncio
import json
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
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
        # User-driven soft delete: call_ids the operator has removed from the
        # dashboard. The on-disk artefacts (metadata.json, transcript.jsonl)
        # are intentionally NOT touched — they serve as the durable backup.
        # All read paths (``get_calls`` / ``get_call`` / ``get_aggregates`` /
        # ``get_calls_in_range`` / ``hydrate``) filter against this set so
        # the call is invisible to the UI and excluded from rolling metrics.
        # Populated from ``<log_root>/.deleted_call_ids.json`` on hydrate so
        # deletions survive a process restart.
        self._deleted_call_ids: set[str] = set()
        self._deleted_ids_path: str | None = None

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
        """Record the moment a call's media stream begins (publishes ``call_start``)."""
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
            # Only overwrite ``caller`` / ``callee`` / ``direction`` when the
            # caller explicitly passed a non-empty value in ``data`` —
            # otherwise we'd clobber the values set by
            # ``record_call_initiated`` with the empty strings the bridge
            # sees on the outbound WS path (``/ws/stream/outbound`` carries
            # no caller/callee query parameters).
            if existing is not None:
                update_payload: dict[str, Any] = {"call_id": event_data["call_id"]}
                if event_data["caller"]:
                    update_payload["caller"] = event_data["caller"]
                if event_data["callee"]:
                    update_payload["callee"] = event_data["callee"]
                if "direction" in data and data["direction"]:
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
        dashboard.
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
        self._publish(
            "call_initiated",
            {
                k: entry[k]
                for k in ("call_id", "caller", "callee", "direction", "status")
            },
        )

    def update_call_status(self, call_id: str, status: str, **extra: Any) -> None:
        """Update the status of an active or completed call.

        Used by the Twilio ``statusCallback`` handler and by operators
        wiring custom provider webhooks. Known statuses:
        ``initiated``, ``ringing``, ``in-progress``, ``completed``,
        ``no-answer``, ``busy``, ``failed``, ``canceled``, ``webhook_error``.
        """
        if not call_id or not status:
            return
        terminal = status in {
            "completed",
            "no-answer",
            "busy",
            "failed",
            "canceled",
            "webhook_error",
        }
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
                        self._calls = self._calls[-self._max_calls :]
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
        """Append a completed conversation turn to the active call (publishes ``turn_complete``)."""
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
        """Move a call from the active set into history with final metrics (publishes ``call_end``)."""
        call_id = data.get("call_id", "")
        if not call_id:
            return
        with self._lock:
            active = self._active_calls.pop(call_id, None)
            # The Twilio ``statusCallback`` for ``CallStatus=completed``
            # arrives shortly before the WS ``stop`` frame and runs
            # ``update_call_status``, which already moved the row from
            # ``_active_calls`` into ``_calls``. By the time
            # ``record_call_end`` runs the active record is gone and the
            # completed entry already exists. Without this lookup we'd
            # append a second row with ``started_at=0`` (no active to copy
            # from) and empty caller/callee — which is then ranked first
            # by ``get_calls`` (newest wins) and the older, well-formed
            # row gets shadowed. End result: the call disappears from the
            # dashboard's 24 h window. See dashboard BUG C.
            existing_idx = -1
            existing: dict[str, Any] | None = None
            if active is None:
                for idx in range(len(self._calls) - 1, -1, -1):
                    if self._calls[idx].get("call_id") == call_id:
                        existing_idx = idx
                        existing = self._calls[idx]
                        break

            # Resolve the final transcript and turns. ``data["transcript"]``
            # from the SDK is the authoritative ``conversation_history``
            # snapshot at hang-up; when it's missing or empty (e.g.
            # webhook-rejected inbound, Realtime adopted path where
            # ``conversation.item.input_audio_transcription.completed`` raced
            # a ``response.cancel`` and never fired, or the active record
            # was already moved to ``self._calls`` by an earlier
            # statusCallback), fall back to the running transcript /
            # turns we accumulated on the active record via ``record_turn``.
            # This keeps the live-transcript pane stable across the
            # ``call_status (completed)`` → ``call_end`` gap, and matches
            # the TS parity (``dashboard/store.ts`` resolvedTranscript /
            # resolvedTurns). See dashboard BUG D.
            data_transcript = data.get("transcript") or []
            resolved_transcript: list[Any]
            if data_transcript:
                resolved_transcript = list(data_transcript)
            elif active is not None and active.get("transcript"):
                resolved_transcript = list(active["transcript"])
            elif existing is not None and existing.get("transcript"):
                resolved_transcript = list(existing["transcript"])
            else:
                resolved_transcript = []
            source_for_turns = active or existing or {}
            preserved_turns = list(source_for_turns.get("turns") or [])
            entry: dict[str, Any] = {
                "call_id": call_id,
                "ended_at": time.time(),
                "transcript": resolved_transcript,
                "turns": preserved_turns,
            }
            source = active or existing
            if source:
                entry["caller"] = source.get("caller", "")
                entry["callee"] = source.get("callee", "")
                entry["direction"] = source.get("direction", "inbound")
                entry["started_at"] = source.get("started_at", 0)
                # Preserve any explicit status (no-answer, busy, ...) set by
                # a statusCallback during the call. Fall back to "completed".
                prior_status = source.get("status")
                entry["status"] = (
                    prior_status
                    if prior_status and prior_status != "in-progress"
                    else "completed"
                )
            else:
                entry.setdefault("status", "completed")
            if metrics is not None:
                entry["metrics"] = asdict(metrics)
            elif existing is not None and existing.get("metrics"):
                # An earlier ``update_call_status`` may have written a
                # placeholder metrics dict — keep it rather than dropping
                # it on the floor when ``record_call_end`` is invoked
                # without an explicit metrics payload.
                entry["metrics"] = existing["metrics"]
            else:
                # No metrics payload (e.g. webhook-rejected inbound, or
                # outbound call that never hit media): synthesise a minimal
                # metrics shim so the UI can still display a frozen duration.
                started = entry.get("started_at") or 0
                ended = entry.get("ended_at") or time.time()
                entry["metrics"] = {
                    "duration_seconds": max(0.0, float(ended - started)),
                    "turns": [],
                    "cost": {
                        "total": 0.0,
                        "stt": 0.0,
                        "tts": 0.0,
                        "llm": 0.0,
                        "telephony": 0.0,
                    },
                    "latency_avg": {"total_ms": 0.0},
                    "latency_p50": {"total_ms": 0.0},
                    "latency_p90": {"total_ms": 0.0},
                    "latency_p95": {"total_ms": 0.0},
                    "latency_p99": {"total_ms": 0.0},
                    "provider_mode": "",
                }
            if existing_idx >= 0:
                # Update in place so the buffer doesn't grow a duplicate row.
                self._calls[existing_idx] = entry
            else:
                self._calls.append(entry)
                if len(self._calls) > self._max_calls:
                    self._calls = self._calls[-self._max_calls :]
            event_metrics = entry.get("metrics")
        # Publish outside lock to avoid deadlock with subscribe/unsubscribe
        self._publish(
            "call_end",
            {
                "call_id": call_id,
                "metrics": event_metrics,
            },
        )

    def get_calls(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Return the most recent completed calls, newest first.

        Soft-deleted call_ids (see :py:meth:`delete_calls`) are filtered out
        so the dashboard never re-shows a row the user removed. The on-disk
        artefacts are intentionally preserved as a backup.
        """
        with self._lock:
            ordered = [
                c
                for c in reversed(self._calls)
                if c.get("call_id") not in self._deleted_call_ids
            ]
            return ordered[offset : offset + limit]

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        """Return the completed-call record for ``call_id`` if present.

        Soft-deleted call_ids resolve to ``None`` so the SPA's detail pane
        cannot render a row the user removed (it falls back to the live
        record only when ``get_call`` returns ``None``, but a deleted call
        is never live by construction).
        """
        with self._lock:
            if call_id in self._deleted_call_ids:
                return None
            for call in reversed(self._calls):
                if call["call_id"] == call_id:
                    return call
            return None

    # --- Soft delete ---

    def delete_calls(self, call_ids: list[str] | set[str]) -> list[str]:
        """Soft-delete one or more calls from the dashboard view.

        Adds each ``call_id`` to an in-memory set. Subsequent reads via
        :py:meth:`get_calls` / :py:meth:`get_call` /
        :py:meth:`get_aggregates` / :py:meth:`get_calls_in_range` exclude
        the deleted ids, so rolling metrics (avg latency, total spend) are
        recomputed without them. The on-disk ``metadata.json`` /
        ``transcript.jsonl`` files written by ``CallLogger`` are NOT
        touched — they serve as a durable backup the operator can audit
        outside the dashboard.

        **Active calls are never deletable.** A call_id that is currently
        in ``_active_calls`` is silently skipped so a mid-call delete
        from the UI cannot orphan the live transcript pane.

        The deleted set is persisted to ``<log_root>/.deleted_call_ids.json``
        when :py:meth:`hydrate` has been called with a log root — so the
        deletion survives process restart. Persistence is best-effort; an
        I/O error is logged at debug level and swallowed.

        Args:
            call_ids: Iterable of call_id strings to mark deleted. Empty or
                already-deleted ids are de-duplicated. Active call_ids are
                filtered out.

        Returns:
            The list of call_ids actually accepted as deleted (post-filter).
        """
        ids = {cid for cid in (call_ids or []) if isinstance(cid, str) and cid}
        if not ids:
            return []
        with self._lock:
            # Filter out active calls — never delete a live row.
            ids -= set(self._active_calls.keys())
            # De-dup against already-deleted.
            new_ids = ids - self._deleted_call_ids
            if not new_ids:
                return []
            self._deleted_call_ids |= new_ids
            snapshot = sorted(self._deleted_call_ids)
        # Persist outside the lock; SSE publish outside the lock.
        self._persist_deleted_ids(snapshot)
        accepted = sorted(new_ids)
        self._publish("calls_deleted", {"call_ids": accepted})
        return accepted

    def is_deleted(self, call_id: str) -> bool:
        """Return ``True`` when ``call_id`` was soft-deleted from the dashboard."""
        with self._lock:
            return call_id in self._deleted_call_ids

    def get_deleted_call_ids(self) -> list[str]:
        """Return a snapshot of the soft-deleted call_ids (sorted)."""
        with self._lock:
            return sorted(self._deleted_call_ids)

    def _persist_deleted_ids(self, snapshot: list[str]) -> None:
        """Atomically write the deleted-ids set to disk. Best-effort."""
        if self._deleted_ids_path is None:
            return
        import json
        import logging
        import os
        from pathlib import Path

        path = Path(self._deleted_ids_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            payload = {"version": 1, "deleted_call_ids": snapshot}
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            logging.getLogger("getpatter.dashboard.store").debug(
                "MetricsStore._persist_deleted_ids: %s", exc
            )

    def get_active_calls(self) -> list[dict[str, Any]]:
        """Return the currently in-flight calls."""
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
        """Compute aggregate stats (call count, cost, avg duration, latency) across history.

        Soft-deleted calls are excluded so rolling metrics (avg latency,
        total spend) are recomputed without them — matching what the
        operator sees in the call list.
        """
        with self._lock:
            visible = [
                c for c in self._calls if c.get("call_id") not in self._deleted_call_ids
            ]
            total_calls = len(visible)
            if total_calls == 0:
                return {
                    "total_calls": 0,
                    "total_cost": 0.0,
                    "avg_duration": 0.0,
                    "avg_latency_ms": 0.0,
                    "cost_breakdown": {
                        "stt": 0.0,
                        "tts": 0.0,
                        "llm": 0.0,
                        "telephony": 0.0,
                    },
                    "active_calls": len(self._active_calls),
                    "sdk_version": _sdk_version(),
                }

            total_cost = 0.0
            total_duration = 0.0
            total_latency = 0.0
            latency_count = 0
            cost_stt = 0.0
            cost_tts = 0.0
            cost_llm = 0.0
            cost_tel = 0.0

            for call in visible:
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
                # Prefer the user-perceived wait time (agent_response_ms);
                # fall back to round-trip total_ms only when the SDK
                # didn't record the breakdown (legacy hydrate path).
                t_ms = avg_lat.get("agent_response_ms") or avg_lat.get("total_ms", 0.0)
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
                "sdk_version": _sdk_version(),
            }

    def get_calls_in_range(
        self, from_ts: float = 0.0, to_ts: float = 0.0
    ) -> list[dict[str, Any]]:
        """Return calls within a timestamp range (inclusive).

        Soft-deleted calls are filtered out so date-range exports and
        analytics never include rows the operator removed from the UI.
        """
        with self._lock:
            result = []
            for call in self._calls:
                if call.get("call_id") in self._deleted_call_ids:
                    continue
                started = call.get("started_at", 0)
                if from_ts and started < from_ts:
                    continue
                if to_ts and started > to_ts:
                    continue
                result.append(call)
            return result

    @property
    def call_count(self) -> int:
        """Number of completed (non-deleted) calls currently held in memory."""
        with self._lock:
            return sum(
                1 for c in self._calls if c.get("call_id") not in self._deleted_call_ids
            )

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
        log = logging.getLogger("getpatter.dashboard.store")

        # Wire the deleted-ids persistence path FIRST so any subsequent
        # ``delete_calls`` call (even before any history hydrates) lands
        # in the right file. Restoring the set from disk happens here too
        # so deletions survive a process restart.
        deleted_ids_path = Path(log_root) / ".deleted_call_ids.json"
        loaded_deleted: set[str] = set()
        if deleted_ids_path.is_file():
            try:
                with open(deleted_ids_path, encoding="utf-8") as fh:
                    payload = json.load(fh)
                raw = payload.get("deleted_call_ids", [])
                if isinstance(raw, list):
                    loaded_deleted = {
                        cid for cid in raw if isinstance(cid, str) and cid
                    }
            except (OSError, json.JSONDecodeError) as exc:
                log.debug(
                    "MetricsStore.hydrate: skipping %s: %s",
                    deleted_ids_path,
                    exc,
                )
        with self._lock:
            self._deleted_ids_path = str(deleted_ids_path)
            self._deleted_call_ids |= loaded_deleted

        calls_root = Path(log_root) / "calls"
        if not calls_root.is_dir():
            return 0

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
                        # Backfill transcript from sibling ``transcript.jsonl``
                        # when ``metadata.json`` doesn't carry the flat
                        # transcript array (CallLogger writes one turn per
                        # line; ``metadata.json`` only carries the aggregate
                        # count). Without this, hydrated past calls render
                        # with an empty transcript pane on click. Parity
                        # with TS ``loadTranscriptJsonl`` (store.ts:780).
                        if not record.get("transcript"):
                            jsonl_path = call_dir / "transcript.jsonl"
                            from_jsonl = _load_transcript_jsonl(jsonl_path)
                            if from_jsonl:
                                record["transcript"] = from_jsonl
                        collected.append(record)
                        seen.add(call_id)

        # Stable order: oldest first (matches recordCallEnd's append order).
        collected.sort(key=lambda r: r.get("started_at") or 0)

        # Re-check call_id presence under the lock before each insert.
        # Defends against the rare case where hydrate() is invoked
        # concurrently with itself or with live recordCallEnd traffic.
        with self._lock:
            existing_ids = {c.get("call_id") for c in self._calls if c.get("call_id")}
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


def _metrics_from_top_level(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Build a ``metrics`` dict from top-level CallLogger fields.

    ``CallLogger.log_call_end`` writes ``cost`` / ``latency`` / ``duration_ms`` /
    ``telephony_provider`` as top-level keys in ``metadata.json``, but the
    dashboard UI expects them under ``metrics``. Without this fallback every
    hydrated call shows ``$0.00`` and ``—`` for cost and latency.
    """
    cost = meta.get("cost") if isinstance(meta.get("cost"), dict) else None
    latency = meta.get("latency") if isinstance(meta.get("latency"), dict) else None
    duration_ms = meta.get("duration_ms")
    telephony = meta.get("telephony_provider")
    if cost is None and latency is None and duration_ms is None and not telephony:
        return None
    out: dict[str, Any] = {}
    if cost is not None:
        out["cost"] = cost
    if latency is not None:
        # Prefer the full LatencyBreakdown objects (avg/p50/p95/p99) when
        # the server persisted them. Old metadata.json files only carry
        # flat ``p50_ms/p95_ms/p99_ms`` totals — synthesize a minimal
        # latency_avg from those so the table still shows a number, but
        # no breakdown is available for those historical rows.
        full_avg = latency.get("avg") if isinstance(latency.get("avg"), dict) else None
        full_p50 = latency.get("p50") if isinstance(latency.get("p50"), dict) else None
        full_p95 = latency.get("p95") if isinstance(latency.get("p95"), dict) else None
        full_p99 = latency.get("p99") if isinstance(latency.get("p99"), dict) else None
        if full_avg:
            out["latency_avg"] = full_avg
        if full_p50:
            out["latency_p50"] = full_p50
        if full_p95:
            out["latency_p95"] = full_p95
        if full_p99:
            out["latency_p99"] = full_p99
        if not (full_avg or full_p50 or full_p95):
            out["latency_avg"] = {
                "total_ms": latency.get("p95_ms") or latency.get("p50_ms") or 0
            }
        out["latency"] = latency
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        out["duration_seconds"] = float(duration_ms) / 1000.0
    if telephony:
        out["telephony_provider"] = telephony
    return out or None


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
    if metrics is None:
        metrics = _metrics_from_top_level(meta)
    transcript = (
        meta.get("transcript") if isinstance(meta.get("transcript"), list) else []
    )
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


def _sdk_version() -> str:
    """Resolve the installed ``getpatter`` package version at runtime.

    Single source of truth: ``getpatter.__version__``. Surfaced via the
    dashboard ``/api/dashboard/aggregates`` payload so the SPA top-bar
    pill / footer always tracks the package version that's actually
    serving the dashboard — no manual sync needed when bumping versions.
    """
    try:
        from getpatter import __version__

        return str(__version__)
    except Exception:
        return ""


def _load_transcript_jsonl(file_path: Path) -> list[dict[str, Any]]:
    """Reconstruct a flat ``[{role, text, timestamp}, ...]`` transcript
    array from a per-call ``transcript.jsonl`` file written by
    ``CallLogger.log_turn``. Parity with TS ``loadTranscriptJsonl``
    (libraries/typescript/src/dashboard/store.ts:780).

    Each JSONL line carries ``user_text`` / ``agent_text`` plus a
    timestamp (``ts`` ISO-8601 or ``timestamp`` numeric seconds). Splits
    the row into one or two entries so the dashboard's transcript pane
    renders user + assistant turns interleaved. Filters the
    ``[interrupted]`` placeholder agent text (cancelled barge-in turns).
    """
    if not file_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(file_path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                ts_iso = row.get("ts")
                ts_num = row.get("timestamp")
                timestamp: float = 0.0
                if isinstance(ts_iso, str):
                    try:
                        timestamp = datetime.fromisoformat(
                            ts_iso.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        timestamp = 0.0
                if timestamp == 0.0 and isinstance(ts_num, (int, float)):
                    timestamp = float(ts_num)
                user_text = row.get("user_text") or ""
                agent_text = row.get("agent_text") or ""
                if isinstance(user_text, str) and user_text:
                    out.append(
                        {"role": "user", "text": user_text, "timestamp": timestamp}
                    )
                if (
                    isinstance(agent_text, str)
                    and agent_text
                    and agent_text != "[interrupted]"
                ):
                    out.append(
                        {
                            "role": "assistant",
                            "text": agent_text,
                            "timestamp": timestamp,
                        }
                    )
    except OSError:
        return out
    return out

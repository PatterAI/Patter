"""Per-call filesystem logging for Patter voice agents.

Opt-in, off by default. Enable by setting ``PATTER_LOG_DIR`` (absolute path
or ``"auto"`` for platform-default) before constructing ``Patter``. When
unset the CallLogger is a no-op — no files are written, no directories are
created.

Layout::

    <root>/calls/YYYY/MM/DD/<call_id>/
      metadata.json     # envelope written at call start, updated at call end
      transcript.jsonl  # one turn per line (role/text/ts/latency/cost)
      events.jsonl      # operational events (tool_call, barge_in, error)

Files are written atomically (tmp + rename) for ``metadata.json``; JSONL
files are append-only. All timestamps are UTC ISO-8601 with millisecond
precision. Phone numbers in ``metadata.json`` are masked by default via
:func:`getpatter.utils.log_sanitize.mask_phone_number`.

Schema follows industry convention — fields map to OpenTelemetry
``gen_ai.*`` semantic conventions.

Environment variables:

- ``PATTER_LOG_DIR``       — root directory or ``"auto"`` (enables logging)
- ``PATTER_LOG_RETENTION_DAYS`` — auto-cleanup threshold (default ``30``;
                                   ``0`` = keep forever)
- ``PATTER_LOG_REDACT_PHONE`` — ``full`` | ``mask`` (default) | ``hash_only``

See ``docs/python-sdk/observability.mdx`` for the full guide.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from getpatter.utils.log_sanitize import mask_phone_number, sanitize_log_value

logger = logging.getLogger("getpatter")

# --- Schema constants -----------------------------------------------------

SCHEMA_VERSION: str = "1.0"
"""Bumped on any breaking shape change; readers should tolerate unknown keys."""

DEFAULT_RETENTION_DAYS: int = 30


def _xdg_data_home() -> Path:
    env = os.environ.get("XDG_DATA_HOME")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share"


def _platform_default_root() -> Path:
    """Return the platform-idiomatic default data directory for Patter."""
    import sys

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "patter"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "patter"
        return Path.home() / "AppData" / "Local" / "patter"
    # Linux / other POSIX — honour XDG.
    return _xdg_data_home() / "patter"


def resolve_log_root(explicit: str | None = None) -> Path | None:
    """Resolve the log root directory, or ``None`` if logging is disabled.

    Precedence:
      1. ``explicit`` argument
      2. ``PATTER_LOG_DIR`` env var (``"auto"`` → platform default)
      3. disabled (return ``None``)
    """
    value = explicit if explicit is not None else os.environ.get("PATTER_LOG_DIR")
    if not value:
        return None
    if value.strip().lower() == "auto":
        return _platform_default_root()
    return Path(value).expanduser()


def _retention_days() -> int:
    raw = os.environ.get("PATTER_LOG_RETENTION_DAYS")
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(0, value)


def _redact_mode() -> str:
    raw = (os.environ.get("PATTER_LOG_REDACT_PHONE") or "mask").strip().lower()
    if raw in {"full", "mask", "hash_only"}:
        return raw
    return "mask"


def _redact_phone(raw: str) -> str:
    if not raw:
        return ""
    mode = _redact_mode()
    if mode == "full":
        return raw
    if mode == "hash_only":
        import hashlib

        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    # Default: last-4 masking, same helper as stdout logs.
    return mask_phone_number(raw)


def _utc_iso(ts: float | None = None) -> str:
    """Return an RFC 3339 / ISO 8601 UTC timestamp with millisecond precision."""
    moment = datetime.fromtimestamp(
        ts if ts is not None else time.time(), tz=timezone.utc
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON to ``path`` atomically via tmp file + rename.

    Guarantees a reader never sees a half-written file even across crashes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp.", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str))
        fh.write("\n")


class CallLogger:
    """Per-call filesystem logger.

    Instantiate once per server (or accept ``None`` to disable). All methods
    degrade gracefully: errors during file writes are logged but never raised
    to the caller — logging must not take down a live phone call.
    """

    def __init__(self, root: Path | str | None) -> None:
        self._root: Path | None = None
        if root is not None:
            self._root = Path(root).expanduser()
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                logger.info("Call logs: %s", self._root)
            except OSError as exc:
                logger.warning("Could not create call log root %s: %s", self._root, exc)
                self._root = None

    @property
    def enabled(self) -> bool:
        """True when a writable log root is configured."""
        return self._root is not None

    # -- Path helpers -----------------------------------------------------

    def _call_dir(self, call_id: str, started_at: float | None = None) -> Path | None:
        if self._root is None:
            return None
        dt = datetime.fromtimestamp(started_at or time.time(), tz=timezone.utc)
        safe_id = sanitize_log_value(call_id, max_len=64).replace("/", "_") or "unknown"
        return (
            self._root
            / "calls"
            / f"{dt.year:04d}"
            / f"{dt.month:02d}"
            / f"{dt.day:02d}"
            / safe_id
        )

    # -- Public API -------------------------------------------------------

    def log_call_start(
        self,
        call_id: str,
        *,
        caller: str = "",
        callee: str = "",
        telephony_provider: str = "",
        provider_mode: str = "",
        agent: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Create the per-call directory and seed ``metadata.json``."""
        if not self.enabled:
            return
        started_at = time.time()
        call_dir = self._call_dir(call_id, started_at)
        if call_dir is None:
            return
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "call_id": call_id,
            "trace_id": trace_id,
            "started_at": _utc_iso(started_at),
            "ended_at": None,
            "duration_ms": None,
            "status": "in_progress",
            "caller": _redact_phone(caller),
            "callee": _redact_phone(callee),
            "telephony_provider": telephony_provider,
            "provider_mode": provider_mode,
            "agent": agent or {},
            "turns": 0,
            "cost": None,
            "latency": None,
            "error": None,
        }
        try:
            _atomic_write_json(call_dir / "metadata.json", metadata)
        except OSError as exc:
            logger.warning("call_log write failed (%s): %s", call_id, exc)
        # Best-effort cleanup of ancient days — sample-based so a startup
        # on an install with 10k days of history doesn't stall.
        if os.urandom(1)[0] < 5:  # ~2% of calls trigger a sweep
            self._sweep_old_days()

    def log_turn(
        self,
        call_id: str,
        turn: dict[str, Any],
    ) -> None:
        """Append one turn record to ``transcript.jsonl``.

        ``turn`` is expected to match the ``TurnMetrics`` shape (role,
        user_text, agent_text, latency, stt_audio_seconds, tts_characters,
        timestamp). We persist the full dict so downstream tooling sees
        every field we record in metrics.
        """
        if not self.enabled:
            return
        call_dir = self._call_dir(call_id)
        if call_dir is None:
            return
        record = {
            "schema_version": SCHEMA_VERSION,
            "ts": _utc_iso(turn.get("timestamp")),
            **turn,
        }
        try:
            _append_jsonl(call_dir / "transcript.jsonl", record)
        except OSError as exc:
            logger.warning("call_log turn write failed (%s): %s", call_id, exc)

    def log_event(
        self,
        call_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append an operational event (tool_call, barge_in, error, ...)."""
        if not self.enabled:
            return
        call_dir = self._call_dir(call_id)
        if call_dir is None:
            return
        record = {
            "schema_version": SCHEMA_VERSION,
            "ts": _utc_iso(),
            "type": event_type,
            "data": payload or {},
        }
        try:
            _append_jsonl(call_dir / "events.jsonl", record)
        except OSError as exc:
            logger.warning("call_log event write failed (%s): %s", call_id, exc)

    def log_call_end(
        self,
        call_id: str,
        *,
        duration_seconds: float | None = None,
        turns: int | None = None,
        cost: dict[str, Any] | None = None,
        latency: dict[str, Any] | None = None,
        status: str = "completed",
        error: str | None = None,
    ) -> None:
        """Finalise ``metadata.json`` with end-of-call aggregates."""
        if not self.enabled:
            return
        call_dir = self._call_dir(call_id)
        if call_dir is None:
            return
        metadata_path = call_dir / "metadata.json"
        existing: dict[str, Any] = {}
        try:
            with metadata_path.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # If we never wrote a start marker (e.g. crash on setup) build
            # a minimal envelope so the final state isn't lost.
            existing = {
                "schema_version": SCHEMA_VERSION,
                "call_id": call_id,
                "started_at": None,
            }
        existing.update(
            {
                "ended_at": _utc_iso(),
                "duration_ms": round(duration_seconds * 1000, 1)
                if duration_seconds is not None
                else None,
                "status": status,
                "turns": turns,
                "cost": cost,
                "latency": latency,
                "error": error,
            }
        )
        try:
            _atomic_write_json(metadata_path, existing)
        except OSError as exc:
            logger.warning("call_log finalize failed (%s): %s", call_id, exc)

    # -- Retention --------------------------------------------------------

    def _sweep_old_days(self) -> None:
        """Remove day-directories older than ``PATTER_LOG_RETENTION_DAYS``.

        Sampled — called from ~2% of ``log_call_start`` invocations so we
        don't need a daemon. ``retention=0`` disables cleanup entirely.
        """
        if self._root is None:
            return
        days = _retention_days()
        if days == 0:
            return
        cutoff = time.time() - (days * 86400)
        root = self._root / "calls"
        if not root.is_dir():
            return
        try:
            for year_dir in root.iterdir():
                if not year_dir.is_dir() or not year_dir.name.isdigit():
                    continue
                for month_dir in year_dir.iterdir():
                    if not month_dir.is_dir() or not month_dir.name.isdigit():
                        continue
                    for day_dir in month_dir.iterdir():
                        if not day_dir.is_dir() or not day_dir.name.isdigit():
                            continue
                        try:
                            dt = datetime(
                                int(year_dir.name),
                                int(month_dir.name),
                                int(day_dir.name),
                                tzinfo=timezone.utc,
                            )
                        except ValueError:
                            continue
                        if dt.timestamp() < cutoff:
                            self._rmtree(day_dir)
                    # Drop empty month / year dirs left behind.
                    if not any(month_dir.iterdir()):
                        month_dir.rmdir()
                if not any(year_dir.iterdir()):
                    year_dir.rmdir()
        except OSError as exc:
            logger.debug("call_log sweep failed: %s", exc)

    @staticmethod
    def _rmtree(path: Path) -> None:
        for child in path.iterdir():
            if child.is_dir():
                CallLogger._rmtree(child)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
        try:
            path.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Async wrappers — handlers are async so we offload blocking IO to threads.
# ---------------------------------------------------------------------------


async def alog_call_start(logger_: CallLogger | None, *args, **kwargs) -> None:
    """Async wrapper around :meth:`CallLogger.log_call_start` (offloads blocking I/O)."""
    if logger_ is None or not logger_.enabled:
        return
    await asyncio.to_thread(logger_.log_call_start, *args, **kwargs)


async def alog_turn(
    logger_: CallLogger | None, call_id: str, turn: dict[str, Any]
) -> None:
    """Async wrapper around :meth:`CallLogger.log_turn`."""
    if logger_ is None or not logger_.enabled:
        return
    await asyncio.to_thread(logger_.log_turn, call_id, turn)


async def alog_event(
    logger_: CallLogger | None,
    call_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Async wrapper around :meth:`CallLogger.log_event`."""
    if logger_ is None or not logger_.enabled:
        return
    await asyncio.to_thread(logger_.log_event, call_id, event_type, payload)


async def alog_call_end(logger_: CallLogger | None, call_id: str, **kwargs) -> None:
    """Async wrapper around :meth:`CallLogger.log_call_end`."""
    if logger_ is None or not logger_.enabled:
        return
    await asyncio.to_thread(logger_.log_call_end, call_id, **kwargs)

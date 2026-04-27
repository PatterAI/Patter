"""Dashboard notification for live call updates.

When the SDK completes a call, it fires a POST to the standalone dashboard
(if running) so calls appear in real time.  Data lives only in memory —
nothing is written to disk.
"""

from __future__ import annotations

__all__ = ["notify_dashboard"]

import dataclasses
from typing import Any


def _default_serializer(obj: Any) -> Any:
    """JSON serializer that handles frozen dataclasses."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses inside a dict/list/tuple into plain
    JSON-serializable structures.

    Callers SHOULD pre-flatten dataclasses themselves (e.g. via ``asdict``);
    this helper is a defensive safety net so legacy call-sites that pass raw
    dataclasses (such as ``CallMetrics``) keep working without forcing a
    round-trip through ``json.dumps``/``json.loads``.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


async def notify_dashboard(call_data: dict[str, Any], port: int = 8000) -> None:
    """Fire-and-forget async POST to a running standalone dashboard.

    This coroutine is *fire-and-forget* from the caller's perspective: it
    NEVER raises, and never blocks longer than the configured timeout
    (~1s). Schedule it with ``asyncio.create_task(notify_dashboard(...))``
    so the call_start / call_end fast paths don't wait for the dashboard
    to respond.

    Silently ignores connection errors — the dashboard may not be running.
    """
    try:
        import httpx

        # Defensive flatten of nested dataclasses. The previous implementation
        # round-tripped through ``json.dumps``/``json.loads``; ``httpx`` does
        # JSON encoding itself, so we only need plain Python types.
        payload = _to_jsonable(call_data)

        async with httpx.AsyncClient(timeout=1.0) as client:
            await client.post(
                f"http://127.0.0.1:{port}/api/dashboard/ingest",
                json=payload,
            )
    except Exception:
        # Dashboard offline / network blip — never break the live call path.
        pass

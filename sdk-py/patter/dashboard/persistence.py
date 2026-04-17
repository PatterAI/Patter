"""Dashboard notification for live call updates.

When the SDK completes a call, it fires a POST to the standalone dashboard
(if running) so calls appear in real time.  Data lives only in memory —
nothing is written to disk.
"""

from __future__ import annotations

__all__ = ["notify_dashboard"]

import dataclasses
import json
from typing import Any


def _default_serializer(obj: Any) -> Any:
    """JSON serializer that handles frozen dataclasses."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def notify_dashboard(call_data: dict[str, Any], port: int = 8000) -> None:
    """Fire-and-forget POST to a running standalone dashboard.

    Silently ignores connection errors — the dashboard may not be running.
    """
    try:
        import httpx

        httpx.post(
            f"http://127.0.0.1:{port}/api/dashboard/ingest",
            json=json.loads(json.dumps(call_data, default=_default_serializer)),
            timeout=1.0,
        )
    except Exception:
        pass

"""Data export utilities for the dashboard."""

from __future__ import annotations

import csv
import io
import json
from typing import Any


def calls_to_csv(calls: list[dict[str, Any]]) -> str:
    """Convert call list to CSV string.

    Columns: call_id, caller, callee, direction, started_at, ended_at,
    duration_s, cost_total, cost_stt, cost_tts, cost_llm, cost_telephony,
    avg_latency_ms, turns_count, provider_mode.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "call_id", "caller", "callee", "direction", "started_at", "ended_at",
        "duration_s", "cost_total", "cost_stt", "cost_tts", "cost_llm",
        "cost_telephony", "avg_latency_ms", "turns_count", "provider_mode",
    ])

    for call in calls:
        metrics = call.get("metrics") or {}
        cost = metrics.get("cost", {})
        latency_avg = metrics.get("latency_avg", {})
        turns = metrics.get("turns", ())

        writer.writerow([
            call.get("call_id", ""),
            call.get("caller", ""),
            call.get("callee", ""),
            call.get("direction", ""),
            call.get("started_at", ""),
            call.get("ended_at", ""),
            metrics.get("duration_seconds", ""),
            cost.get("total", ""),
            cost.get("stt", ""),
            cost.get("tts", ""),
            cost.get("llm", ""),
            cost.get("telephony", ""),
            latency_avg.get("total_ms", ""),
            len(turns) if isinstance(turns, (list, tuple)) else "",
            metrics.get("provider_mode", ""),
        ])

    return output.getvalue()


def calls_to_json(calls: list[dict[str, Any]]) -> str:
    """Convert call list to JSON string."""
    return json.dumps(calls, default=str)

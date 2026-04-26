"""Regression tests for MetricsStore.hydrate.

Mirrors `sdk-ts/tests/dashboard-store.test.ts` (`MetricsStore.hydrate`
suite) so the cross-SDK behaviour stays in lockstep.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from getpatter.dashboard.store import MetricsStore


def _build_fixture(root: Path, calls: list[dict[str, str]]) -> None:
    """Write CallLogger-shaped metadata.json files into ``root/calls/Y/M/D/<id>/``."""
    for c in calls:
        date = datetime.fromisoformat(c["iso"].replace("Z", "+00:00"))
        year = f"{date.year:04d}"
        month = f"{date.month:02d}"
        day = f"{date.day:02d}"
        call_dir = root / "calls" / year / month / day / c["id"]
        call_dir.mkdir(parents=True, exist_ok=True)
        end = date + timedelta(seconds=30)
        meta = {
            "call_id": c["id"],
            "caller": "+15550001111",
            "callee": "+15550002222",
            "direction": "outbound",
            "started_at": date.isoformat().replace("+00:00", "Z"),
            "ended_at": end.isoformat().replace("+00:00", "Z"),
            "status": "completed",
            "metrics": {"p95_latency_ms": 1500},
        }
        meta.update(c.get("meta") or {})
        (call_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def test_returns_zero_when_log_root_missing(tmp_path: Path) -> None:
    store = MetricsStore()
    assert store.hydrate(None) == 0
    assert store.hydrate("") == 0
    assert store.hydrate(str(tmp_path / "nonexistent")) == 0
    assert store.call_count == 0


def test_rebuilds_call_list_from_disk(tmp_path: Path) -> None:
    _build_fixture(
        tmp_path,
        [
            {"id": "CA-old", "iso": "2026-04-25T10:00:00.000Z"},
            {"id": "CA-new", "iso": "2026-04-26T15:30:00.000Z"},
        ],
    )

    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 2
    listed = store.get_calls()
    assert listed[0]["call_id"] == "CA-new"  # newest first
    assert listed[1]["call_id"] == "CA-old"
    assert listed[0]["metrics"] == {"p95_latency_ms": 1500}
    assert listed[0]["direction"] == "outbound"
    assert listed[0]["status"] == "completed"


def test_idempotent_on_re_hydrate(tmp_path: Path) -> None:
    _build_fixture(
        tmp_path, [{"id": "CA-1", "iso": "2026-04-26T15:00:00.000Z"}]
    )
    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1
    assert store.hydrate(str(tmp_path)) == 0
    assert store.call_count == 1


def test_tolerates_corrupt_metadata(tmp_path: Path) -> None:
    _build_fixture(
        tmp_path, [{"id": "CA-good", "iso": "2026-04-26T15:00:00.000Z"}]
    )
    bad_dir = tmp_path / "calls" / "2026" / "04" / "26" / "CA-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "metadata.json").write_text("{ not valid json", encoding="utf-8")

    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1
    assert store.get_calls()[0]["call_id"] == "CA-good"


def test_respects_max_calls(tmp_path: Path) -> None:
    _build_fixture(
        tmp_path,
        [
            {"id": f"CA-{i}", "iso": f"2026-04-26T15:0{i}:00.000Z"}
            for i in range(7)
        ],
    )
    store = MetricsStore(max_calls=3)
    assert store.hydrate(str(tmp_path)) == 7
    listed = store.get_calls()
    assert len(listed) == 3
    assert listed[0]["call_id"] == "CA-6"
    assert listed[2]["call_id"] == "CA-4"


@pytest.mark.parametrize("invalid_name", ["not_numeric", ".DS_Store"])
def test_skips_non_numeric_directory_layers(tmp_path: Path, invalid_name: str) -> None:
    """Stray non-numeric YYYY/MM/DD entries must not break the walk."""
    _build_fixture(
        tmp_path, [{"id": "CA-only", "iso": "2026-04-26T15:00:00.000Z"}]
    )
    # Add a noise dir at the year level
    (tmp_path / "calls" / invalid_name).mkdir(parents=True, exist_ok=True)
    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1

"""Regression tests for MetricsStore.hydrate.

Mirrors `libraries/typescript/tests/dashboard-store.test.ts` (`MetricsStore.hydrate`
suite) so the cross-SDK behaviour stays in lockstep.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
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
    _build_fixture(tmp_path, [{"id": "CA-1", "iso": "2026-04-26T15:00:00.000Z"}])
    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1
    assert store.hydrate(str(tmp_path)) == 0
    assert store.call_count == 1


def test_tolerates_corrupt_metadata(tmp_path: Path) -> None:
    _build_fixture(tmp_path, [{"id": "CA-good", "iso": "2026-04-26T15:00:00.000Z"}])
    bad_dir = tmp_path / "calls" / "2026" / "04" / "26" / "CA-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "metadata.json").write_text("{ not valid json", encoding="utf-8")

    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1
    assert store.get_calls()[0]["call_id"] == "CA-good"


def test_respects_max_calls(tmp_path: Path) -> None:
    _build_fixture(
        tmp_path,
        [{"id": f"CA-{i}", "iso": f"2026-04-26T15:0{i}:00.000Z"} for i in range(7)],
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
    _build_fixture(tmp_path, [{"id": "CA-only", "iso": "2026-04-26T15:00:00.000Z"}])
    (tmp_path / "calls" / invalid_name).mkdir(parents=True, exist_ok=True)
    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1


def test_skips_records_with_unparseable_started_at(tmp_path: Path) -> None:
    """A malformed ``started_at`` must NOT land in the store as epoch 0,
    which would corrupt every sort/range query that depends on it."""
    _build_fixture(tmp_path, [{"id": "CA-good", "iso": "2026-04-26T15:00:00.000Z"}])
    bad_dir = tmp_path / "calls" / "2026" / "04" / "26" / "CA-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "metadata.json").write_text(
        json.dumps(
            {
                "call_id": "CA-bad",
                "caller": "+1",
                "callee": "+2",
                "started_at": "not-a-date",
            }
        ),
        encoding="utf-8",
    )

    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1
    listed = store.get_calls()
    assert len(listed) == 1
    assert listed[0]["call_id"] == "CA-good"
    assert all(c["call_id"] != "CA-bad" for c in listed)


def test_accepts_numeric_unix_seconds_timestamps(tmp_path: Path) -> None:
    """Documented dual-format requirement: numeric (Unix-seconds) timestamps
    must hydrate correctly alongside ISO strings."""
    call_dir = tmp_path / "calls" / "2026" / "04" / "26" / "CA-numeric"
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "metadata.json").write_text(
        json.dumps(
            {
                "call_id": "CA-numeric",
                "caller": "+1",
                "callee": "+2",
                "started_at": 1745683200,
                "ended_at": 1745683230,
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )

    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1
    listed = store.get_calls()
    assert listed[0]["started_at"] == 1745683200.0
    assert listed[0]["ended_at"] == 1745683230.0


def test_hydrate_lifts_top_level_cost_and_latency_into_metrics(tmp_path: Path) -> None:
    """``CallLogger.log_call_end`` writes ``cost`` / ``latency`` / ``duration_ms``
    at the top of metadata.json (no ``metrics`` key). The hydrate path must
    promote those into ``metrics`` so the dashboard renders cost and latency
    instead of ``$0.00`` / ``—`` for hydrated calls.
    """
    call_dir = tmp_path / "calls" / "2026" / "05" / "08" / "CA-real-shape"
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "metadata.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "call_id": "CA-real-shape",
                "started_at": "2026-05-08T23:33:00.000Z",
                "ended_at": "2026-05-08T23:33:57.000Z",
                "duration_ms": 57400,
                "status": "completed",
                "caller": "",
                "callee": "",
                "telephony_provider": "twilio",
                "provider_mode": "pipeline",
                "agent": {"provider": "pipeline", "language": "en"},
                "turns": 9,
                "cost": {
                    "stt": 0.001526,
                    "tts": 0.02988,
                    "llm": 0.000406,
                    "telephony": 0.0085,
                    "total": 0.040312,
                },
                "latency": {"p50_ms": 2127.7, "p95_ms": 3461.7, "p99_ms": 3640.1},
                "error": None,
            }
        ),
        encoding="utf-8",
    )

    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1
    rec = store.get_calls()[0]
    metrics = rec["metrics"]
    assert metrics is not None
    assert metrics["cost"]["total"] == pytest.approx(0.040312)
    assert metrics["latency"]["p95_ms"] == pytest.approx(3461.7)
    assert metrics["latency_avg"]["total_ms"] == pytest.approx(3461.7)
    assert metrics["duration_seconds"] == pytest.approx(57.4)
    assert metrics["telephony_provider"] == "twilio"


def test_hydrate_preserves_explicit_metrics_when_present(tmp_path: Path) -> None:
    """If a metadata.json already has ``metrics`` (legacy or future shape) we
    must NOT overwrite it with the top-level fallback.
    """
    call_dir = tmp_path / "calls" / "2026" / "05" / "08" / "CA-explicit"
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "metadata.json").write_text(
        json.dumps(
            {
                "call_id": "CA-explicit",
                "started_at": "2026-05-08T10:00:00Z",
                "metrics": {"cost": {"total": 0.999}, "marker": "kept"},
                "cost": {"total": 0.001},
                "latency": {"p95_ms": 9999},
            }
        ),
        encoding="utf-8",
    )
    store = MetricsStore()
    assert store.hydrate(str(tmp_path)) == 1
    metrics = store.get_calls()[0]["metrics"]
    assert metrics["marker"] == "kept"
    assert metrics["cost"]["total"] == pytest.approx(0.999)

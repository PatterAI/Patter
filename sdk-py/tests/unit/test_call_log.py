"""Unit tests for getpatter.services.call_log."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from getpatter.services.call_log import (
    CallLogger,
    SCHEMA_VERSION,
    resolve_log_root,
)


# ---------------------------------------------------------------------------
# resolve_log_root
# ---------------------------------------------------------------------------


def test_resolve_log_root_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PATTER_LOG_DIR", raising=False)
    assert resolve_log_root() is None


def test_resolve_log_root_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATTER_LOG_DIR", str(tmp_path))
    resolved = resolve_log_root()
    assert resolved == tmp_path


def test_resolve_log_root_auto_returns_platform_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATTER_LOG_DIR", "auto")
    resolved = resolve_log_root()
    assert resolved is not None
    assert resolved.name == "patter"


def test_explicit_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATTER_LOG_DIR", "/should/be/ignored")
    assert resolve_log_root(str(tmp_path)) == tmp_path


# ---------------------------------------------------------------------------
# Disabled logger — no file writes, graceful
# ---------------------------------------------------------------------------


def test_disabled_logger_is_noop(tmp_path: Path) -> None:
    logger = CallLogger(None)
    assert logger.enabled is False
    # All calls must be safe and produce no files.
    logger.log_call_start("c1", caller="+15551234567", callee="+15557654321")
    logger.log_turn("c1", {"role": "user", "text": "hi", "timestamp": 1_700_000_000.0})
    logger.log_event("c1", "tool_call", {"name": "lookup"})
    logger.log_call_end("c1", duration_seconds=10)
    # tmp_path should remain untouched.
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Enabled logger — full lifecycle
# ---------------------------------------------------------------------------


def test_call_start_writes_atomic_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATTER_LOG_REDACT_PHONE", "mask")
    logger = CallLogger(tmp_path)
    logger.log_call_start(
        "call-123",
        caller="+15551234567",
        callee="+15557654321",
        telephony_provider="twilio",
        provider_mode="openai_realtime",
        agent={"provider": "openai_realtime", "voice": "nova"},
    )
    # Find the metadata file under calls/YYYY/MM/DD/call-123/.
    found = list(tmp_path.glob("calls/*/*/*/call-123/metadata.json"))
    assert len(found) == 1
    payload = json.loads(found[0].read_text("utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["call_id"] == "call-123"
    assert payload["status"] == "in_progress"
    assert payload["telephony_provider"] == "twilio"
    assert payload["agent"]["provider"] == "openai_realtime"
    # Phone redaction default: last-4 mask.
    assert payload["caller"].endswith("4567")
    assert payload["caller"].startswith("***")


def test_call_start_respects_full_phone_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATTER_LOG_REDACT_PHONE", "full")
    logger = CallLogger(tmp_path)
    logger.log_call_start("call-abc", caller="+15551234567", callee="")
    payload = json.loads(next(tmp_path.glob("**/metadata.json")).read_text("utf-8"))
    assert payload["caller"] == "+15551234567"


def test_call_start_hash_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATTER_LOG_REDACT_PHONE", "hash_only")
    logger = CallLogger(tmp_path)
    logger.log_call_start("call-hash", caller="+15551234567", callee="")
    payload = json.loads(next(tmp_path.glob("**/metadata.json")).read_text("utf-8"))
    assert payload["caller"].startswith("sha256:")
    assert len(payload["caller"]) == len("sha256:") + 16


def test_log_turn_appends_jsonl(tmp_path: Path) -> None:
    logger = CallLogger(tmp_path)
    logger.log_call_start("c1", caller="", callee="")
    logger.log_turn("c1", {"role": "user", "text": "hello", "turn_index": 0})
    logger.log_turn("c1", {"role": "assistant", "text": "hi!", "turn_index": 0})
    transcript = next(tmp_path.glob("**/transcript.jsonl"))
    lines = transcript.read_text("utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["text"] == "hello"
    assert "ts" in first


def test_log_event_appends_jsonl(tmp_path: Path) -> None:
    logger = CallLogger(tmp_path)
    logger.log_call_start("c1", caller="", callee="")
    logger.log_event("c1", "barge_in", {"offset_ms": 850})
    events = next(tmp_path.glob("**/events.jsonl"))
    record = json.loads(events.read_text("utf-8").strip())
    assert record["type"] == "barge_in"
    assert record["data"]["offset_ms"] == 850


def test_log_call_end_finalises_metadata(tmp_path: Path) -> None:
    logger = CallLogger(tmp_path)
    logger.log_call_start("c1", caller="+15551112222", callee="+15553334444")
    logger.log_call_end(
        "c1",
        duration_seconds=42.5,
        turns=3,
        cost={"total": 0.05, "stt": 0.01},
        latency={"p50_ms": 400, "p95_ms": 900},
    )
    payload = json.loads(next(tmp_path.glob("**/metadata.json")).read_text("utf-8"))
    assert payload["status"] == "completed"
    assert payload["duration_ms"] == 42500.0
    assert payload["turns"] == 3
    assert payload["cost"]["total"] == 0.05
    assert payload["latency"]["p95_ms"] == 900
    # Original fields preserved (redacted phone, etc).
    assert payload["call_id"] == "c1"
    assert payload["caller"].endswith("2222")


def test_log_call_end_without_start_creates_minimal_envelope(tmp_path: Path) -> None:
    logger = CallLogger(tmp_path)
    # No log_call_start call — finalise should still write a record.
    logger.log_call_end("orphan", duration_seconds=1.0, status="error", error="boom")
    payload = json.loads(next(tmp_path.glob("**/metadata.json")).read_text("utf-8"))
    assert payload["call_id"] == "orphan"
    assert payload["status"] == "error"
    assert payload["error"] == "boom"


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------


def test_sweep_removes_old_day_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATTER_LOG_RETENTION_DAYS", "7")
    logger = CallLogger(tmp_path)
    # Seed a very old day directory (year 2000).
    old_dir = tmp_path / "calls" / "2000" / "01" / "15" / "old-call"
    old_dir.mkdir(parents=True)
    (old_dir / "metadata.json").write_text("{}")
    # Seed a "recent" dir for today — we don't assert on it explicitly, just
    # make sure the sweep didn't raise.
    logger._sweep_old_days()
    assert not old_dir.exists()


def test_sweep_respects_zero_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATTER_LOG_RETENTION_DAYS", "0")
    logger = CallLogger(tmp_path)
    old_dir = tmp_path / "calls" / "2000" / "01" / "15" / "old-call"
    old_dir.mkdir(parents=True)
    (old_dir / "metadata.json").write_text("{}")
    logger._sweep_old_days()
    # retention=0 means keep forever.
    assert old_dir.exists()


# ---------------------------------------------------------------------------
# Safety: writes must never take down a call
# ---------------------------------------------------------------------------


def test_write_failure_is_silently_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logger = CallLogger(tmp_path)
    logger.log_call_start("c1", caller="", callee="")
    # Make the transcript.jsonl path unwritable by pointing the root to a
    # nonexistent directory AFTER init. We simulate by moving the root away.
    import shutil

    shutil.rmtree(tmp_path)
    # All subsequent calls must not raise.
    logger.log_turn("c1", {"role": "user", "text": "boom"})
    logger.log_event("c1", "error", {"detail": "sim"})
    logger.log_call_end("c1", duration_seconds=1)

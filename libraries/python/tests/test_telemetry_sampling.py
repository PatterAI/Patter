"""Authentic tests for the client-side telemetry sampling gate (F4).

A real local HTTP collector (stdlib ``http.server`` in a background thread)
captures what the SDK actually sends over a real ``httpx`` POST. The sampling
decision, the per-run hash of ``run_id``, the ``sample_rate`` stamping, the
payload builder, and the network egress are all REAL — only the CI/test
*environment detection* is neutralised (so the enabled path runs inside pytest)
and the network boundary is local. ``run_id`` is the only thing pinned to a
fixed value per test, because the keep/drop decision is deterministic per run
and the test needs to exercise both the kept and the dropped branch.

Mirrors ``libraries/typescript/tests/telemetry-sampling.test.ts``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from getpatter.telemetry import client as client_mod
from getpatter.telemetry.client import TelemetryClient, _run_keep_ratio
from getpatter.telemetry.env import sample_rate

# Two fixed run ids whose SHA-256-derived ratio straddles 0.5, so at rate=0.5
# one run KEEPS its sampleable call events and the other DROPS them. Computed
# from the real ``_run_keep_ratio`` formula (asserted below, not trusted blind).
_KEEP_RUN_ID = "run00000000000000000000000000000000"  # ratio ~0.031 -> keep @0.5
_DROP_RUN_ID = "run00000000000000000000000000000003"  # ratio ~0.742 -> drop @0.5


class _Collector:
    """A real local HTTP server that records POSTed JSON bodies."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://127.0.0.1:{port}/v1/ingest"

    @property
    def events(self) -> list[dict]:
        out: list[dict] = []
        for batch in self.requests:
            if isinstance(batch, list):
                out.extend(batch)
        return out

    def event_names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def start(self) -> None:
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (stdlib name)
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length)
                try:
                    collector.requests.append(json.loads(body))
                except Exception:
                    collector.requests.append(body)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args: object) -> None:  # silence
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def collector():
    c = _Collector()
    c.start()
    yield c
    c.stop()


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    """Neutralise CI/test detection, isolate the state dir, clear disablers → ON."""
    monkeypatch.setattr("getpatter.telemetry.consent.is_ci", lambda: False)
    monkeypatch.setattr("getpatter.telemetry.consent.is_test", lambda: False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_DISABLED", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_DEBUG", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_ENDPOINT", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_SAMPLE", raising=False)
    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", str(tmp_path / "getpatter-state"))


def _pin_run_id(monkeypatch, run_id: str) -> None:
    """Pin the per-run sampling seed. ``record``/``__init__`` read ``run_id`` via
    the client module's imported symbol; ``build_event`` reads its own (the run_id
    *field* on the wire is irrelevant to the gate)."""
    monkeypatch.setattr(client_mod, "run_id", lambda: run_id)


async def _wait_for(collector: _Collector, n: int, timeout: float = 2.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while len(collector.events) < n and loop.time() < deadline:
        await asyncio.sleep(0.01)


# --- the seed fixtures are real (the formula is the source of truth) ---------


def test_seed_run_ids_straddle_half():
    """Guard the fixtures: the two pinned run ids really do keep/drop at 0.5."""
    assert _run_keep_ratio_for(_KEEP_RUN_ID) < 0.5
    assert _run_keep_ratio_for(_DROP_RUN_ID) >= 0.5


def _run_keep_ratio_for(run_id: str) -> float:
    import hashlib

    return int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


# --- env: sample_rate() parsing + clamping + fail-safe ----------------------


def test_sample_rate_defaults_to_one_when_unset(monkeypatch):
    monkeypatch.delenv("PATTER_TELEMETRY_SAMPLE", raising=False)
    assert sample_rate() == 1.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", 0.0),
        ("0.0", 0.0),
        ("0.5", 0.5),
        ("1", 1.0),
        ("1.0", 1.0),
        ("  0.25  ", 0.25),  # surrounding whitespace tolerated
    ],
)
def test_sample_rate_parses_valid_values(monkeypatch, raw, expected):
    monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", raw)
    assert sample_rate() == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "0.5x", "-0.1", "-1", "2", "1.5", "10", "nan", "inf", "-inf"],
)
def test_sample_rate_malformed_or_out_of_range_degrades_to_one(monkeypatch, raw):
    # Fail safe: anything not a clean float in [0, 1] keeps everything (1.0).
    monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", raw)
    assert sample_rate() == 1.0


def test_sample_rate_never_raises(monkeypatch):
    for raw in ["", "garbage", "NaN", "1e9999", "+-+"]:
        monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", raw)
        sample_rate()  # must not raise


# --- rate = 0: call events dropped; activation/error events still delivered --


async def test_rate_zero_drops_call_events_keeps_activation_and_errors(
    enabled, collector, monkeypatch
):
    monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", "0")
    # run id is irrelevant at rate 0 (keep_sampled is always False), but pin it
    # for a stable test.
    _pin_run_id(monkeypatch, _KEEP_RUN_ID)
    client = TelemetryClient(sdk_version="0.6.8", endpoint=collector.url)

    # High-frequency call events: dropped.
    client.record("call_started", engine="realtime", carrier="twilio")
    client.record("call_completed", outcome="completed", carrier="twilio")

    # NEVER-sampled events: always delivered, even at rate 0.
    client.record("first_run")
    client.record("config_incomplete", missing="carrier_credentials")
    client.record("sdk_initialized", engine="realtime")

    # An ERROR call_completed is force-kept regardless of rate (unbiased errors).
    client.record(
        "call_completed", outcome="error", error_code="provider_error", carrier="twilio"
    )

    await _wait_for(collector, 4)
    await client.aclose()

    names = sorted(collector.event_names())
    # Two clean call events dropped; everything else delivered.
    assert names == [
        "call_completed",  # the error one
        "config_incomplete",
        "first_run",
        "sdk_initialized",
    ]
    # The single surviving call_completed is the error one and carries sample_rate.
    err = next(e for e in collector.events if e["event"] == "call_completed")
    assert err["outcome"] == "error"
    assert err["error_code"] == "provider_error"
    assert err["sample_rate"] == 0.0


async def test_rate_zero_error_via_error_code_only_is_kept(
    enabled, collector, monkeypatch
):
    # An error_code key alone (outcome may still be "completed") forces a keep.
    monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", "0")
    _pin_run_id(monkeypatch, _KEEP_RUN_ID)
    client = TelemetryClient(sdk_version="0.6.8", endpoint=collector.url)
    client.record("call_completed", outcome="completed", error_code="timeout")
    client.record("call_started", engine="realtime")  # dropped
    await _wait_for(collector, 1)
    await client.aclose()

    assert collector.event_names() == ["call_completed"]
    assert collector.events[0]["error_code"] == "timeout"


# --- rate = 0.5: deterministic keep vs drop + sample_rate stamping -----------


async def test_rate_half_kept_run_delivers_call_events_with_sample_rate(
    enabled, collector, monkeypatch
):
    monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", "0.5")
    _pin_run_id(monkeypatch, _KEEP_RUN_ID)  # ratio < 0.5 -> this run KEEPS
    client = TelemetryClient(sdk_version="0.6.8", endpoint=collector.url)
    assert client._keep_sampled is True
    assert client._sample_rate == 0.5

    client.record("call_started", engine="realtime", carrier="twilio")
    client.record("call_completed", outcome="completed", carrier="twilio")
    await _wait_for(collector, 2)
    await client.aclose()

    assert sorted(collector.event_names()) == ["call_completed", "call_started"]
    # Both kept sampled events carry the rate so analytics can weight by 1/rate.
    for e in collector.events:
        assert e["sample_rate"] == 0.5


async def test_rate_half_dropped_run_drops_clean_call_events(
    enabled, collector, monkeypatch
):
    monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", "0.5")
    _pin_run_id(monkeypatch, _DROP_RUN_ID)  # ratio >= 0.5 -> this run DROPS
    client = TelemetryClient(sdk_version="0.6.8", endpoint=collector.url)
    assert client._keep_sampled is False

    client.record("call_started", engine="realtime")
    client.record("call_completed", outcome="completed")
    # first_run still passes; an error call_completed still passes.
    client.record("first_run")
    client.record("call_completed", outcome="error", error_code="auth")
    await _wait_for(collector, 2)
    await client.aclose()

    assert sorted(collector.event_names()) == ["call_completed", "first_run"]
    err = next(e for e in collector.events if e["event"] == "call_completed")
    assert err["outcome"] == "error"
    assert err["sample_rate"] == 0.5  # force-kept error still carries the rate


async def test_rate_half_decision_is_stable_across_repeated_records(
    enabled, collector, monkeypatch
):
    """Determinism: the SAME run id yields the SAME keep/drop decision for every
    record call within the process — not a per-event coin flip."""
    monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", "0.5")
    _pin_run_id(monkeypatch, _DROP_RUN_ID)  # this run drops sampleable events
    client = TelemetryClient(sdk_version="0.6.8", endpoint=collector.url)

    for _ in range(20):
        client.record("call_started", engine="realtime")
    await asyncio.sleep(0.1)
    await client.aclose()

    # All 20 dropped — consistently, never a stray keep.
    assert collector.events == []

    # And a fresh client in the same run (same pinned run id) makes the SAME call.
    client2 = TelemetryClient(sdk_version="0.6.8", endpoint=collector.url)
    assert client2._keep_sampled is False


# --- unset / rate >= 1: no sampling, no stamping ----------------------------


async def test_unset_keeps_everything_without_sample_rate(
    enabled, collector, monkeypatch
):
    monkeypatch.delenv("PATTER_TELEMETRY_SAMPLE", raising=False)
    _pin_run_id(monkeypatch, _DROP_RUN_ID)  # would drop IF sampling were active
    client = TelemetryClient(sdk_version="0.6.8", endpoint=collector.url)
    assert client._sample_rate == 1.0
    assert client._keep_sampled is True

    client.record("call_started", engine="realtime")
    client.record("call_completed", outcome="completed")
    await _wait_for(collector, 2)
    await client.aclose()

    assert sorted(collector.event_names()) == ["call_completed", "call_started"]
    # Payloads stay lean at rate 1.0 — no sample_rate stamped.
    for e in collector.events:
        assert "sample_rate" not in e


async def test_malformed_value_degrades_to_keep_all(enabled, collector, monkeypatch):
    monkeypatch.setenv("PATTER_TELEMETRY_SAMPLE", "garbage")
    _pin_run_id(monkeypatch, _DROP_RUN_ID)
    client = TelemetryClient(sdk_version="0.6.8", endpoint=collector.url)
    assert client._sample_rate == 1.0  # fail safe

    client.record("call_started", engine="realtime")
    client.record("call_completed", outcome="completed")
    await _wait_for(collector, 2)
    await client.aclose()

    assert sorted(collector.event_names()) == ["call_completed", "call_started"]
    for e in collector.events:
        assert "sample_rate" not in e


def test_run_keep_ratio_is_in_unit_interval():
    r = _run_keep_ratio()
    assert 0.0 <= r < 1.0001

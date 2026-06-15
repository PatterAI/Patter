"""Authentic tests for the anonymous telemetry client.

A real local HTTP collector (stdlib ``http.server`` in a background thread)
captures what the SDK actually sends over a real ``httpx`` POST. Only the
CI/test *environment detection* is neutralised (so the enabled path can run
inside pytest/CI) — the consent logic, buffer, payload builder, redaction, and
network egress are all real.
"""

from __future__ import annotations

import asyncio
import gc
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from getpatter.telemetry import build_event, stack
from getpatter.telemetry.client import TelemetryClient


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
        """Flattened list of every event across all received batches."""
        out: list[dict] = []
        for batch in self.requests:
            if isinstance(batch, list):
                out.extend(batch)
        return out

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
    """Neutralise CI/test detection, isolate the state dir, clear disablers → ON.

    Pointing ``PATTER_TELEMETRY_STATE_DIR`` at a per-test tmp dir keeps the
    install-id, first-run, opt-out, and version files off the developer's real
    home directory (and any local ``getpatter telemetry disable`` marker).
    """
    monkeypatch.setattr("getpatter.telemetry.consent.is_ci", lambda: False)
    monkeypatch.setattr("getpatter.telemetry.consent.is_test", lambda: False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_DISABLED", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_DEBUG", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_ENDPOINT", raising=False)
    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", str(tmp_path / "getpatter-state"))


async def _wait_for(collector: _Collector, n: int, timeout: float = 2.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while len(collector.events) < n and loop.time() < deadline:
        await asyncio.sleep(0.01)


# --- enabled path -----------------------------------------------------------


async def test_event_reaches_collector_when_enabled(enabled, collector):
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    assert client.enabled is True
    client.record(
        "feature_used", engine="realtime", provider="openai", carrier="twilio"
    )
    await _wait_for(collector, 1)
    await client.aclose()

    assert len(collector.events) == 1
    event = collector.events[0]
    assert event["event"] == "feature_used"
    assert event["sdk"] == "python"
    assert event["sdk_version"] == "0.6.3"
    assert event["runtime"] == "cpython"
    assert event["schema_version"] == 8
    assert event["engine"] == "realtime"
    assert event["provider"] == "openai"
    assert event["carrier"] == "twilio"
    assert isinstance(event["run_id"], str) and len(event["run_id"]) >= 16


async def test_denylisted_dimensions_are_dropped(enabled, collector):
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    # An allowed dimension plus several radioactive ones that must never ship.
    client.record(
        "feature_used",
        engine="pipeline",
        phone_number="+15551234567",
        transcript="hello there, my SSN is ...",
        api_key="sk-secret",
        caller="+15557654321",
    )
    await _wait_for(collector, 1)
    await client.aclose()

    event = collector.events[0]
    assert event["engine"] == "pipeline"
    for forbidden in ("phone_number", "transcript", "api_key", "caller"):
        assert forbidden not in event
    # No value anywhere in the serialised payload leaks the secret/number.
    blob = json.dumps(event)
    assert "+1555" not in blob
    assert "sk-secret" not in blob


# --- realtime model capture --------------------------------------------------


async def test_agent_records_realtime_model_in_feature_used(
    enabled, collector, monkeypatch
):
    """The Realtime engine's model variant ships in ``feature_used`` (pipeline
    already carries per-layer models; realtime previously sent only the engine
    family). The dedupe key includes the model, so a second agent on a different
    Realtime model records again.
    """
    monkeypatch.setenv("PATTER_TELEMETRY_ENDPOINT", collector.url)
    from getpatter import OpenAIRealtime, Patter

    phone = Patter()
    phone.agent(
        system_prompt="hi",
        engine=OpenAIRealtime(api_key="sk-test", model="gpt-realtime-2"),
    )
    phone.agent(
        system_prompt="hi",
        engine=OpenAIRealtime(api_key="sk-test", model="gpt-realtime-mini"),
    )

    deadline = asyncio.get_event_loop().time() + 2.0
    while (
        sum(1 for e in collector.events if e["event"] == "feature_used") < 2
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.01)

    feature_events = [e for e in collector.events if e["event"] == "feature_used"]
    assert sorted(e.get("llm_model") for e in feature_events) == [
        "openai-gpt-realtime-2",
        "openai-gpt-realtime-mini",
    ]
    assert all(e["engine"] == "realtime" for e in feature_events)


# --- pending-buffer survival (fire-and-forget clients) ----------------------


def test_unreferenced_client_buffered_event_survives_gc_until_atexit(
    enabled, collector
):
    """The CLI records via ``TelemetryClient(...).record(...)`` without keeping a
    reference. The buffered event must survive garbage collection and ship via
    the atexit flush — a client holding undelivered events may not die with its
    last reference (regression: the WeakSet registry let CPython collect it,
    silently losing every ``cli_command`` event).
    """
    from getpatter.telemetry import client as client_mod

    TelemetryClient(sdk_version="0.6.6", endpoint=collector.url).record(
        "cli_command", cli_command="dashboard"
    )
    gc.collect()

    client_mod._atexit_flush_all()

    assert [e["event"] for e in collector.events] == ["cli_command"]


async def test_aclose_awaits_in_flight_flush_started_by_record(enabled, collector):
    """``record`` schedules a flush task that drains the buffer immediately;
    ``aclose`` must await that in-flight delivery — not just its own (now-empty)
    flush — or a graceful shutdown right after recording kills the POST mid-air.
    """
    client = TelemetryClient(sdk_version="0.6.6", endpoint=collector.url)
    client.record("cli_command", cli_command="dashboard")
    await asyncio.sleep(0)  # let the scheduled flush task start and drain
    await client.aclose()

    assert [e["event"] for e in collector.events] == ["cli_command"]


async def test_drain_delivers_final_events_and_keeps_client_usable(enabled, collector):
    """``Patter.disconnect()`` drains via ``drain()``: the final events of a
    short-lived script (``call_completed`` carrying duration/cost/latency) must
    be DELIVERED before the loop closes — a fire-and-forget flush was cancelled
    at loop teardown and the bounded atexit fallback routinely lost them
    (observed live: call_started reached Axiom, call_completed never did).
    Unlike ``aclose``, the client must stay usable for a subsequent serve().
    """
    client = TelemetryClient(sdk_version="0.6.7", endpoint=collector.url)
    client.record("call_completed", outcome="completed", carrier="twilio")
    await client.drain()
    assert [e["event"] for e in collector.events] == ["call_completed"]

    # Still usable after drain (disconnect() must not kill a reusable instance).
    client.record("cli_command", cli_command="dashboard")
    await client.drain()
    assert [e["event"] for e in collector.events] == ["call_completed", "cli_command"]
    await client.aclose()


class _GatedCollector(_Collector):
    """A real collector that holds its FIRST response until released.

    Lets a test pin the "event recorded while a flush POST is in flight"
    window deterministically: the first POST blocks server-side, the test
    records a second event, then releases the gate.
    """

    def __init__(self) -> None:
        super().__init__()
        self.first_request_started = threading.Event()
        self._gate = threading.Event()
        self._held_one = False

    def release(self) -> None:
        self._gate.set()

    def start(self) -> None:
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (stdlib name)
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length)
                hold = not collector._held_one
                collector._held_one = True
                collector.first_request_started.set()
                if hold:
                    collector._gate.wait(timeout=5.0)
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


@pytest.fixture
def gated_collector():
    c = _GatedCollector()
    c.start()
    yield c
    c.release()  # never leave the handler thread blocked
    c.stop()


async def test_event_recorded_during_in_flight_flush_is_chained(
    enabled, gated_collector
):
    """An event recorded *while* a flush POST is in flight must not strand in
    the buffer: the completing flush chains another one (regression: ``record``
    saw the live flush task and skipped scheduling, so constructor-time events
    shadowed agent-time events until ``aclose``/process exit).

    Pinned WITHOUT calling ``aclose`` — delivery must happen on its own.
    """
    client = TelemetryClient(sdk_version="0.6.7", endpoint=gated_collector.url)
    client.record("cli_command", cli_command="dashboard")

    # Wait until the first POST is genuinely in flight (held by the server).
    await asyncio.to_thread(gated_collector.first_request_started.wait, 5.0)
    client.record("first_run")  # lands in the buffer; no flush is scheduled
    gated_collector.release()

    await _wait_for(gated_collector, 2)
    assert sorted(e["event"] for e in gated_collector.events) == [
        "cli_command",
        "first_run",
    ]
    # Two separate POSTs: the second was chained by the completing first flush.
    assert len(gated_collector.requests) == 2
    await client.aclose()


# --- disabled paths: zero egress -------------------------------------------


async def test_disabled_by_do_not_track(enabled, collector, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    assert client.enabled is False
    client.record("feature_used", engine="realtime")
    await asyncio.sleep(0.1)
    await client.aclose()
    assert collector.events == []


async def test_disabled_by_kill_switch(enabled, collector, monkeypatch):
    monkeypatch.setenv("PATTER_TELEMETRY_DISABLED", "1")
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    assert client.enabled is False
    client.record("feature_used", engine="realtime")
    await asyncio.sleep(0.1)
    await client.aclose()
    assert collector.events == []


async def test_disabled_by_constructor_flag(enabled, collector):
    client = TelemetryClient(sdk_version="0.6.3", flag=False, endpoint=collector.url)
    assert client.enabled is False
    client.record("feature_used", engine="realtime")
    await asyncio.sleep(0.1)
    await client.aclose()
    assert collector.events == []


async def test_disabled_in_ci(collector, monkeypatch):
    # Don't use the `enabled` fixture: exercise the real CI detection.
    monkeypatch.setattr("getpatter.telemetry.consent.is_test", lambda: False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setenv("CI", "true")
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    assert client.enabled is False
    client.record("feature_used")
    await asyncio.sleep(0.1)
    await client.aclose()
    assert collector.events == []


# --- fail-safety ------------------------------------------------------------


async def test_offline_collector_is_silent(enabled):
    # Point at a closed port — record + flush must not raise; behaviour identical.
    client = TelemetryClient(
        sdk_version="0.6.3", endpoint="http://127.0.0.1:1/v1/ingest"
    )
    assert client.enabled is True
    client.record("feature_used", engine="realtime")
    await asyncio.sleep(0.1)
    await client.aclose()  # must complete without raising


async def test_record_never_raises_on_bad_event(enabled, collector):
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    client.record("not_a_real_event")  # unknown event name — swallowed
    await asyncio.sleep(0.1)
    await client.aclose()
    assert collector.events == []


async def test_flush_sync_sends_buffered_events(enabled, collector):
    # Exercises the synchronous atexit path (httpx.Client, not AsyncClient).
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    client.record("sdk_initialized", engine="pipeline")
    # Drain synchronously before the scheduled async flush runs.
    client._flush_sync()
    await _wait_for(collector, 1)
    await client.aclose()

    assert len(collector.events) == 1
    assert collector.events[0]["event"] == "sdk_initialized"
    assert collector.events[0]["engine"] == "pipeline"


async def test_debug_prints_without_sending(enabled, collector, monkeypatch, capsys):
    monkeypatch.setenv("PATTER_TELEMETRY_DEBUG", "1")
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    client.record("sdk_initialized", engine="convai")
    await asyncio.sleep(0.1)
    await client.aclose()

    assert collector.events == []  # nothing sent
    err = capsys.readouterr().err
    assert "[patter telemetry]" in err
    assert "sdk_initialized" in err


# --- unit -------------------------------------------------------------------


def test_build_event_has_required_anonymous_fields():
    event = build_event(
        "sdk_initialized", sdk_version="1.2.3", dimensions={"engine": "realtime"}
    )
    assert event["event"] == "sdk_initialized"
    assert event["sdk"] == "python"
    assert event["sdk_version"] == "1.2.3"
    assert event["runtime"] == "cpython"
    assert event["os"] in {"linux", "darwin", "windows", "unknown"}
    assert event["arch"] in {"x86_64", "arm64"} or isinstance(event["arch"], str)
    assert "." in event["runtime_version"]  # major.minor, no patch
    assert isinstance(event["ci"], bool)
    assert event["engine"] == "realtime"


def test_build_event_rejects_unknown_event():
    with pytest.raises(ValueError):
        build_event("definitely_not_an_event", sdk_version="1.0.0")


def test_offlist_dimension_value_coerced_to_other():
    # A raw custom value for an enum dimension must never reach the wire — the
    # value allowlist coerces it to "other".
    event = build_event(
        "feature_used",
        sdk_version="1.0.0",
        dimensions={"provider": "AcmeSecretVendorLLM", "engine": "realtime"},
    )
    assert event["provider"] == "other"  # coerced
    assert event["engine"] == "realtime"  # on-list, preserved
    assert "AcmeSecretVendorLLM" not in json.dumps(event)


def test_build_event_keeps_known_values():
    event = build_event(
        "feature_used",
        sdk_version="1.0.0",
        dimensions={"provider": "deepgram", "carrier": "telnyx"},
    )
    assert event["provider"] == "deepgram"
    assert event["carrier"] == "telnyx"


def test_engine_family_parity_logic():
    # _telemetry_engine_family must mirror the TS telemetryEngineFamily exactly,
    # including the provider-based branches (parity with the TS public option).
    from getpatter.client import _telemetry_engine_family as fam

    assert fam(None, None, None, None) == "realtime"
    assert fam(None, "pipeline", None, None) == "pipeline"
    assert fam(None, "elevenlabs_convai", None, None) == "convai"
    assert fam(None, "openai_realtime", None, None) == "realtime"
    assert fam(None, None, object(), None) == "pipeline"  # stt set -> pipeline

    class _ConvAIEngine:
        pass

    assert fam(_ConvAIEngine(), None, None, None) == "convai"

    class _SomeRealtimeEngine:
        pass

    assert fam(_SomeRealtimeEngine(), None, None, None) == "realtime"


def test_telemetry_buckets():
    from getpatter.client import (
        _telemetry_bucket_custom_tools as bt,
        _telemetry_bucket_mcp as bm,
    )

    assert [bt(n) for n in (0, 1, 2, 3, 4, 6, 7, 12, 13, 100)] == [
        "0",
        "1",
        "2_3",
        "2_3",
        "4_6",
        "4_6",
        "7_12",
        "7_12",
        "13_plus",
        "13_plus",
    ]
    assert [bm(n) for n in (0, 1, 2, 3, 4)] == ["0", "1", "2_3", "2_3", "4_plus"]


def test_telemetry_provider_family():
    from getpatter.client import _telemetry_provider_family as pf

    assert pf("realtime") == "openai"
    assert pf("convai") == "elevenlabs"
    assert pf("pipeline") == "other"


def test_telemetry_integration_detects_openclaw_without_leaking():
    from getpatter.client import _telemetry_integration
    from getpatter.models import ConsultConfig

    consult = ConsultConfig.openclaw(agent="receptionist")
    integration, kind, mcp = _telemetry_integration(consult, None)
    assert integration == "openclaw"
    assert kind == "consult"
    assert mcp == "0"


def test_telemetry_integration_custom_consult_collapses_to_other():
    from getpatter.client import _telemetry_integration
    from getpatter.models import ConsultConfig

    # A custom consult endpoint must never leak its host — collapses to "other".
    consult = ConsultConfig(url="https://my-secret-brand-orchestrator.example.com/x")
    integration, kind, mcp = _telemetry_integration(consult, None)
    assert integration == "other"
    assert kind == "consult"


def test_telemetry_integration_mcp_bucketed():
    from getpatter.client import _telemetry_integration

    integration, kind, mcp = _telemetry_integration(None, [object(), object()])
    assert integration == "mcp"
    assert kind == "mcp"
    assert mcp == "2_3"


async def test_call_completed_from_real_metrics(enabled, collector):
    # Build a REAL CallMetrics and run the real record_call_completed helper +
    # real client → real collector. Only the network boundary is local.
    from getpatter.models import CallMetrics, CostBreakdown, LatencyBreakdown
    from getpatter.telemetry.call_metrics import record_call_completed

    metrics = CallMetrics(
        call_id="CAtest",
        duration_seconds=42.0,  # -> 10s_1m
        turns=(),
        cost=CostBreakdown(),
        latency_avg=LatencyBreakdown(),
        latency_p95=LatencyBreakdown(agent_response_ms=2500.0),  # -> 2s_5s
        provider_mode="pipeline",
        llm_provider="openai",
        telephony_provider="twilio",
    )
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    record_call_completed(client, outcome="completed", metrics=metrics)
    await _wait_for(collector, 1)
    await client.aclose()

    event = collector.events[0]
    assert event["event"] == "call_completed"
    assert event["outcome"] == "completed"
    assert event["engine"] == "pipeline"
    assert event["provider"] == "openai"
    assert event["carrier"] == "twilio"
    # Raw values now (not buckets): whole seconds / whole milliseconds.
    assert event["duration_seconds"] == 42
    assert event["latency_ms"] == 2500
    # No cost field anywhere.
    assert "cost" not in event
    assert "$" not in json.dumps(event)


async def test_call_completed_failed_outcome(enabled, collector):
    # A non-connected failure: only outcome + carrier, no latency/duration.
    from getpatter.telemetry.call_metrics import record_call_completed

    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    record_call_completed(client, outcome="no_answer", carrier="twilio")
    await _wait_for(collector, 1)
    await client.aclose()

    event = collector.events[0]
    assert event["event"] == "call_completed"
    assert event["outcome"] == "no_answer"
    assert event["carrier"] == "twilio"
    assert "latency_ms" not in event
    assert "duration_seconds" not in event


def _telemetry_server(collector):
    """A real EmbeddedServer with an enabled telemetry client → collector."""
    from getpatter.local_config import LocalConfig
    from getpatter.models import Agent
    from getpatter.server import EmbeddedServer

    config = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="AC" + "a" * 32,
        twilio_token="test-token",
        openai_key="sk-test",
        phone_number="+15550001234",
        webhook_url="abc.ngrok.io",
    )
    server = EmbeddedServer(config=config, agent=Agent(system_prompt="Test"))
    server._telemetry = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    return server


async def test_server_wiring_emits_call_completed(enabled, collector):
    # Verify the REAL server call-end wrapper emits call_completed — not just the
    # helper in isolation.
    from getpatter.models import CallMetrics, CostBreakdown, LatencyBreakdown

    server = _telemetry_server(collector)
    _start, on_call_end, _metrics, _transcript_line, _transcript = (
        server._wrap_callbacks()
    )
    metrics = CallMetrics(
        call_id="CAx",
        duration_seconds=73.0,
        turns=(),
        cost=CostBreakdown(),
        latency_avg=LatencyBreakdown(),
        latency_p95=LatencyBreakdown(agent_response_ms=1800.0),
        provider_mode="pipeline",
        llm_provider="openai",
        telephony_provider="twilio",
    )
    await on_call_end({"call_id": "CAx", "metrics": metrics})
    await _wait_for(collector, 1)
    await server._telemetry.aclose()

    event = next(x for x in collector.events if x["event"] == "call_completed")
    assert event["outcome"] == "completed"
    assert event["duration_seconds"] == 73
    assert event["latency_ms"] == 1800


async def test_server_wiring_emits_failed_outcome(enabled, collector):
    # Verify the REAL terminal hook emits call_completed for a non-connected
    # failure (no_answer), gated so connected calls don't double-emit.
    server = _telemetry_server(collector)
    server._resolve_completion("CAy", outcome="no_answer", status="no-answer")
    await _wait_for(collector, 1)
    await server._telemetry.aclose()

    event = next(x for x in collector.events if x["event"] == "call_completed")
    assert event["outcome"] == "no_answer"
    assert event["carrier"] == "twilio"


def test_call_completed_none_guarded():
    from getpatter.telemetry.call_metrics import record_call_completed

    # Must never raise on missing telemetry / metrics.
    record_call_completed(None, outcome="completed")
    record_call_completed(None, outcome="completed", metrics=object())


def test_value_allowlist_coerces_new_dimensions():
    event = build_event(
        "agent_configured",
        sdk_version="1.0.0",
        dimensions={
            "integration": "SomeCustomerBrand",  # off-list -> other
            "integration_kind": "consult",
            "custom_tool_count_bucket": "2_3",
            "builtin_tool_count": 1,  # numeric passthrough
        },
    )
    assert event["integration"] == "other"
    assert event["integration_kind"] == "consult"
    assert event["custom_tool_count_bucket"] == "2_3"
    assert event["builtin_tool_count"] == 1
    assert "SomeCustomerBrand" not in json.dumps(event)


def test_metrics_record_error_maps_codes():
    from getpatter.exceptions import RateLimitError
    from getpatter.services.metrics import CallMetricsAccumulator

    acc = CallMetricsAccumulator("CAx", "pipeline", "twilio")
    acc.record_error(RateLimitError("provider 429"))
    assert acc._error_code == "rate_limit"  # ErrorCode lowercased
    acc.record_error(TimeoutError())
    assert acc._error_code == "timeout"
    acc.record_error(ValueError("boom"))  # non-Patter, non-timeout
    assert acc._error_code == "other"
    # The message text is never stored — only the code.
    assert "boom" not in acc._error_code and "429" not in acc._error_code


async def test_call_completed_error_code_flips_outcome(enabled, collector):
    from getpatter.models import CallMetrics, CostBreakdown, LatencyBreakdown
    from getpatter.telemetry.call_metrics import record_call_completed

    metrics = CallMetrics(
        call_id="CAx",
        duration_seconds=12.0,
        turns=(),
        cost=CostBreakdown(),
        latency_avg=LatencyBreakdown(),
        latency_p95=LatencyBreakdown(agent_response_ms=900.0),
        provider_mode="pipeline",
        llm_provider="openai",
        telephony_provider="twilio",
        error_code="rate_limit",
    )
    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    # _on_call_end always passes outcome="completed"; an error_code flips it.
    record_call_completed(client, outcome="completed", metrics=metrics)
    await _wait_for(collector, 1)
    await client.aclose()

    event = collector.events[0]
    assert event["outcome"] == "error"
    assert event["error_code"] == "rate_limit"
    assert event["latency_ms"] == 900


async def test_register_hermes_emits_integration(enabled, collector, monkeypatch):
    # Real PatterTool.register_hermes against a fake Hermes registry → the wrapped
    # Patter's telemetry emits feature_used{integration=hermes}.
    from getpatter.integrations.patter_tool import PatterTool

    class _FakePatter:
        def __init__(self, tel):
            self._telemetry = tel

    class _FakeRegistry:
        def register(self, **kw):
            pass

    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    tool = PatterTool(phone=_FakePatter(client), agent={"system_prompt": "x"})
    tool.register_hermes(_FakeRegistry())
    await _wait_for(collector, 1)
    await client.aclose()

    event = next(e for e in collector.events if e["event"] == "agent_configured")
    assert event["integration"] == "hermes"


# --- Stack capture: carrier + STT + TTS + LLM vendor/model (schema v3) --------


class _FakeProvider:
    """Minimal stand-in for a provider adapter exposing provider_key + model."""

    def __init__(self, provider_key: str, model: str) -> None:
        self.provider_key = provider_key
        self.model = model


def test_model_token_known_models_normalize():
    assert stack.model_token("deepgram", "nova-3") == "deepgram-nova-3"
    assert stack.model_token("openai", "gpt-4o") == "openai-gpt-4o"
    # underscores → hyphens; trailing release date stripped so date variants merge
    assert (
        stack.model_token("elevenlabs", "eleven_flash_v2_5")
        == "elevenlabs-eleven-flash-v2-5"
    )
    assert (
        stack.model_token("anthropic", "claude-haiku-4-5-20251001")
        == "anthropic-claude-haiku-4-5"
    )


def test_model_token_pii_risky_coerced_to_vendor_other():
    # Fine-tuned id (embeds an org name), self-hosted path, custom name, overlong, empty.
    assert (
        stack.model_token("openai", "ft:gpt-4o:acme-corp:custom:xZ9") == "openai-other"
    )
    assert stack.model_token("openai", "openclaw/agent-x") == "openai-other"
    assert stack.model_token("openai", "my custom model") == "openai-other"
    assert stack.model_token("openai", "x" * 50) == "openai-other"
    assert stack.model_token("openai", "") == "openai-other"


def test_vendor_of_aliases_and_unknown():
    assert stack.vendor_of("cartesia_tts") == "cartesia"
    assert stack.vendor_of("openai_tts") == "openai"
    assert stack.vendor_of("elevenlabs_ws") == "elevenlabs"
    assert stack.vendor_of("deepgram") == "deepgram"
    assert stack.vendor_of("totally-unknown") == "other"
    assert stack.vendor_of(None) == "other"


def test_stack_dimensions_full_pipeline():
    dims = stack.stack_dimensions(
        _FakeProvider("deepgram", "nova-3"),
        _FakeProvider("elevenlabs", "eleven_turbo_v2_5"),
        _FakeProvider("anthropic", "claude-opus-4-8"),
    )
    assert dims == {
        "stt_provider": "deepgram",
        "stt_model": "deepgram-nova-3",
        "tts_provider": "elevenlabs",
        "tts_model": "elevenlabs-eleven-turbo-v2-5",
        "llm_provider": "anthropic",
        "llm_model": "anthropic-claude-opus-4-8",
    }


def test_stack_dimensions_omits_absent_layers():
    dims = stack.stack_dimensions(None, None, _FakeProvider("openai", "gpt-4o"))
    assert dims == {"llm_provider": "openai", "llm_model": "openai-gpt-4o"}


def test_build_event_carries_stack_and_drops_forged_model():
    ev = build_event(
        "feature_used",
        sdk_version="0.6.3",
        dimensions={
            "engine": "pipeline",
            "stt_provider": "deepgram",
            "stt_model": "deepgram-nova-3",
            "llm_provider": "anthropic",
            "llm_model": "anthropic-claude-opus-4-8",
        },
    )
    assert ev["stt_provider"] == "deepgram"
    assert ev["llm_model"] == "anthropic-claude-opus-4-8"
    # An off-allowlist vendor is coerced to "other".
    ev2 = build_event(
        "feature_used", sdk_version="0.6.3", dimensions={"stt_provider": "nsa"}
    )
    assert ev2["stt_provider"] == "other"
    # A forged model token that fails the safe-shape check is dropped entirely.
    ev3 = build_event(
        "feature_used", sdk_version="0.6.3", dimensions={"llm_model": "BAD/with:stuff"}
    )
    assert "llm_model" not in ev3


# --- Persistent install id + per-call cost (schema v3) ------------------------


def test_install_id_is_stable_anonymous_and_persisted(monkeypatch, tmp_path):
    from getpatter.telemetry import install_id as iid

    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", str(tmp_path))
    iid._install_id = None  # reset the process cache
    first = iid.install_id()
    assert len(first) == 32 and all(c in "0123456789abcdef" for c in first)
    assert iid.install_id() == first  # stable within the process
    # Persisted to disk: a cold cache reads the same id back.
    iid._install_id = None
    assert iid.install_id() == first


def test_build_event_includes_install_id_and_keeps_cost_as_float(monkeypatch, tmp_path):
    from getpatter.telemetry import install_id as iid

    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", str(tmp_path))
    iid._install_id = None
    ev = build_event(
        "call_completed",
        sdk_version="0.6.3",
        dimensions={"outcome": "completed", "cost_usd": 0.0123},
    )
    assert len(str(ev["install_id"])) == 32
    assert ev["cost_usd"] == 0.0123  # float preserved (cost is not rounded to int)


async def test_call_completed_carries_cost(enabled, collector):
    from getpatter.telemetry.call_metrics import record_call_completed

    class _Cost:
        total = 0.0456

    class _Metrics:
        provider_mode = "pipeline"
        telephony_provider = "twilio"
        duration_seconds = 42.0
        latency_p95 = None
        error_code = ""
        cost = _Cost()
        llm_provider = "anthropic"

    client = TelemetryClient(sdk_version="0.6.3", endpoint=collector.url)
    record_call_completed(client, outcome="completed", metrics=_Metrics())
    await _wait_for(collector, 1)
    await client.aclose()

    event = next(e for e in collector.events if e["event"] == "call_completed")
    assert event["cost_usd"] == 0.0456
    assert event["duration_seconds"] == 42


# --- Deploy-shape + feature-adoption + upgrade funnel (schema v4) -------------


def test_environment_probes_return_allowlisted_values():
    from getpatter.telemetry import environment as env
    from getpatter.telemetry.events import DIMENSION_VALUES

    assert env.invoked_by_agent() in DIMENSION_VALUES["invoked_by_agent"]
    assert env.serverless() in DIMENSION_VALUES["serverless"]
    assert env.cloud() in DIMENSION_VALUES["cloud"]
    assert env.package_manager() in DIMENSION_VALUES["package_manager"]
    assert isinstance(env.in_container(), bool)


def test_version_funnel_and_days_bucket(monkeypatch, tmp_path):
    from getpatter.telemetry import install_id as iid

    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", str(tmp_path))
    iid._install_id = None
    iid.install_id()  # creates the install-id file (used for the days bucket)
    assert iid.previous_version("0.6.3") == ""  # first run → no prior version
    assert iid.previous_version("0.6.4") == "0.6.3"  # now sees the prior
    assert iid.days_since_install_bucket() in {"0", "1_7", "8_30", "30_plus"}


def test_build_event_v4_bool_enum_and_version_dims():
    ev = build_event(
        "agent_configured",
        sdk_version="0.6.4",
        dimensions={
            "noise_reduction": "far_field",
            "turn_detection": "custom",
            "preambles_used": True,
            "per_tool_timeouts_set": False,
            "llm_fallback_configured": True,
        },
    )
    assert ev["noise_reduction"] == "far_field"
    assert ev["preambles_used"] is True and ev["per_tool_timeouts_set"] is False
    # An off-list enum is coerced; a non-bool on a bool dim is dropped; a version passes.
    ev2 = build_event(
        "sdk_initialized",
        sdk_version="0.6.4",
        dimensions={
            "cloud": "mars",
            "container": "nope",
            "previous_sdk_version": "0.6.3",
        },
    )
    assert ev2["cloud"] == "other"
    assert "container" not in ev2
    assert ev2["previous_sdk_version"] == "0.6.3"


# --- CLI usage + first-run + call funnel + persisted opt-out (schema v6) ------


def test_schema_version_is_8():
    from getpatter.telemetry import SCHEMA_VERSION

    assert SCHEMA_VERSION == 8
    assert build_event("first_run", sdk_version="0.6.5")["schema_version"] == 8


def test_call_uid_kept_only_when_hex32():
    # A well-formed random correlation id (32 lowercase hex) survives; every
    # off-shape value is DROPPED (key absent), never coerced to "other".
    good = "a" * 32
    ev = build_event("call_started", sdk_version="0.6.5", dimensions={"call_uid": good})
    assert ev["call_uid"] == good

    for bad in ("not-a-uid", "A" * 32, "a" * 31, "a" * 33, 123, True):
        ev_bad = build_event(
            "call_completed",
            sdk_version="0.6.5",
            dimensions={"outcome": "completed", "call_uid": bad},
        )
        assert "call_uid" not in ev_bad
        # Never coerced — the off-shape value can't reach the wire at all.
        assert bad == 123 or str(bad) not in json.dumps(ev_bad)


def test_server_telemetry_call_uid_pairing_semantics():
    # Exercise the REAL EmbeddedServer helper. Constructing the server fully is
    # cheap enough, but the method only needs the map — use a bare instance so
    # the test is independent of the heavy __init__ wiring.
    from getpatter.server import EmbeddedServer

    server = object.__new__(EmbeddedServer)
    server._telemetry_call_uids = {}

    # Same call_id twice → same uid (so call_started/call_completed pair).
    uid1 = server._telemetry_call_uid("CAabc")
    uid2 = server._telemetry_call_uid("CAabc")
    assert uid1 == uid2
    assert len(uid1) == 32 and all(c in "0123456789abcdef" for c in uid1)

    # pop=True removes it; a fresh lookup mints a NEW uid.
    popped = server._telemetry_call_uid("CAabc", pop=True)
    assert popped == uid1
    assert "CAabc" not in server._telemetry_call_uids
    uid3 = server._telemetry_call_uid("CAabc")
    assert uid3 != uid1

    # pop=True on a call_id that was never seen still returns a fresh uid (a
    # no_answer call has only its lone terminal event — it still gets a uid).
    lone = server._telemetry_call_uid("CAnever", pop=True)
    assert lone is not None and len(lone) == 32
    assert "CAnever" not in server._telemetry_call_uids

    # Falsy call_id → None (no uid for an unknown call).
    assert server._telemetry_call_uid("") is None
    assert server._telemetry_call_uid(None) is None


def test_server_telemetry_call_uid_fifo_cap_at_512():
    # The map is bounded: inserting a 513th distinct call_id evicts the oldest.
    from getpatter.server import EmbeddedServer

    server = object.__new__(EmbeddedServer)
    server._telemetry_call_uids = {}

    for i in range(512):
        server._telemetry_call_uid(f"call-{i}")
    assert len(server._telemetry_call_uids) == 512
    assert "call-0" in server._telemetry_call_uids

    # The 513th insert evicts the first-inserted entry (insertion-ordered FIFO).
    server._telemetry_call_uid("call-512")
    assert len(server._telemetry_call_uids) == 512
    assert "call-0" not in server._telemetry_call_uids
    assert "call-512" in server._telemetry_call_uids


def test_build_event_v6_new_events_and_dims():
    cli = build_event(
        "cli_command", sdk_version="0.6.5", dimensions={"cli_command": "dashboard"}
    )
    assert cli["event"] == "cli_command" and cli["cli_command"] == "dashboard"
    # The wizard commands are first-class enum values (schema v6) — they must
    # pass through uncoerced or their usage is invisible in the data.
    for wizard in ("hermes", "openclaw"):
        assert (
            build_event(
                "cli_command", sdk_version="0.6.8", dimensions={"cli_command": wizard}
            )["cli_command"]
            == wizard
        )
    # An unknown command coerces to "other"; it can never reach the wire raw.
    assert (
        build_event(
            "cli_command", sdk_version="0.6.5", dimensions={"cli_command": "rm -rf /"}
        )["cli_command"]
        == "other"
    )
    started = build_event(
        "call_started",
        sdk_version="0.6.5",
        dimensions={
            "engine": "pipeline",
            "provider": "other",
            "carrier": "telnyx",
            "direction": "outbound",
        },
    )
    assert started["direction"] == "outbound"
    # Off-list direction is coerced to "other".
    assert (
        build_event(
            "call_completed",
            sdk_version="0.6.5",
            dimensions={"outcome": "completed", "direction": "sideways"},
        )["direction"]
        == "other"
    )


async def test_call_started_event_reaches_collector(enabled, collector):
    from getpatter.telemetry.call_metrics import record_call_started

    client = TelemetryClient(sdk_version="0.6.5", endpoint=collector.url)
    record_call_started(
        client,
        provider_mode="openai_realtime",
        telephony_provider="Twilio",
        direction="inbound",
    )
    await _wait_for(collector, 1)
    await client.aclose()

    event = next(e for e in collector.events if e["event"] == "call_started")
    assert event["engine"] == "realtime"
    assert event["provider"] == "openai"
    assert event["carrier"] == "twilio"
    assert event["direction"] == "inbound"


def test_call_started_omits_unknown_direction():
    """An unknown/absent direction is omitted, never guessed."""
    from getpatter.telemetry import call_metrics as cm

    recorded: list[tuple[str, dict]] = []

    class _Sink:
        def record(self, name, **dims):
            recorded.append((name, dims))

    cm.record_call_started(_Sink(), provider_mode="pipeline", direction=None)
    assert recorded[0][0] == "call_started"
    assert "direction" not in recorded[0][1]


def test_direction_on_call_completed():
    from getpatter.telemetry import call_metrics as cm

    recorded: list[dict] = []

    class _Sink:
        def record(self, name, **dims):
            recorded.append(dims)

    cm.record_call_completed(
        _Sink(), outcome="failed", carrier="telnyx", direction="outbound"
    )
    assert recorded[0]["direction"] == "outbound"


def test_is_first_run_is_idempotent(monkeypatch, tmp_path):
    from getpatter.telemetry import install_id as iid

    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", str(tmp_path))
    assert iid.is_first_run() is True  # first call marks it
    assert iid.is_first_run() is False  # every later call


# --- state-dir hardening + visible disclosure + relay-cap chunking (0.6.8) ----


def test_xdg_state_home_uses_getpatter_subdir(monkeypatch, tmp_path):
    """XDG spec: state files live in an app subdirectory, never the shared root."""
    from getpatter.telemetry import install_id as iid

    monkeypatch.delenv("PATTER_TELEMETRY_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    iid._install_id = None
    first = iid.install_id()
    assert (tmp_path / "getpatter" / "install-id").read_text().strip() == first
    assert not (tmp_path / "install-id").exists()


def test_xdg_legacy_install_id_migrates(monkeypatch, tmp_path):
    """A pre-0.6.8 id in the bare XDG root is kept (no double-counted install)
    and migrated into the subdirectory."""
    from getpatter.telemetry import install_id as iid

    monkeypatch.delenv("PATTER_TELEMETRY_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    legacy_id = "ab" * 16
    (tmp_path / "install-id").write_text(legacy_id)
    iid._install_id = None
    assert iid.install_id() == legacy_id
    assert (tmp_path / "getpatter" / "install-id").read_text().strip() == legacy_id


def test_xdg_legacy_opt_out_still_honored(monkeypatch, tmp_path):
    """An opt-out persisted by a pre-0.6.8 build (bare XDG root) must keep
    disabling telemetry after the state-dir move — consent survives upgrades."""
    from getpatter.telemetry import install_id as iid
    from getpatter.telemetry.consent import is_enabled

    monkeypatch.setattr("getpatter.telemetry.consent.is_ci", lambda: False)
    monkeypatch.setattr("getpatter.telemetry.consent.is_test", lambda: False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_DISABLED", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    (tmp_path / "telemetry-disabled").write_text("1")  # pre-0.6.8 marker
    assert iid.is_opted_out() is True
    assert is_enabled() is False
    iid.set_opt_out(False)  # re-enable must clear the legacy marker too
    assert iid.is_opted_out() is False
    assert is_enabled() is True


def test_xdg_legacy_first_run_not_reemitted(monkeypatch, tmp_path):
    from getpatter.telemetry import install_id as iid

    monkeypatch.delenv("PATTER_TELEMETRY_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "first-run").write_text("1")  # pre-0.6.8 marker
    assert iid.is_first_run() is False


def test_first_use_notice_is_visible_on_stderr(enabled, collector, monkeypatch, capsys):
    """The opt-out disclosure must be visible by default. The SDK attaches no
    logging handler, so a logger.info-only notice is invisible (lastResort shows
    WARNING+) — it goes straight to stderr, once per process."""
    from getpatter.telemetry import client as client_mod

    monkeypatch.setattr(client_mod, "_NOTICE_SHOWN", False)
    TelemetryClient(sdk_version="0.6.7", endpoint=collector.url)
    err = capsys.readouterr().err
    assert "Anonymous usage telemetry is on" in err
    assert "docs.getpatter.com/telemetry" in err
    # Once per process: a second client does not repeat it.
    TelemetryClient(sdk_version="0.6.7", endpoint=collector.url)
    assert "Anonymous usage telemetry" not in capsys.readouterr().err


async def test_flush_chunks_large_buffers_to_relay_cap(enabled, collector):
    """The relay rejects >64 events per request — a large flush must ship as
    multiple POSTs, never one oversized batch (which the relay drops/truncates)."""
    client = TelemetryClient(sdk_version="0.6.7", endpoint=collector.url)
    for _ in range(100):
        client.record("cli_command", cli_command="dashboard")
    await _wait_for(collector, 100)
    await client.aclose()

    assert len(collector.events) == 100  # nothing truncated
    batches = [len(b) for b in collector.requests if isinstance(b, list)]
    assert batches and max(batches) <= 64


def test_flush_sync_chunks_large_buffers(enabled, collector):
    """The synchronous atexit path applies the same relay-cap chunking."""
    client = TelemetryClient(sdk_version="0.6.7", endpoint=collector.url)
    for _ in range(70):
        client.record("cli_command", cli_command="eval")
    client._flush_sync()

    assert len(collector.events) == 70
    batches = [len(b) for b in collector.requests if isinstance(b, list)]
    assert batches and max(batches) <= 64


def test_persisted_opt_out_disables_consent(monkeypatch, tmp_path):
    from getpatter.telemetry import install_id as iid
    from getpatter.telemetry.consent import is_enabled

    monkeypatch.setattr("getpatter.telemetry.consent.is_ci", lambda: False)
    monkeypatch.setattr("getpatter.telemetry.consent.is_test", lambda: False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("PATTER_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", str(tmp_path))

    assert is_enabled() is True
    iid.set_opt_out(True)
    assert iid.is_opted_out() is True
    assert is_enabled() is False  # persisted marker wins over the default-ON
    iid.set_opt_out(False)
    assert iid.is_opted_out() is False
    assert is_enabled() is True

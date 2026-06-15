"""Authentic tests for the per-stage / error / time-to-first-call call dims.

Covers telemetry workstream F2 (per-stage latency), F3 (error_layer +
disconnect_reason), and F4 (time_to_first_call_bucket) on the shared
``call_started`` / ``call_completed`` emit path.

These exercise the REAL builders in ``getpatter.telemetry.call_metrics`` against
REAL ``CallMetrics`` / ``LatencyBreakdown`` / ``CostBreakdown`` model objects.
Only the outermost boundary — the telemetry client's ``record`` sink — is a
local capture double (no HTTP, no env), so every dimension assertion checks the
dict the builder actually produced. Nothing here mocks the code under test.
"""

from __future__ import annotations

import os
from typing import Any

from getpatter.models import (
    CallMetrics,
    CostBreakdown,
    LatencyBreakdown,
)
from getpatter.telemetry import call_metrics
from getpatter.telemetry import install_id as install_id_mod
from getpatter.telemetry.call_metrics import (
    record_call_completed,
    record_call_started,
)


class CaptureTelemetry:
    """Capture double for the telemetry sink — the only mocked surface.

    Mirrors ``TelemetryClient.record(name, **dimensions)`` and stores each call
    so a test can assert the real, fully-built dimension dict.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, name: str, **dimensions: Any) -> None:
        self.records.append((name, dict(dimensions)))


def _metrics(
    *,
    provider_mode: str,
    latency: LatencyBreakdown,
    error_code: str = "",
    duration_seconds: float = 12.0,
    telephony_provider: str = "twilio",
    llm_provider: str = "",
) -> CallMetrics:
    """Build a real CallMetrics object with a known p95 latency breakdown."""
    return CallMetrics(
        call_id="ca-test",
        duration_seconds=duration_seconds,
        turns=(),
        cost=CostBreakdown(total=0.06),
        latency_avg=latency,
        latency_p95=latency,
        provider_mode=provider_mode,
        telephony_provider=telephony_provider,
        llm_provider=llm_provider,
        error_code=error_code,
    )


# --------------------------------------------------------------------------- #
# F2 — per-stage latency                                                       #
# --------------------------------------------------------------------------- #


def test_pipeline_call_emits_four_per_stage_latencies_as_whole_ints() -> None:
    cap = CaptureTelemetry()
    latency = LatencyBreakdown(
        stt_ms=120.4,
        tts_ms=88.9,
        llm_ttft_ms=305.5,
        endpoint_ms=42.2,
        agent_response_ms=436.0,
    )
    record_call_completed(
        cap,
        outcome="completed",
        metrics=_metrics(
            provider_mode="pipeline", latency=latency, llm_provider="cerebras"
        ),
    )

    assert len(cap.records) == 1
    name, dims = cap.records[0]
    assert name == "call_completed"
    # Rounded to whole ints, never the float.
    assert dims["stt_latency_ms"] == 120
    assert dims["tts_first_byte_ms"] == 89
    assert dims["llm_ttft_ms"] == 306
    assert dims["eou_latency_ms"] == 42
    assert all(
        isinstance(dims[k], int)
        for k in (
            "stt_latency_ms",
            "tts_first_byte_ms",
            "llm_ttft_ms",
            "eou_latency_ms",
        )
    )


def test_realtime_call_omits_stt_and_tts_stage_dims() -> None:
    """Realtime has no separate STT/TTS span — stt_ms/tts_ms stay 0, so those
    dims are omitted entirely (never sent as a misleading 0)."""
    cap = CaptureTelemetry()
    # Realtime: stt_ms / tts_ms are 0 (no separate stage); only the fused
    # endpoint + ttft are meaningful.
    latency = LatencyBreakdown(
        stt_ms=0.0,
        tts_ms=0.0,
        llm_ttft_ms=210.0,
        endpoint_ms=30.0,
    )
    record_call_completed(
        cap,
        outcome="completed",
        metrics=_metrics(provider_mode="openai_realtime", latency=latency),
    )

    _, dims = cap.records[0]
    assert "stt_latency_ms" not in dims
    assert "tts_first_byte_ms" not in dims
    assert dims["llm_ttft_ms"] == 210
    assert dims["eou_latency_ms"] == 30


def test_absent_optional_stage_is_omitted_not_zero() -> None:
    """A ``None`` optional stage (llm_ttft/endpoint) is omitted, not coerced."""
    cap = CaptureTelemetry()
    latency = LatencyBreakdown(stt_ms=100.0, tts_ms=50.0)  # ttft/endpoint default None
    record_call_completed(
        cap,
        outcome="completed",
        metrics=_metrics(provider_mode="pipeline", latency=latency),
    )

    _, dims = cap.records[0]
    assert dims["stt_latency_ms"] == 100
    assert dims["tts_first_byte_ms"] == 50
    assert "llm_ttft_ms" not in dims
    assert "eou_latency_ms" not in dims


# --------------------------------------------------------------------------- #
# F3 — error_layer + disconnect_reason                                        #
# --------------------------------------------------------------------------- #


def test_clean_completion_maps_to_error_layer_none_and_disconnect_completed() -> None:
    cap = CaptureTelemetry()
    record_call_completed(
        cap,
        outcome="completed",
        metrics=_metrics(provider_mode="pipeline", latency=LatencyBreakdown()),
    )
    _, dims = cap.records[0]
    assert dims["error_layer"] == "none"
    assert dims["disconnect_reason"] == "completed"
    assert dims["outcome"] == "completed"


def test_llm_auth_error_maps_to_llm_layer_and_error_disconnect() -> None:
    cap = CaptureTelemetry()
    record_call_completed(
        cap,
        outcome="completed",
        metrics=_metrics(
            provider_mode="pipeline", latency=LatencyBreakdown(), error_code="auth"
        ),
    )
    _, dims = cap.records[0]
    # Connected call with a terminal error flips the outcome to "error".
    assert dims["outcome"] == "error"
    assert dims["error_code"] == "auth"
    assert dims["error_layer"] == "llm"
    assert dims["disconnect_reason"] == "error"


def test_timeout_error_maps_disconnect_to_timeout() -> None:
    cap = CaptureTelemetry()
    record_call_completed(
        cap,
        outcome="completed",
        metrics=_metrics(
            provider_mode="pipeline", latency=LatencyBreakdown(), error_code="timeout"
        ),
    )
    _, dims = cap.records[0]
    assert dims["error_layer"] == "llm"
    assert dims["disconnect_reason"] == "timeout"


def test_webhook_verification_error_maps_to_carrier_layer() -> None:
    cap = CaptureTelemetry()
    record_call_completed(
        cap,
        outcome="completed",
        metrics=_metrics(
            provider_mode="pipeline",
            latency=LatencyBreakdown(),
            error_code="webhook_verification",
        ),
    )
    _, dims = cap.records[0]
    assert dims["error_layer"] == "carrier"


def test_provider_error_maps_to_other_layer() -> None:
    cap = CaptureTelemetry()
    record_call_completed(
        cap,
        outcome="completed",
        metrics=_metrics(
            provider_mode="pipeline",
            latency=LatencyBreakdown(),
            error_code="provider_error",
        ),
    )
    _, dims = cap.records[0]
    assert dims["error_layer"] == "other"
    assert dims["disconnect_reason"] == "error"


def test_no_answer_failure_maps_disconnect_no_answer_and_layer_none() -> None:
    cap = CaptureTelemetry()
    record_call_completed(cap, outcome="no_answer", carrier="twilio")
    _, dims = cap.records[0]
    assert dims["outcome"] == "no_answer"
    assert dims["disconnect_reason"] == "no_answer"
    assert dims["error_layer"] == "none"


def test_busy_failure_maps_disconnect_busy() -> None:
    cap = CaptureTelemetry()
    record_call_completed(cap, outcome="busy", carrier="telnyx")
    _, dims = cap.records[0]
    assert dims["disconnect_reason"] == "busy"
    assert dims["error_layer"] == "none"


def test_carrier_failed_maps_disconnect_error_and_layer_carrier() -> None:
    cap = CaptureTelemetry()
    record_call_completed(cap, outcome="failed", carrier="twilio")
    _, dims = cap.records[0]
    assert dims["disconnect_reason"] == "error"
    assert dims["error_layer"] == "carrier"


# --------------------------------------------------------------------------- #
# F4 — time_to_first_call_bucket (both events)                                #
# --------------------------------------------------------------------------- #


def _set_install_mtime(state_dir: str, age_seconds: float) -> None:
    """Create a real install-id file whose mtime is ``age_seconds`` old."""
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "install-id")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("0" * 32)
    import time as _time

    when = _time.time() - age_seconds
    os.utime(path, (when, when))


def test_time_to_first_call_bucket_on_started_and_completed(
    tmp_path: Any, monkeypatch: Any
) -> None:
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", state_dir)
    # Reset the cached install id so the new state dir is honored.
    monkeypatch.setattr(install_id_mod, "_install_id", None, raising=False)

    # A 30-minute-old install → lt_1h on both events.
    _set_install_mtime(state_dir, age_seconds=30 * 60)

    started = CaptureTelemetry()
    record_call_started(started, provider_mode="pipeline", telephony_provider="twilio")
    completed = CaptureTelemetry()
    record_call_completed(
        completed,
        outcome="completed",
        metrics=_metrics(provider_mode="pipeline", latency=LatencyBreakdown()),
    )

    assert started.records[0][1]["time_to_first_call_bucket"] == "lt_1h"
    assert completed.records[0][1]["time_to_first_call_bucket"] == "lt_1h"


def test_time_to_first_call_bucket_boundaries(tmp_path: Any, monkeypatch: Any) -> None:
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", state_dir)

    cases = [
        (60, "lt_1h"),
        (3600 + 1, "1h_1d"),
        (86400 + 1, "1d_7d"),
        (604800 + 1, "gt_7d"),
    ]
    for age, expected in cases:
        _set_install_mtime(state_dir, age_seconds=age)
        cap = CaptureTelemetry()
        record_call_started(cap, provider_mode="pipeline")
        assert cap.records[0][1]["time_to_first_call_bucket"] == expected


def test_time_to_first_call_bucket_unknown_when_state_unreadable(
    tmp_path: Any, monkeypatch: Any
) -> None:
    # Point the state dir at a path with no install-id file → mtime unreadable.
    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", str(tmp_path / "does-not-exist"))
    cap = CaptureTelemetry()
    record_call_started(cap, provider_mode="pipeline")
    assert cap.records[0][1]["time_to_first_call_bucket"] == "unknown"


def test_install_age_seconds_reads_mtime(tmp_path: Any, monkeypatch: Any) -> None:
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("PATTER_TELEMETRY_STATE_DIR", state_dir)
    _set_install_mtime(state_dir, age_seconds=7200)
    age = install_id_mod.install_age_seconds()
    assert age is not None
    assert 7000 <= age <= 7400  # ~2h, allow scheduling slack


def test_builders_never_raise_with_telemetry_none() -> None:
    # Fire-and-forget contract: None sink is a no-op, never throws.
    record_call_started(None, provider_mode="pipeline")
    record_call_completed(None, outcome="completed")
    # Reference call_metrics module to keep the import meaningful.
    assert hasattr(call_metrics, "record_call_completed")

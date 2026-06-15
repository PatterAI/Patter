"""Build the per-call ``call_completed`` telemetry event.

Pure, None-guarded, and never raises — it is called inline on the call-end path,
so it must do only O(1) work and never block or throw. It records only coarse,
anonymous facts (engine/provider/carrier families, the terminal outcome, and the
raw latency/duration and total USD cost of the call); no per-call identifier, no PII.

``latency_ms`` (whole milliseconds) and ``duration_seconds`` (whole seconds) are
sent at full resolution — they are operational metrics, not the kind of
commercially-sensitive or name-bearing data that the bucketing posture protects.

Mirrors ``libraries/typescript/src/telemetry/call-metrics.ts``.
"""

from __future__ import annotations

from typing import Any

from getpatter.telemetry.install_id import install_age_seconds


def _engine_from_mode(mode: str | None) -> str:
    if mode in ("openai_realtime", "openai_realtime_2"):
        return "realtime"
    if mode == "elevenlabs_convai":
        return "convai"
    if mode == "pipeline":
        return "pipeline"
    return "other"


def _provider_from_metrics(metrics: Any) -> str:
    mode = getattr(metrics, "provider_mode", None)
    if mode in ("openai_realtime", "openai_realtime_2"):
        return "openai"
    if mode == "elevenlabs_convai":
        return "elevenlabs"
    # Pipeline: the primary brain is the LLM, else STT, else TTS. The value
    # allowlist coerces anything not on the provider enum to "other".
    for attr in ("llm_provider", "stt_provider", "tts_provider"):
        value = getattr(metrics, attr, None)
        if value:
            return str(value).lower()
    return "other"


def _provider_from_mode(mode: str | None) -> str:
    """Coarse provider family from the provider mode, for ``call_started`` (no
    metrics yet). Pipeline's brain vendor isn't known cheaply at connect, so it
    collapses to ``other`` (the value allowlist coerces anything off-list anyway)."""
    if mode in ("openai_realtime", "openai_realtime_2"):
        return "openai"
    if mode == "elevenlabs_convai":
        return "elevenlabs"
    return "other"


def _carrier_family(telephony_provider: str | None) -> str:
    return str(telephony_provider).lower() if telephony_provider else "none"


def _direction(value: Any) -> str | None:
    """Normalise the call direction to ``inbound`` / ``outbound``; omit if unknown
    (rather than guessing a default that would bias the inbound/outbound split)."""
    v = str(value).lower() if value else ""
    return v if v in ("inbound", "outbound") else None


def _turn_count_bucket(n: int) -> str:
    """Coarse bucket for the number of conversational turns in the call."""
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 3:
        return "2_3"
    if n <= 6:
        return "4_6"
    if n <= 12:
        return "7_12"
    return "13_plus"


def _latency_ms(metrics: Any) -> float | None:
    p95 = getattr(metrics, "latency_p95", None)
    return getattr(p95, "agent_response_ms", None) if p95 is not None else None


def _whole_ms(value: Any) -> int | None:
    """Coerce a millisecond latency to a whole non-negative int, or ``None``.

    ``None`` when the source is absent OR ``0`` (a stage that did not run — e.g.
    realtime/convai have no separate STT/TTS span, so those breakdown fields
    stay ``0.0`` and are omitted rather than reported as a false zero).
    """
    if value is None:
        return None
    try:
        ms = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return None
    return ms if ms > 0 else None


def _per_stage_latencies(metrics: Any) -> dict[str, int]:
    """Read the per-stage latency breakdown off the existing p95 accumulator.

    Read-only — no new audio-path instrumentation. Maps the
    ``LatencyBreakdown`` fields onto the wire dims, omitting any stage whose
    source is ``None`` or ``0`` (stage did not run). Same source object as
    ``latency_ms`` (``metrics.latency_p95``).
    """
    p95 = getattr(metrics, "latency_p95", None)
    if p95 is None:
        return {}
    mapping = {
        "stt_latency_ms": getattr(p95, "stt_ms", None),
        "llm_ttft_ms": getattr(p95, "llm_ttft_ms", None),
        "tts_first_byte_ms": getattr(p95, "tts_ms", None),
        "eou_latency_ms": getattr(p95, "endpoint_ms", None),
    }
    out: dict[str, int] = {}
    for key, raw in mapping.items():
        whole = _whole_ms(raw)
        if whole is not None:
            out[key] = whole
    return out


# Coarse, deterministic error_code -> error_layer mapping. NEVER the message;
# the layer is inferred only from the structured code. ``provider_error`` is not
# attributable to a specific stage from the code alone, so it maps to ``other``.
# Keep byte-for-byte identical to ``ERROR_LAYER_BY_CODE`` in ``call-metrics.ts``.
_ERROR_LAYER_BY_CODE: dict[str, str] = {
    "auth": "llm",
    "rate_limit": "llm",
    "timeout": "llm",
    "provider_error": "other",
    "connection": "other",
    "webhook_verification": "carrier",
    "provision": "carrier",
    "config": "config",
    "input_validation": "config",
    "internal": "internal",
}


def _error_layer(error_code: str) -> str:
    """Map a terminal ``error_code`` to its coarse originating layer.

    ``"none"`` on a clean completion (no code); the value allowlist coerces
    anything off the enum to ``"other"``.
    """
    if not error_code:
        return "none"
    return _ERROR_LAYER_BY_CODE.get(error_code, "other")


def _disconnect_reason(outcome: str, error_code: str) -> str:
    """Map the terminal outcome (+ error_code) to a coarse disconnect reason.

    Derived only from already-known state. ``hangup_local`` / ``hangup_remote``
    are NOT set here because the hanging-up side is not reliably known on this
    path; clean ends collapse to ``completed`` and the value allowlist coerces
    anything off-list to ``other``. Keep identical to ``disconnectReason`` in
    ``call-metrics.ts``.
    """
    if outcome == "no_answer":
        return "no_answer"
    if outcome == "busy":
        return "busy"
    if outcome == "error":
        return "timeout" if error_code == "timeout" else "error"
    if outcome == "failed":
        return "timeout" if error_code == "timeout" else "error"
    if outcome == "completed":
        return "completed"
    return "other"


def _time_to_first_call_bucket(age_seconds: float | None) -> str:
    """Bucket the install age (seconds) into a coarse time-to-first-call band.

    ``unknown`` when the age can't be read. Keep boundaries byte-for-byte
    identical to ``timeToFirstCallBucket`` in ``call-metrics.ts``.
    """
    if age_seconds is None:
        return "unknown"
    if age_seconds < 3600:
        return "lt_1h"
    if age_seconds < 86400:
        return "1h_1d"
    if age_seconds < 604800:
        return "1d_7d"
    return "gt_7d"


def record_call_started(
    telemetry: Any,
    *,
    provider_mode: str | None = None,
    telephony_provider: str | None = None,
    direction: Any = None,
    call_uid: str | None = None,
) -> None:
    """Emit a ``call_started`` event when a call connects (media stream begins).

    Pairs with ``call_completed`` to give a connect→complete funnel and a
    denominator for the failure rate, and carries the inbound/outbound split. No
    metrics exist yet at connect, so only coarse engine/provider/carrier/direction
    are recorded. Safe with ``telemetry=None``. Swallows everything.

    Mirrors ``recordCallStarted`` in ``call-metrics.ts``.
    """
    if telemetry is None:
        return
    try:
        dims: dict[str, Any] = {
            "engine": _engine_from_mode(provider_mode),
            "provider": _provider_from_mode(provider_mode),
            "carrier": _carrier_family(telephony_provider),
        }
        d = _direction(direction)
        if d is not None:
            dims["direction"] = d
        if call_uid is not None:
            dims["call_uid"] = call_uid
        # F4: how long after install this call fires (coarse bucket).
        dims["time_to_first_call_bucket"] = _time_to_first_call_bucket(
            install_age_seconds()
        )
        telemetry.record("call_started", **dims)
    except Exception:
        pass


def record_call_completed(
    telemetry: Any,
    *,
    outcome: str,
    metrics: Any = None,
    carrier: str | None = None,
    direction: Any = None,
    call_uid: str | None = None,
) -> None:
    """Emit a ``call_completed`` event.

    Two callers:
    * Connected calls (the call-end path) pass ``metrics`` and ``outcome="completed"``.
    * Non-connected failures pass ``outcome`` in {no_answer, busy, failed} and a
      ``carrier`` (no metrics → latency/duration omitted).

    ``direction`` (inbound/outbound) is recorded when known. Safe to call with
    ``telemetry=None``. Swallows everything.
    """
    if telemetry is None:
        return
    try:
        dims: dict[str, Any] = {"outcome": outcome}
        d = _direction(direction)
        if d is not None:
            dims["direction"] = d
        if call_uid is not None:
            dims["call_uid"] = call_uid
        if metrics is not None:
            dims["engine"] = _engine_from_mode(getattr(metrics, "provider_mode", None))
            dims["provider"] = _provider_from_metrics(metrics)
            dims["carrier"] = _carrier_family(
                getattr(metrics, "telephony_provider", None)
            )
            duration = getattr(metrics, "duration_seconds", None)
            if duration is not None:
                dims["duration_seconds"] = max(0, int(round(duration)))
            latency = _latency_ms(metrics)
            if latency is not None:
                dims["latency_ms"] = max(0, int(round(latency)))
            cost = getattr(metrics, "cost", None)
            cost_total = getattr(cost, "total", None) if cost is not None else None
            if cost_total is not None:
                dims["cost_usd"] = max(0.0, round(float(cost_total), 4))
            turns = getattr(metrics, "turns", None)
            if turns is not None:
                dims["turn_count_bucket"] = _turn_count_bucket(len(turns))
            # F2: per-stage latency, read-only off the same p95 breakdown as
            # latency_ms. Each dim is omitted when its stage didn't run.
            dims.update(_per_stage_latencies(metrics))
            # A connected call that ended with a terminal error: surface the code
            # and flip the outcome to "error" (the value allowlist coerces an
            # unknown code to "other").
            error_code = getattr(metrics, "error_code", "") or ""
            if error_code:
                dims["error_code"] = error_code
                dims["outcome"] = "error"
            # F3: coarse error layer + disconnect reason, derived deterministically
            # from the (now-final) outcome and error_code. error_layer is "none"
            # on a clean completion.
            dims["error_layer"] = _error_layer(error_code)
            dims["disconnect_reason"] = _disconnect_reason(dims["outcome"], error_code)
        elif carrier is not None:
            dims["carrier"] = _carrier_family(carrier)
            # F3: non-connected failures (no_answer/busy/failed) carry no metrics,
            # so there is no error_code — map the disconnect reason from the
            # outcome alone; error_layer stays "carrier" only when the carrier
            # itself failed (outcome "failed"), else "none".
            dims["error_layer"] = "carrier" if outcome == "failed" else "none"
            dims["disconnect_reason"] = _disconnect_reason(outcome, "")
        # F4: how long after install this call fires (coarse bucket) — on both
        # call_started and call_completed for the activation-funnel join.
        dims["time_to_first_call_bucket"] = _time_to_first_call_bucket(
            install_age_seconds()
        )
        telemetry.record(
            "call_completed", **{k: v for k, v in dims.items() if v is not None}
        )
    except Exception:
        pass

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


def record_call_started(
    telemetry: Any,
    *,
    provider_mode: str | None = None,
    telephony_provider: str | None = None,
    direction: Any = None,
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
            # A connected call that ended with a terminal error: surface the code
            # and flip the outcome to "error" (the value allowlist coerces an
            # unknown code to "other").
            error_code = getattr(metrics, "error_code", "") or ""
            if error_code:
                dims["error_code"] = error_code
                dims["outcome"] = "error"
        elif carrier is not None:
            dims["carrier"] = _carrier_family(carrier)
        telemetry.record(
            "call_completed", **{k: v for k, v in dims.items() if v is not None}
        )
    except Exception:
        pass

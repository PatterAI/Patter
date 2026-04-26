"""Optional observability hooks for Patter.

Currently ships with OpenTelemetry tracing and an in-process EventBus.

Enable OTel tracing with::

    export PATTER_OTEL_ENABLED=1
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

Then call :func:`getpatter.observability.init_tracing` once at process start.
"""

from getpatter.observability.event_bus import EventBus, PatterEventType
from getpatter.observability.metric_types import (
    CachedTokenDetails,
    EOUMetrics,
    InputTokenDetails,
    InterruptionMetrics,
    LLMUsage,
    Metadata,
    OutputTokenDetails,
    ProcessingMetrics,
    RealtimeUsage,
    TTFBMetrics,
)
from getpatter.observability.tracing import (
    SPAN_CALL,
    SPAN_LLM,
    SPAN_STT,
    SPAN_TOOL,
    SPAN_TTS,
    get_tracer,
    init_tracing,
    is_enabled,
    shutdown_tracing,
    start_span,
)

__all__ = [
    # Tracing
    "init_tracing",
    "is_enabled",
    "start_span",
    "shutdown_tracing",
    "get_tracer",
    "SPAN_CALL",
    "SPAN_STT",
    "SPAN_LLM",
    "SPAN_TTS",
    "SPAN_TOOL",
    # Event bus
    "EventBus",
    "PatterEventType",
    # Metric types
    "Metadata",
    "LLMUsage",
    "CachedTokenDetails",
    "InputTokenDetails",
    "OutputTokenDetails",
    "RealtimeUsage",
    "EOUMetrics",
    "InterruptionMetrics",
    "TTFBMetrics",
    "ProcessingMetrics",
]

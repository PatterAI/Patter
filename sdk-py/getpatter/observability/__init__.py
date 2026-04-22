"""Optional observability hooks for Patter.

Currently ships with OpenTelemetry tracing. Enable with::

    export PATTER_OTEL_ENABLED=1
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

Then call :func:`getpatter.observability.init_tracing` once at process start.
"""

from getpatter.observability.tracing import (
    init_tracing,
    is_enabled,
    start_span,
    shutdown_tracing,
    get_tracer,
)

__all__ = [
    "init_tracing",
    "is_enabled",
    "start_span",
    "shutdown_tracing",
    "get_tracer",
]

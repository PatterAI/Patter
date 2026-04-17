"""OpenTelemetry tracing helpers for Patter.

Design goals:
* **Zero cost when disabled.** ``start_span`` falls back to a cheap no-op
  context manager unless the opt-in env var ``PATTER_OTEL_ENABLED=1`` is set
  *and* the OTel SDK could be imported.
* **Opt-in only.** No telemetry is emitted by default. We never export PII
  (user utterances, tool payloads) as span attributes — only sizes and
  provider identifiers.
* **Single source of truth for span names.** Downstream services should
  attribute spans by the constants at the bottom of this module.

Spans used by Patter:

* ``patter.call`` — the top-level call span (created in the stream handler)
* ``patter.stt`` — a streamed STT inference
* ``patter.llm`` — an LLM completion (per turn)
* ``patter.tts`` — a TTS synthesis
* ``patter.tool`` — a single tool invocation
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger("patter.observability")

ENV_FLAG = "PATTER_OTEL_ENABLED"
SERVICE_NAME = "patter"

# --- Span names -------------------------------------------------------------
SPAN_CALL = "patter.call"
SPAN_STT = "patter.stt"
SPAN_LLM = "patter.llm"
SPAN_TTS = "patter.tts"
SPAN_TOOL = "patter.tool"

# --- State ------------------------------------------------------------------
_tracer: Any = None
_provider: Any = None
_initialized = False


def is_enabled() -> bool:
    """True only if the env flag is set *and* the tracer initialized cleanly."""
    return bool(_tracer) and os.getenv(ENV_FLAG, "").lower() in {"1", "true", "yes"}


def init_tracing(
    service_name: str = SERVICE_NAME,
    otlp_endpoint: str | None = None,
    resource_attributes: dict[str, str] | None = None,
) -> bool:
    """Install the OTel provider + OTLP exporter.

    Returns True if tracing was actually wired. Silently returns False if
    the env flag is off or the OTel packages are not installed.
    """
    global _tracer, _provider, _initialized

    if _initialized:
        return is_enabled()

    if os.getenv(ENV_FLAG, "").lower() not in {"1", "true", "yes"}:
        _initialized = True
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "PATTER_OTEL_ENABLED=1 but opentelemetry SDK not installed. "
            "Install with: pip install getpatter[tracing]"
        )
        _initialized = True
        return False

    attrs = {"service.name": service_name}
    if resource_attributes:
        attrs.update(resource_attributes)
    resource = Resource.create(attrs)
    _provider = TracerProvider(resource=resource)

    endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces") if endpoint else OTLPSpanExporter()
        _provider.add_span_processor(BatchSpanProcessor(exporter))
    except ImportError:
        logger.warning("opentelemetry OTLP exporter not installed; spans will not be exported")

    trace.set_tracer_provider(_provider)
    _tracer = trace.get_tracer(service_name)
    _initialized = True
    logger.info("Patter OTel tracing enabled (service=%s, endpoint=%s)", service_name, endpoint)
    return True


def get_tracer() -> Any:
    """Returns the current tracer or None."""
    return _tracer


@contextmanager
def start_span(
    name: str,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Start a span with optional attributes.

    No-op (yields None) when tracing is disabled. Callers should tolerate a
    None span — see :meth:`_noop_span_attr`.
    """
    if not is_enabled():
        yield None
        return

    span_cm = _tracer.start_as_current_span(name)
    span = span_cm.__enter__()
    try:
        if attributes:
            for k, v in attributes.items():
                try:
                    span.set_attribute(k, v)
                except Exception:  # pragma: no cover
                    pass
        yield span
    except Exception as exc:  # pragma: no cover - we still need to record + re-raise
        try:
            span.record_exception(exc)
        except Exception:
            pass
        raise
    finally:
        try:
            span_cm.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            pass


def shutdown_tracing() -> None:
    """Flush any pending spans. Safe to call unconditionally."""
    global _tracer, _provider, _initialized
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:  # pragma: no cover
            pass
    _tracer = None
    _provider = None
    _initialized = False

"""patter.* span attribute helpers.

Lazy-OTel-guarded helpers used by ``getpatter`` to stamp ``patter.cost.*``
and ``patter.latency.*`` attributes on spans during a call's lifecycle.
The two ContextVars (``patter.call_id`` and ``patter.side``) propagate
through asyncio task trees so spans emitted by deeply nested provider
code inherit the call's identity automatically.

See ``docs/DEVLOG.md`` for the version decision and rollout history.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping

logger = logging.getLogger("getpatter.observability")

try:
    from opentelemetry import trace as _trace

    _OTEL = True
except ImportError:  # pragma: no cover — optional [tracing] extra
    _trace = None  # type: ignore[assignment]
    _OTEL = False

DEFAULT_SIDE = (
    "uut"  # "unit-under-test"; the side value used when no driver/UUT split is in play.
)

_patter_call_id: ContextVar[str | None] = ContextVar("patter.call_id", default=None)
_patter_side: ContextVar[str] = ContextVar("patter.side", default=DEFAULT_SIDE)


def record_patter_attrs(attrs: Mapping[str, Any]) -> None:
    """Stamp ``patter.*`` attributes on the current span, plus call_id and side.

    Behaviour:
    - No-op if OTel is missing or no ``patter_call_scope`` is active.
    - If a recording span is active, attributes are stamped on it.
    - If no recording span is active, a transient zero-duration
      ``patter.billable`` span is opened solely to carry the attributes.
      This is a best-effort fallback for callers without their own span;
      downstream collectors that filter zero-duration spans may drop
      these. Callers that want guaranteed attribution should wrap their
      billable work in their own span.

    Caller-provided ``patter.call_id`` / ``patter.side`` keys win over
    the ContextVar values (via ``setdefault``).
    """
    if not _OTEL:
        return
    call_id = _patter_call_id.get()
    if call_id is None:
        return
    side = _patter_side.get()
    full = dict(attrs)
    # setdefault: caller-provided patter.call_id/side wins
    full.setdefault("patter.call_id", call_id)
    # setdefault: caller-provided patter.call_id/side wins
    full.setdefault("patter.side", side)

    span = _trace.get_current_span()
    if span is not None and span.is_recording():
        for k, v in full.items():
            span.set_attribute(k, v)
        return

    tracer = _trace.get_tracer("getpatter.observability")
    with tracer.start_as_current_span("patter.billable") as new_span:
        for k, v in full.items():
            new_span.set_attribute(k, v)


@contextmanager
def patter_call_scope(*, call_id: str, side: str = DEFAULT_SIDE) -> Iterator[None]:
    """Bind call_id and side to the current asyncio task tree."""
    if not call_id:
        raise ValueError("patter_call_scope requires non-empty call_id")
    cid_token = _patter_call_id.set(call_id)
    side_token = _patter_side.set(side)
    try:
        yield
    finally:
        _patter_call_id.reset(cid_token)
        _patter_side.reset(side_token)


def attach_span_exporter(
    patter_instance: Any, exporter: Any, *, side: str = DEFAULT_SIDE
) -> None:
    """Wire ``exporter`` into the global TracerProvider via SimpleSpanProcessor.

    Stores ``side`` on the Patter instance (``_patter_side`` attr) so the
    per-call handler reads it when entering ``patter_call_scope``.

    Idempotency contract: idempotent on the *same exporter object reference*.
    If the caller constructs two distinct exporter instances pointing at the
    same backend (e.g. two ``OTLPSpanExporter(endpoint=...)`` calls), both
    will be attached and spans will be exported twice. Hold a single
    exporter object and pass it on every call to avoid duplicates.

    If the global TracerProvider is not a ``TracerProvider`` instance
    (e.g. the no-op ``ProxyTracerProvider``), it is replaced with a fresh
    one and a warning is logged.
    """
    patter_instance._patter_side = side

    if not _OTEL:
        logger.debug("attach_span_exporter: OTel not installed; only side= stored")
        return

    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError:
        logger.warning(
            "attach_span_exporter: opentelemetry-sdk not installed; "
            "spans will not be exported. Install getpatter[tracing]."
        )
        return

    provider = _trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        # Replacing a non-SDK provider would destroy any host-app
        # instrumentation already attached. Warn loudly so the operator
        # can pass their own SDK TracerProvider via init_tracing() instead.
        logger.warning(
            "attach_span_exporter: replacing existing TracerProvider %r with "
            "a fresh getpatter-managed TracerProvider. If your host app uses "
            "OTel auto-instrumentation, configure a TracerProvider before "
            "calling _attach_span_exporter to avoid losing those processors.",
            type(provider).__name__,
        )
        provider = TracerProvider()
        _trace.set_tracer_provider(provider)

    seen = getattr(provider, "_patter_attached_exporters", None)
    if seen is None:
        seen = set()
        provider._patter_attached_exporters = seen
    if id(exporter) in seen:
        return
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    seen.add(id(exporter))

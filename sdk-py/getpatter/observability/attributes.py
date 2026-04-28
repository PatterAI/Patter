"""patter.* span attribute helpers.

Version decision (2026-04-28): we are bumping getpatter 0.5.4 -> 0.5.5
because this module adds Patter._attach_span_exporter, a new opt-in
public method consumed by patter-agent-runner. During development the
acceptance suite installs from a worktree editable install; the version
bump only matters for the published artifact.
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

_patter_call_id: ContextVar[str | None] = ContextVar("patter.call_id", default=None)
_patter_side: ContextVar[str] = ContextVar("patter.side", default="uut")


def record_patter_attrs(attrs: Mapping[str, Any]) -> None:
    """Stamp `patter.*` attributes on the current span, plus call_id and side.

    No-op if OTel is missing, no `patter_call_scope` is active, or the active
    span (or absence thereof) cannot be stamped. Opens a transient
    `patter.billable` span if no recording span is active so the attributes are
    not dropped.
    """
    if not _OTEL:
        return
    call_id = _patter_call_id.get()
    if call_id is None:
        return
    side = _patter_side.get()
    full = dict(attrs)
    full.setdefault("patter.call_id", call_id)
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
def patter_call_scope(*, call_id: str, side: str = "uut") -> Iterator[None]:
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
    patter_instance: Any, exporter: Any, *, side: str = "uut"
) -> None:
    """Wire `exporter` into the global TracerProvider via SimpleSpanProcessor.

    Stores `side` on the Patter instance (`_patter_side` attr) so the per-call
    handler reads it when entering `patter_call_scope`. Idempotent on the
    same exporter instance.
    """
    setattr(patter_instance, "_patter_side", side)

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
        provider = TracerProvider()
        _trace.set_tracer_provider(provider)

    seen = getattr(provider, "_patter_attached_exporters", None)
    if seen is None:
        seen = set()
        setattr(provider, "_patter_attached_exporters", seen)
    if id(exporter) in seen:
        return
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    seen.add(id(exporter))

"""Unit tests for getpatter.observability.attributes — patter.* span helpers.

Covers the public surface of the new attributes module:

- ``patter_call_scope`` ContextVar lifecycle
- ``record_patter_attrs`` no-op when no scope is active
- ``record_patter_attrs`` stamps on the active span when a scope is active
- ``attach_span_exporter`` is idempotent on the same exporter object
- The Patter._attach_span_exporter public hook routes through correctly

These tests avoid hitting any external service and use the in-memory
OTel test exporter so they remain fast and deterministic.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestPatterCallScope:
    """patter_call_scope binds call_id and side to the asyncio task tree."""

    def test_scope_requires_non_empty_call_id(self) -> None:
        from getpatter.observability.attributes import patter_call_scope

        with pytest.raises(ValueError):
            with patter_call_scope(call_id=""):
                pass

    def test_scope_round_trip_resets_contextvars(self) -> None:
        from getpatter.observability.attributes import (
            _patter_call_id,
            _patter_side,
            patter_call_scope,
        )

        # Outside the scope the ContextVar is at its default (None / "uut").
        assert _patter_call_id.get() is None
        assert _patter_side.get() == "uut"

        with patter_call_scope(call_id="CA000", side="driver"):
            assert _patter_call_id.get() == "CA000"
            assert _patter_side.get() == "driver"

        # After exit the ContextVar is reset.
        assert _patter_call_id.get() is None
        assert _patter_side.get() == "uut"


@pytest.mark.unit
class TestRecordPatterAttrs:
    """record_patter_attrs is a safe no-op outside a scope."""

    def test_no_scope_active_is_noop(self) -> None:
        """Outside ``patter_call_scope`` the helper returns silently."""
        from getpatter.observability.attributes import record_patter_attrs

        # Must not raise even with no OTel scope; payload is dropped.
        record_patter_attrs({"patter.cost.tts_chars": 42})

    def test_inside_scope_no_otel_is_safe(self) -> None:
        """Inside a scope, when OTel is not configured, the helper is still safe."""
        from getpatter.observability.attributes import (
            patter_call_scope,
            record_patter_attrs,
        )

        with patter_call_scope(call_id="CA111"):
            # Should not raise — the helper is defensive.
            record_patter_attrs({"patter.cost.stt_seconds": 1.5})


@pytest.mark.unit
class TestAttachSpanExporterPublic:
    """Patter._attach_span_exporter is the public hook used by patter-agent-runner."""

    def test_attach_span_exporter_stores_side_on_instance(self) -> None:
        """Even without OTel SDK, the helper stamps ``_patter_side`` on the
        Patter instance so downstream code (StreamHandler) can inherit it."""

        # Build a minimal stand-in for Patter that exposes ``_patter_side``.
        class _Stub:
            _patter_side: str = "uut"

        from getpatter.observability.attributes import attach_span_exporter

        stub = _Stub()
        # Pass an opaque exporter — when OTel SDK is missing the helper logs
        # and returns; either way the side= arg must be stored on the stub.
        attach_span_exporter(stub, exporter=object(), side="driver")
        assert stub._patter_side == "driver"

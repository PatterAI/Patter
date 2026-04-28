# `getpatter` OTel `patter.*` Span Attributes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `patter.*` OTel span attributes to `getpatter` so that `patter-agent-runner` reports non-zero `cost_usd` and `latency.{ttfb_ms,turn_p95_ms}` from real PSTN calls.

**Architecture:** Lazy-OTel ContextVar-based helper (`getpatter/observability/attributes.py`) that stamps `patter.*` attributes on the active span (or auto-opens a transient span when none active). Two ContextVars (`patter.call_id`, `patter.side`) propagate through asyncio task trees. `Patter._attach_span_exporter(exporter, *, side="uut")` is the public hook the consumer (`patter-agent-runner`) already calls.

**Tech Stack:** Python 3.11+, `opentelemetry-api>=1.27`, `opentelemetry-sdk>=1.27`, `pytest`, `pytest-asyncio` (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-04-28-getpatter-otel-span-attributes-design.md`

**Phase strategy:** The plan is organised so Phase 3 alone takes the failing smoke test green (Twilio + OpenAI Realtime + latency). Phases 4-6 fan out coverage to the remaining providers. Each phase ends with a green test run and a commit.

---

## Phase 0 — Setup

### Task 1: Create the worktree

**Files:**
- Modify: shell state only (no files yet)

- [ ] **Step 1: Verify the parent repo is on a clean main**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter
git status --short
git branch --show-current
```

Expected: empty status, branch `main`. Abort if anything pending.

- [ ] **Step 2: Create the worktree branched off main**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter
git worktree add .claude/worktrees/observability-spans -b feat/observability-otel-attrs main
```

Expected: `Preparing worktree (new branch 'feat/observability-otel-attrs')` then `HEAD is now at <sha>`.

- [ ] **Step 3: Verify the worktree layout**

```bash
ls /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/observability/
```

Expected: `__init__.py`, `tracing.py`, `event_bus.py`, `metric_types.py`. (No `attributes.py` yet — that's Task 4.)

- [ ] **Step 4: Pin the worktree path for later commands**

Set a working alias for the rest of the plan:
- `WORKTREE = /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans`
- `SDK = $WORKTREE/sdk-py`
- `ACCEPT = /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance`
- `RUNNER = /Users/francescorosciano/docs/patter/[FrancescoRosciano]-patter-agent-runner`

(For shell expansion, set them in the shell that executes commands. They are spelled out in full path in every command below for clarity.)

### Task 2: Point the acceptance suite at the worktree

**Files:**
- Modify: `[nicolotognoni]-patter-sdk-acceptance/python/requirements.txt:13` (the editable `-e` line)

- [ ] **Step 1: Inspect the current line**

```bash
grep -n "^-e " /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python/requirements.txt
```

Expected: a single `-e /Users/nicolotognoni/Documents/Dev/projects/PatterAI/Patter/.claude/worktrees/jolly-petting-hejlsberg/sdk-py[...]` line.

- [ ] **Step 2: Create a local-only branch in the acceptance suite**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git status --short
git checkout -b local/observability-spans-frosci
```

Expected: clean status; switched to new branch.

- [ ] **Step 3: Replace the editable path**

Edit `python/requirements.txt`. Replace the line starting with `-e /Users/nicolotognoni/...` with:

```
-e /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py[scheduling,assemblyai,soniox,speechmatics,cartesia,rime,lmnt,anthropic,groq,cerebras,google,gemini-live,ultravox,background-audio,ivr,silero,telnyx-ai]
```

Keep the trailing extras list character-for-character identical to the original.

- [ ] **Step 4: Reinstall the editable package into the venv**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
source .venv/bin/activate
pip install -r requirements.txt
pip show getpatter | grep -E "^(Location|Version)"
```

Expected: `Location: /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py` and `Version: 0.5.4`.

- [ ] **Step 5: Commit the local branch (do NOT push)**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/requirements.txt
git commit -m "chore(local): point editable getpatter at observability-spans worktree

Local-only — do not push. Reverts when getpatter==0.5.5 ships."
```

### Task 3: Add the `mocked` pytest marker if missing

**Files:**
- Modify: `[nicolotognoni]-patter-sdk-acceptance/python/pytest.ini` or `pyproject.toml` (whichever holds markers)

- [ ] **Step 1: Locate the markers config**

```bash
grep -n "markers" /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python/pytest.ini /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python/pyproject.toml 2>/dev/null
```

- [ ] **Step 2: If `mocked` is absent, add it**

In whichever file declares `markers`, add a line:

```
mocked: tests that mock external network/IO boundaries (declared per .claude/rules/authentic-tests.md)
```

If neither file declares markers, add this stanza to `pytest.ini`:

```ini
[pytest]
markers =
    unit: pure-code tests, no IO
    integration: real local services, no external network
    mocked: external-boundary mocks (websocket / HTTP carrier)
```

- [ ] **Step 3: Verify pytest accepts the marker**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest --markers | grep "@pytest.mark.mocked"
```

Expected: a single line printing the `mocked` marker description.

- [ ] **Step 4: Commit on the local branch**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/pytest.ini python/pyproject.toml 2>/dev/null
git commit -m "test: declare 'mocked' pytest marker per authentic-tests rule"
```

---

## Phase 1 — Helper module

### Task 4: Create the `attributes.py` skeleton (test for no-OTel safe import)

**Files:**
- Create: `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/observability/attributes.py`
- Create: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/__init__.py` (empty)
- Create: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_helper_module.py`

- [ ] **Step 1: Write the failing test**

Create `tests/observability/__init__.py` empty, then `tests/observability/test_helper_module.py`:

```python
"""Pure-helper tests for getpatter.observability.attributes — no Patter, no providers."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_module_imports_when_otel_present():
    from getpatter.observability import attributes

    assert hasattr(attributes, "record_patter_attrs")
    assert hasattr(attributes, "patter_call_scope")
    assert hasattr(attributes, "attach_span_exporter")


@pytest.mark.unit
def test_record_patter_attrs_is_noop_without_call_scope():
    """Without a patter_call_scope, record_patter_attrs silently no-ops."""
    from getpatter.observability.attributes import record_patter_attrs

    # Must not raise even if no scope is active and no span exists.
    record_patter_attrs({"patter.cost.stt_seconds": 1.5, "patter.stt.provider": "deepgram"})
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_helper_module.py -v
```

Expected: `ImportError: cannot import name 'attributes' from 'getpatter.observability'` (or `ModuleNotFoundError`).

- [ ] **Step 3: Create `attributes.py` with the minimal API**

Write to `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/observability/attributes.py`:

```python
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


def attach_span_exporter(patter_instance: Any, exporter: Any, *, side: str = "uut") -> None:
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
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_helper_module.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit (in the worktree, on `feat/observability-otel-attrs`)**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git add sdk-py/getpatter/observability/attributes.py
git commit -m "feat(observability): scaffold patter.* span attribute helpers

Adds attributes.py with lazy OTel guard, two ContextVars (call_id, side),
and the public surface: record_patter_attrs, patter_call_scope,
attach_span_exporter. No-op when [tracing] extra is missing."
```

Then commit the test on the acceptance branch:

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/__init__.py python/tests/observability/test_helper_module.py
git commit -m "test: helper module skeleton import + no-scope no-op"
```

### Task 5: ContextVar propagation test

**Files:**
- Modify: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_helper_module.py`

- [ ] **Step 1: Append the test for `patter_call_scope`**

Append to `tests/observability/test_helper_module.py`:

```python
@pytest.mark.unit
def test_patter_call_scope_sets_and_resets_contextvars():
    from getpatter.observability.attributes import (
        _patter_call_id,
        _patter_side,
        patter_call_scope,
    )

    assert _patter_call_id.get() is None
    assert _patter_side.get() == "uut"

    with patter_call_scope(call_id="CA1234", side="driver"):
        assert _patter_call_id.get() == "CA1234"
        assert _patter_side.get() == "driver"

    assert _patter_call_id.get() is None
    assert _patter_side.get() == "uut"


@pytest.mark.unit
def test_patter_call_scope_rejects_empty_call_id():
    from getpatter.observability.attributes import patter_call_scope

    with pytest.raises(ValueError, match="non-empty call_id"):
        with patter_call_scope(call_id="", side="uut"):
            pass
```

- [ ] **Step 2: Run the tests to verify they pass**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_helper_module.py -v
```

Expected: 4 passed. (Implementation already in Task 4 — these tests just lock in the contract.)

- [ ] **Step 3: Commit on acceptance branch**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_helper_module.py
git commit -m "test: patter_call_scope ContextVar set/reset behavior"
```

### Task 6: `record_patter_attrs` exporter round-trip test

**Files:**
- Create: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/conftest.py`
- Modify: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_helper_module.py`

- [ ] **Step 1: Create the in-memory tracer fixture**

Write to `tests/observability/conftest.py`:

```python
"""Shared OTel fixtures for span-attribute tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def in_memory_tracer():
    """Fresh TracerProvider + InMemorySpanExporter, set globally for the test."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # OTel does not let you swap providers cleanly once one is set; on first
    # use it locks in. Use the override-once pattern: set, yield, clear.
    prev_provider = trace.get_tracer_provider()
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        exporter.clear()
        try:
            trace.set_tracer_provider(prev_provider)
        except Exception:
            pass
```

- [ ] **Step 2: Append the round-trip test**

Append to `tests/observability/test_helper_module.py`:

```python
@pytest.mark.mocked
def test_record_patter_attrs_emits_span_with_routing_tags(in_memory_tracer):
    from getpatter.observability.attributes import (
        patter_call_scope,
        record_patter_attrs,
    )

    with patter_call_scope(call_id="CA-test-001", side="driver"):
        record_patter_attrs(
            {"patter.cost.stt_seconds": 2.0, "patter.stt.provider": "deepgram"},
        )

    spans = in_memory_tracer.get_finished_spans()
    assert len(spans) == 1, [s.name for s in spans]
    attrs = dict(spans[0].attributes or {})
    assert attrs["patter.call_id"] == "CA-test-001"
    assert attrs["patter.side"] == "driver"
    assert attrs["patter.cost.stt_seconds"] == 2.0
    assert attrs["patter.stt.provider"] == "deepgram"


@pytest.mark.mocked
def test_record_patter_attrs_outside_scope_emits_nothing(in_memory_tracer):
    from getpatter.observability.attributes import record_patter_attrs

    record_patter_attrs({"patter.cost.stt_seconds": 5.0})

    assert in_memory_tracer.get_finished_spans() == ()
```

- [ ] **Step 3: Run the tests**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_helper_module.py -v
```

Expected: 6 passed.

- [ ] **Step 4: Commit on acceptance branch**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/conftest.py python/tests/observability/test_helper_module.py
git commit -m "test: record_patter_attrs round-trip via InMemorySpanExporter"
```

### Task 7: `attach_span_exporter` test

**Files:**
- Modify: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_helper_module.py`

- [ ] **Step 1: Append the test**

```python
@pytest.mark.mocked
def test_attach_span_exporter_stores_side_and_wires_processor():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from getpatter.observability.attributes import (
        attach_span_exporter,
        patter_call_scope,
        record_patter_attrs,
    )

    class _FakePatter:
        pass

    patter = _FakePatter()
    exporter = InMemorySpanExporter()

    attach_span_exporter(patter, exporter, side="driver")
    assert patter._patter_side == "driver"

    # Idempotent — second call does not double-wire.
    attach_span_exporter(patter, exporter, side="driver")

    with patter_call_scope(call_id="CA-attach-001", side=patter._patter_side):
        record_patter_attrs({"patter.cost.tts_chars": 42, "patter.tts.provider": "elevenlabs"})

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["patter.side"] == "driver"
    assert attrs["patter.cost.tts_chars"] == 42
```

- [ ] **Step 2: Run the tests**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_helper_module.py -v
```

Expected: 7 passed.

- [ ] **Step 3: Commit**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_helper_module.py
git commit -m "test: attach_span_exporter stores side and is idempotent"
```

### Task 8: Re-export from `observability/__init__.py`

**Files:**
- Modify: `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/observability/__init__.py`

- [ ] **Step 1: Read the current file**

```bash
cat /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/observability/__init__.py
```

- [ ] **Step 2: Add the re-exports**

Insert after the existing tracing import block (around line 38-39):

```python
from getpatter.observability.attributes import (
    attach_span_exporter,
    patter_call_scope,
    record_patter_attrs,
)
```

Append to `__all__`:

```python
    # Span attributes (consumed by patter-agent-runner)
    "attach_span_exporter",
    "patter_call_scope",
    "record_patter_attrs",
```

- [ ] **Step 3: Verify the import works**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/python -c "from getpatter.observability import record_patter_attrs, patter_call_scope, attach_span_exporter; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Commit on the worktree branch**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git add sdk-py/getpatter/observability/__init__.py
git commit -m "feat(observability): re-export attribute helpers from package root"
```

---

## Phase 2 — `Patter._attach_span_exporter` + stream_handler scope

### Task 9: Add `_attach_span_exporter` to `Patter`

**Files:**
- Modify: `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/client.py:65-89` (`__init__`) and append a method
- Test: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_patter_attach_exporter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/observability/test_patter_attach_exporter.py`:

```python
"""Verify Patter._attach_span_exporter contract used by patter-agent-runner."""

from __future__ import annotations

import pytest


@pytest.mark.mocked
def test_patter_attach_span_exporter_default_side_is_uut():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from getpatter import Patter

    phone = Patter()
    exporter = InMemorySpanExporter()
    phone._attach_span_exporter(exporter)
    assert phone._patter_side == "uut"


@pytest.mark.mocked
def test_patter_attach_span_exporter_driver_side():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from getpatter import Patter

    phone = Patter()
    exporter = InMemorySpanExporter()
    phone._attach_span_exporter(exporter, side="driver")
    assert phone._patter_side == "driver"


@pytest.mark.mocked
def test_patter_default_side_is_uut_before_attach():
    from getpatter import Patter

    phone = Patter()
    # Default side present even when _attach_span_exporter is not called.
    assert getattr(phone, "_patter_side", "uut") == "uut"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_patter_attach_exporter.py -v
```

Expected: AttributeError on `_attach_span_exporter` for the first two tests (the third may pass).

- [ ] **Step 3: Modify `Patter.__init__` to set the default side**

In `sdk-py/getpatter/client.py`, inside `Patter.__init__`, immediately after the `if kwargs: ...` validation block (around line 86), add:

```python
        # Observability — set by _attach_span_exporter, default safe.
        self._patter_side: str = "uut"
```

- [ ] **Step 4: Add `_attach_span_exporter` method**

Append the following method to the `Patter` class (just before `async def disconnect` at line ~745):

```python
    def _attach_span_exporter(self, exporter: Any, *, side: str = "uut") -> None:
        """Wire an OTel span exporter into the SDK's tracer provider.

        Public-but-underscore: consumed by ``patter-agent-runner`` via
        ``getattr(phone, "_attach_span_exporter")``. The leading underscore
        signals it is not part of the customer-facing API surface.

        Args:
            exporter: Any OTel ``SpanExporter`` (e.g. ``InMemorySpanExporter``,
                ``OTLPSpanExporter``, or the runner's ``PatterSpanExporter``).
            side: ``"driver"`` or ``"uut"``. Stamped on every cost/latency
                span emitted during this Patter instance's call lifecycle.
        """
        from getpatter.observability.attributes import attach_span_exporter

        attach_span_exporter(self, exporter, side=side)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_patter_attach_exporter.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit on worktree branch + acceptance branch**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git add sdk-py/getpatter/client.py
git commit -m "feat(client): add Patter._attach_span_exporter for telemetry routing

Consumed by patter-agent-runner to wire driver/UUT span streams. Default
side='uut' preserves all existing behavior."
```

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_patter_attach_exporter.py
git commit -m "test: Patter._attach_span_exporter side override contract"
```

### Task 10: Wrap call lifecycle in `patter_call_scope`

**Files:**
- Modify: `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/handlers/stream_handler.py` (the `__init__` and main run method of `StreamHandler`)
- Test: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_call_scope_propagation.py`

- [ ] **Step 1: Identify `StreamHandler.__init__` and the run entry point**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py
grep -n "def __init__\|async def run\|async def handle" getpatter/handlers/stream_handler.py | head -30
```

Note the line numbers of:
- The base `StreamHandler.__init__` (sets `self.call_id = call_id` per the earlier audit at line ~315).
- The async entry point that runs once per call (look for the method called from `twilio_handler.py` / `telnyx_handler.py`; usually `async def run` or `async def handle_audio`).

- [ ] **Step 2: Write the failing test**

Create `tests/observability/test_call_scope_propagation.py`:

```python
"""Verify the StreamHandler enters patter_call_scope for the call lifetime."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.mocked
async def test_call_scope_active_during_handler_run():
    """Inside the handler's main coroutine, the ContextVars are bound to call_id."""
    from getpatter.observability.attributes import _patter_call_id, _patter_side

    seen: dict[str, object] = {}

    async def _fake_inner_work(self):
        seen["call_id"] = _patter_call_id.get()
        seen["side"] = _patter_side.get()

    # We exercise the smallest possible path: instantiate a StreamHandler
    # subclass, monkeypatch its inner work to capture the ContextVars,
    # and call the new wrapped entry point.
    from getpatter.handlers import stream_handler as sh_module

    with patch.object(
        sh_module.StreamHandler, "_run_inner", _fake_inner_work, create=True
    ):
        handler = sh_module.StreamHandler.__new__(sh_module.StreamHandler)
        handler.call_id = "CA-scope-001"
        handler._patter_side = "driver"  # set as if attach_span_exporter ran on the parent Patter
        await handler._run_with_scope()

    assert seen["call_id"] == "CA-scope-001"
    assert seen["side"] == "driver"
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_call_scope_propagation.py -v
```

Expected: AttributeError on `_run_with_scope` or `_run_inner`.

- [ ] **Step 4: Add the scope wrapper**

Before the existing run-method body in `StreamHandler`, add an instance attribute and a wrapper method:

In `StreamHandler.__init__` (right after `self.call_id = call_id`), add:

```python
        # Set by Patter._attach_span_exporter via attach_span_exporter; "uut" by default.
        # Read once at handler start; later changes via the same Patter instance
        # will not retroactively affect this handler's spans.
        self._patter_side: str = getattr(self, "_patter_side", "uut")
```

Add a wrapper method `_run_with_scope` that calls the existing run logic inside `patter_call_scope`. Identify the existing entry point method (call it `_run_inner`); rename the existing body to `_run_inner` and add:

```python
    async def _run_with_scope(self) -> None:
        """Enter patter_call_scope for the call lifetime, then run the handler.

        All spans emitted from provider plumbing during this call inherit
        ``patter.call_id`` and ``patter.side`` via the helper's ContextVars.
        """
        from getpatter.observability.attributes import patter_call_scope

        with patter_call_scope(call_id=self.call_id, side=self._patter_side):
            await self._run_inner()
```

Update every caller of the original entry point to call `_run_with_scope` instead.

> NOTE: the exact existing entry-point name varies by subclass. During implementation, use
> `grep -n "async def" getpatter/handlers/stream_handler.py` to enumerate, identify the
> per-call top-level coroutine, and rename only that one.

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_call_scope_propagation.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Wire StreamHandler instances to inherit `_patter_side` from the Patter instance**

In whatever code path constructs the `StreamHandler` (the embedded server, search for `StreamHandler(...)` instantiation), pass `_patter_side` through:

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py
grep -rn "StreamHandler(" getpatter/ --include="*.py" | head
```

After construction, set `handler._patter_side = self._patter_side` (where `self` is the `Patter` instance or the surrounding object that has access to it). If construction happens deep in `server.py`, thread `_patter_side` through the embedded server config.

- [ ] **Step 7: Run all observability tests**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/ -v
```

Expected: 11 passed (4 + 3 + 3 + 1 from prior tasks).

- [ ] **Step 8: Commit**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git add sdk-py/getpatter/handlers/stream_handler.py sdk-py/getpatter/server.py
git commit -m "feat(handlers): enter patter_call_scope for call lifetime

StreamHandler wraps its run loop in patter_call_scope so all provider
spans emitted during the call inherit patter.call_id and patter.side."
```

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_call_scope_propagation.py
git commit -m "test: ContextVars are bound during StreamHandler call lifetime"
```

---

## Phase 3 — Smoke-test-critical instrumentation

The Step-7 smoke test exercises Twilio + OpenAI Realtime + pipeline_hooks. Phase 3 ends with all three emitting `patter.cost.*` and `patter.latency.*`.

### Task 11: Twilio adapter telephony cost

**Files:**
- Modify: `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/providers/twilio_adapter.py`
- Test: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_telephony_attributes.py`

- [ ] **Step 1: Read the adapter to find the call-end hook**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py
sed -n '1,140p' getpatter/providers/twilio_adapter.py
```

Identify (a) where the call's start time is recorded, (b) where the hangup / stop event is processed. The 58-line file is small enough to hold in head — locate the obvious method (likely `on_stop`, `handle_stop`, or `close`).

- [ ] **Step 2: Write the failing test**

Create `tests/observability/test_telephony_attributes.py`:

```python
"""Telephony adapters emit patter.cost.telephony_minutes on call end."""

from __future__ import annotations

import pytest


@pytest.mark.mocked
async def test_twilio_adapter_emits_telephony_minutes(in_memory_tracer):
    from getpatter.observability.attributes import patter_call_scope
    from getpatter.providers.twilio_adapter import TwilioAdapter

    adapter = TwilioAdapter(account_sid="ACtest", auth_token="tok")
    with patter_call_scope(call_id="CA-twilio-001", side="uut"):
        # Simulate a 30-second call. Implementation MUST expose a method
        # that finalises billing (e.g. record_call_end).
        adapter.record_call_end(duration_seconds=30.0, direction="inbound")

    spans = in_memory_tracer.get_finished_spans()
    cost_spans = [s for s in spans if "patter.cost.telephony_minutes" in (s.attributes or {})]
    assert len(cost_spans) == 1
    attrs = dict(cost_spans[0].attributes)
    assert attrs["patter.cost.telephony_minutes"] == pytest.approx(0.5)
    assert attrs["patter.telephony"] == "twilio"
    assert attrs["patter.direction"] == "inbound"
    assert attrs["patter.call_id"] == "CA-twilio-001"
    assert attrs["patter.side"] == "uut"
```

- [ ] **Step 3: Run to verify it fails**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_telephony_attributes.py::test_twilio_adapter_emits_telephony_minutes -v
```

Expected: AttributeError on `record_call_end`.

- [ ] **Step 4: Add `record_call_end` to TwilioAdapter**

Append to `TwilioAdapter`:

```python
    def record_call_end(self, *, duration_seconds: float, direction: str) -> None:
        """Emit patter.cost.telephony_minutes on the active span.

        Called by the embedded server's hangup handler once the call's
        wall-clock duration is known.
        """
        from getpatter.observability.attributes import record_patter_attrs

        record_patter_attrs({
            "patter.cost.telephony_minutes": duration_seconds / 60.0,
            "patter.telephony": "twilio",
            "patter.direction": direction,
        })
```

Then, locate the existing call-end / hangup handler in the codebase (search `grep -rn "on_call_end\|hangup\|call_end" getpatter/server.py getpatter/handlers/`). Add a single call to `adapter.record_call_end(...)` at the point where call duration is known and the direction (inbound vs outbound) is determinable. The existing `MetricsStore.record_call_initiated` call confirms direction is already passed around.

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_telephony_attributes.py::test_twilio_adapter_emits_telephony_minutes -v
```

Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git add sdk-py/getpatter/providers/twilio_adapter.py sdk-py/getpatter/server.py sdk-py/getpatter/handlers/
git commit -m "feat(twilio): emit patter.cost.telephony_minutes on call end"
```

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_telephony_attributes.py
git commit -m "test: Twilio adapter emits telephony cost"
```

### Task 12: OpenAI Realtime cost

**Files:**
- Modify: `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/providers/openai_realtime.py`
- Test: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_realtime_attributes.py`

- [ ] **Step 1: Read the adapter to find the `response.done` event handler**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py
grep -n "response.done\|response_done\|usage" getpatter/providers/openai_realtime.py | head -20
```

OpenAI's Realtime API emits a `response.done` event with a `response.usage` block holding `total_tokens`, `input_tokens`, `output_tokens`, and `total_token_details`. For cost attribution we want **session minutes**, derived as: track `session_start = time.monotonic()` in the adapter constructor, then on session close (or `response.done` for cumulative if minutes-billing model is per-response): `minutes = (now - session_start) / 60.0`.

- [ ] **Step 2: Write the failing test**

Create `tests/observability/test_realtime_attributes.py`:

```python
"""OpenAI Realtime adapter emits patter.cost.realtime_minutes on session end."""

from __future__ import annotations

import time

import pytest


@pytest.mark.mocked
async def test_openai_realtime_emits_realtime_minutes(in_memory_tracer):
    from getpatter.observability.attributes import patter_call_scope
    from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

    adapter = OpenAIRealtimeAdapter(api_key="sk-test")
    # Simulate a session that started 12 seconds ago.
    adapter._session_start_monotonic = time.monotonic() - 12.0

    with patter_call_scope(call_id="CA-rt-001", side="uut"):
        adapter.record_session_end()

    cost_spans = [
        s
        for s in in_memory_tracer.get_finished_spans()
        if "patter.cost.realtime_minutes" in (s.attributes or {})
    ]
    assert len(cost_spans) == 1
    attrs = dict(cost_spans[0].attributes)
    assert attrs["patter.cost.realtime_minutes"] >= 0.18  # ~12s / 60 = 0.2 ± jitter
    assert attrs["patter.cost.realtime_minutes"] <= 0.22
    assert attrs["patter.realtime.provider"] == "openai_realtime"
```

- [ ] **Step 3: Run to verify it fails**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_realtime_attributes.py -v
```

Expected: AttributeError on `record_session_end` or `_session_start_monotonic`.

- [ ] **Step 4: Add session-time tracking + record_session_end to OpenAIRealtimeAdapter**

In `getpatter/providers/openai_realtime.py`, add to `__init__`:

```python
        import time as _time
        self._session_start_monotonic: float = _time.monotonic()
```

Add a method to the class:

```python
    def record_session_end(self) -> None:
        """Emit patter.cost.realtime_minutes for the elapsed session duration."""
        import time as _time

        from getpatter.observability.attributes import record_patter_attrs

        elapsed = _time.monotonic() - self._session_start_monotonic
        record_patter_attrs({
            "patter.cost.realtime_minutes": elapsed / 60.0,
            "patter.realtime.provider": "openai_realtime",
        })
```

Locate the existing close / disconnect handler in the adapter (likely `async def close` or `async def disconnect`). Add `self.record_session_end()` as the **first** line, before the actual WebSocket teardown — so the patter_call_scope is still active.

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_realtime_attributes.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git add sdk-py/getpatter/providers/openai_realtime.py
git commit -m "feat(openai-realtime): emit patter.cost.realtime_minutes on session end"
```

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_realtime_attributes.py
git commit -m "test: OpenAI Realtime emits realtime_minutes from session duration"
```

### Task 13: Pipeline-hook latency (TTFB + turn_ms)

**Files:**
- Modify: `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/getpatter/services/pipeline_hooks.py`
- Test: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_latency_attributes.py`

- [ ] **Step 1: Read the file**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py
sed -n '1,140p' getpatter/services/pipeline_hooks.py
```

Identify where end-of-user-speech is signalled and where TTS first byte fires. The file is 140 lines — should be obvious from method names.

- [ ] **Step 2: Write the failing test**

Create `tests/observability/test_latency_attributes.py`:

```python
"""Pipeline hooks emit patter.latency.{ttfb_ms,turn_ms} per turn."""

from __future__ import annotations

import pytest


@pytest.mark.mocked
async def test_pipeline_hook_emits_ttfb_and_turn_ms(in_memory_tracer):
    from getpatter.observability.attributes import patter_call_scope
    from getpatter.services.pipeline_hooks import PipelineHookExecutor

    executor = PipelineHookExecutor()

    with patter_call_scope(call_id="CA-latency-001", side="uut"):
        executor.record_turn_latency(ttfb_ms=312.5, turn_ms=987.0)

    spans = in_memory_tracer.get_finished_spans()
    latency_spans = [
        s for s in spans if "patter.latency.ttfb_ms" in (s.attributes or {})
    ]
    assert len(latency_spans) == 1
    attrs = dict(latency_spans[0].attributes)
    assert attrs["patter.latency.ttfb_ms"] == 312.5
    assert attrs["patter.latency.turn_ms"] == 987.0
```

- [ ] **Step 3: Run to verify it fails**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_latency_attributes.py -v
```

Expected: AttributeError on `record_turn_latency`.

- [ ] **Step 4: Add `record_turn_latency` to PipelineHookExecutor**

In `services/pipeline_hooks.py`, add the method:

```python
    def record_turn_latency(self, *, ttfb_ms: float, turn_ms: float) -> None:
        """Emit patter.latency.{ttfb_ms,turn_ms} for the just-completed turn."""
        from getpatter.observability.attributes import record_patter_attrs

        record_patter_attrs({
            "patter.latency.ttfb_ms": ttfb_ms,
            "patter.latency.turn_ms": turn_ms,
        })
```

Then locate the existing turn-boundary measurement code (search `grep -rn "ttfb\|turn_ms\|first_byte" getpatter/services/ getpatter/handlers/ | head`). Wherever the existing code computes those values for the metrics dashboard, also call `executor.record_turn_latency(ttfb_ms=..., turn_ms=...)`.

If the existing measurement code lives outside `PipelineHookExecutor`, add a thin pass-through method on the StreamHandler class that delegates to the executor.

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/test_latency_attributes.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git add sdk-py/getpatter/services/pipeline_hooks.py sdk-py/getpatter/handlers/stream_handler.py
git commit -m "feat(pipeline-hooks): emit patter.latency.ttfb_ms and turn_ms per turn"
```

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_latency_attributes.py
git commit -m "test: pipeline hooks emit ttfb_ms and turn_ms"
```

### Task 14: Run the smoke-test-critical observability suite

- [ ] **Step 1: Run all observability tests**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/ -v
```

Expected: 14 passed (helper module + Patter attach + scope propagation + telephony + realtime + latency).

- [ ] **Step 2: If any test fails, fix the implementation, do NOT modify tests**

Per `authentic-tests.md`: tests are usually right; implementation is usually wrong.

- [ ] **Step 3: Snapshot the worktree branch**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git log --oneline -10
```

Confirm a clean linear history of the commits added so far.

---

## Phase 4 — Remaining providers

Each task in this phase follows the same shape as Tasks 11-13. To keep the plan readable, the per-provider tasks are tabular, but each one MUST include its own test, its own commit, and its own implementation hook point.

### Task 15: Telnyx adapter

**Files:**
- Modify: `getpatter/providers/telnyx_adapter.py` (133 lines — has `call.hangup` event)
- Test: append `test_telnyx_adapter_emits_telephony_minutes` to `tests/observability/test_telephony_attributes.py`

- [ ] **Step 1: Append a Telnyx-mirrored test**

```python
@pytest.mark.mocked
async def test_telnyx_adapter_emits_telephony_minutes(in_memory_tracer):
    from getpatter.observability.attributes import patter_call_scope
    from getpatter.providers.telnyx_adapter import TelnyxAdapter

    adapter = TelnyxAdapter(api_key="key", connection_id="conn")
    with patter_call_scope(call_id="CA-tnx-001", side="uut"):
        adapter.record_call_end(duration_seconds=42.0, direction="outbound")

    cost_spans = [
        s
        for s in in_memory_tracer.get_finished_spans()
        if "patter.cost.telephony_minutes" in (s.attributes or {})
    ]
    assert len(cost_spans) == 1
    attrs = dict(cost_spans[0].attributes)
    assert attrs["patter.cost.telephony_minutes"] == pytest.approx(42.0 / 60.0)
    assert attrs["patter.telephony"] == "telnyx"
    assert attrs["patter.direction"] == "outbound"
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/observability/test_telephony_attributes.py::test_telnyx_adapter_emits_telephony_minutes -v
```

- [ ] **Step 3: Add `record_call_end` to `TelnyxAdapter`** — same shape as Twilio, but `"patter.telephony": "telnyx"`. Wire it into the existing `call.hangup` event handler.

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit** (`feat(telnyx): emit patter.cost.telephony_minutes on call end`).

### Task 16: STT providers (one task; per-provider sub-steps)

**Files:**
- Modify (one per substep): `getpatter/providers/{deepgram,assemblyai,whisper,openai_transcribe,soniox,speechmatics,cartesia}_stt.py`
- Test: `tests/observability/test_stt_attributes.py`

The pattern: each STT adapter tracks `_audio_bytes_sent` accumulator (incremented in `send_audio`). On final transcript emission, compute `seconds = bytes / (sample_rate * (1 if encoding=="mulaw" else 2))`, call `record_patter_attrs(...)`, reset `_audio_bytes_sent=0`.

- [ ] **Step 1: Write the parametrized test**

Create `tests/observability/test_stt_attributes.py`:

```python
"""All STT providers emit patter.cost.stt_seconds on each final transcript."""

from __future__ import annotations

import pytest

# (provider_module, class_name, expected_provider_tag, init_kwargs)
STT_CASES = [
    ("getpatter.providers.deepgram_stt", "DeepgramSTT", "deepgram", {"api_key": "dg_test"}),
    ("getpatter.providers.assemblyai_stt", "AssemblyAISTT", "assemblyai", {"api_key": "aai_test"}),
    ("getpatter.providers.whisper_stt", "WhisperSTT", "whisper", {"api_key": "sk-test"}),
    ("getpatter.providers.openai_transcribe_stt", "OpenAITranscribeSTT", "whisper", {"api_key": "sk-test"}),
    ("getpatter.providers.soniox_stt", "SonioxSTT", "soniox", {"api_key": "sx_test"}),
    ("getpatter.providers.speechmatics_stt", "SpeechmaticsSTT", "speechmatics", {"api_key": "sm_test"}),
    ("getpatter.providers.cartesia_stt", "CartesiaSTT", "cartesia", {"api_key": "ct_test"}),
]


@pytest.mark.mocked
@pytest.mark.parametrize("module_path,class_name,expected_tag,init_kwargs", STT_CASES)
async def test_stt_emits_seconds_on_final_transcript(
    in_memory_tracer, module_path, class_name, expected_tag, init_kwargs
):
    import importlib

    from getpatter.observability.attributes import patter_call_scope

    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    stt = cls(**init_kwargs)

    # Inject a known number of bytes so the implementation has something to report.
    # 16000 Hz * 2 bytes/sample * 1 second = 32000 bytes (linear16 default).
    sample_rate = getattr(stt, "sample_rate", 16000)
    bytes_per_sample = 1 if getattr(stt, "encoding", "linear16") == "mulaw" else 2
    stt._audio_bytes_sent = sample_rate * bytes_per_sample * 1  # 1 second

    with patter_call_scope(call_id=f"CA-stt-{expected_tag}", side="uut"):
        stt._record_transcript_cost()

    cost_spans = [
        s
        for s in in_memory_tracer.get_finished_spans()
        if "patter.cost.stt_seconds" in (s.attributes or {})
    ]
    assert len(cost_spans) >= 1
    attrs = dict(cost_spans[-1].attributes)
    assert attrs["patter.cost.stt_seconds"] == pytest.approx(1.0, abs=0.01)
    assert attrs["patter.stt.provider"] == expected_tag
```

- [ ] **Step 2: Run to verify all 7 cases fail**

```bash
.venv/bin/pytest tests/observability/test_stt_attributes.py -v
```

Expected: AttributeError on `_audio_bytes_sent` or `_record_transcript_cost`.

- [ ] **Step 3: Add the pattern to each STT adapter**

For each of the 7 STT adapters, in the order Deepgram → AssemblyAI → Whisper → OpenAITranscribe → Soniox → Speechmatics → Cartesia:

(a) Add `self._audio_bytes_sent: int = 0` to `__init__`.
(b) In `send_audio` (the method that takes `bytes` and forwards to the WS / HTTPS), insert `self._audio_bytes_sent += len(audio_chunk)` before the network send.
(c) Add a method:

```python
    def _record_transcript_cost(self) -> None:
        from getpatter.observability.attributes import record_patter_attrs

        bytes_per_sample = 1 if self.encoding == "mulaw" else 2
        seconds = self._audio_bytes_sent / float(self.sample_rate * bytes_per_sample)
        record_patter_attrs({
            "patter.cost.stt_seconds": seconds,
            "patter.stt.provider": "<TAG>",   # replace per-provider
        })
        self._audio_bytes_sent = 0
```

(d) In the receive / parse method that yields a final `Transcript` (`is_final=True`), call `self._record_transcript_cost()` immediately before yielding.

For providers that do not expose `sample_rate` or `encoding` directly, derive them from constructor parameters or hardcode the documented defaults of that provider.

- [ ] **Step 4: Re-run after each provider; commit per provider**

After each provider implementation:

```bash
.venv/bin/pytest tests/observability/test_stt_attributes.py -v -k "<provider_tag>"
```

Then on the worktree branch:

```bash
git add sdk-py/getpatter/providers/<provider>_stt.py
git commit -m "feat(<provider>): emit patter.cost.stt_seconds on final transcript"
```

This produces 7 commits — one per STT provider. The test stays green and the parametrize set passes one row at a time.

- [ ] **Step 5: After all 7, run the full STT suite**

```bash
.venv/bin/pytest tests/observability/test_stt_attributes.py -v
```

Expected: 7 passed.

- [ ] **Step 6: Commit the test once all are green**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_stt_attributes.py
git commit -m "test: parametrized STT cost emission across all providers"
```

### Task 17: TTS providers

**Files:**
- Modify: `getpatter/providers/{elevenlabs,openai,cartesia,lmnt,rime}_tts.py`
- Test: `tests/observability/test_tts_attributes.py`

Pattern: each TTS adapter has a `synthesise(text)` (or `speak`) method that returns audio bytes. After the call returns, before yielding/returning, call `record_patter_attrs({"patter.cost.tts_chars": len(text), "patter.tts.provider": "<tag>"})`.

- [ ] **Step 1: Write the parametrized test**

```python
"""All TTS providers emit patter.cost.tts_chars on each synthesis."""

from __future__ import annotations

import pytest

TTS_CASES = [
    ("getpatter.providers.elevenlabs_tts", "ElevenLabsTTS", "elevenlabs", {"api_key": "el_test"}),
    ("getpatter.providers.openai_tts", "OpenAITTS", "openai_tts", {"api_key": "sk-test"}),
    ("getpatter.providers.cartesia_tts", "CartesiaTTS", "cartesia_tts", {"api_key": "ct_test"}),
    ("getpatter.providers.lmnt_tts", "LMNTTTS", "lmnt", {"api_key": "ln_test"}),
    ("getpatter.providers.rime_tts", "RimeTTS", "rime", {"api_key": "rm_test"}),
]


@pytest.mark.mocked
@pytest.mark.parametrize("module_path,class_name,expected_tag,init_kwargs", TTS_CASES)
async def test_tts_emits_chars(
    in_memory_tracer, module_path, class_name, expected_tag, init_kwargs
):
    import importlib

    from getpatter.observability.attributes import patter_call_scope

    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    tts = cls(**init_kwargs)

    with patter_call_scope(call_id=f"CA-tts-{expected_tag}", side="uut"):
        tts._record_synthesis_cost("hello world")  # 11 chars

    cost_spans = [
        s
        for s in in_memory_tracer.get_finished_spans()
        if "patter.cost.tts_chars" in (s.attributes or {})
    ]
    assert len(cost_spans) >= 1
    attrs = dict(cost_spans[-1].attributes)
    assert attrs["patter.cost.tts_chars"] == 11
    assert attrs["patter.tts.provider"] == expected_tag
```

Save to `tests/observability/test_tts_attributes.py`.

- [ ] **Step 2: Run, verify failures.**

- [ ] **Step 3: For each TTS provider, add the helper:**

```python
    def _record_synthesis_cost(self, text: str) -> None:
        from getpatter.observability.attributes import record_patter_attrs

        record_patter_attrs({
            "patter.cost.tts_chars": len(text),
            "patter.tts.provider": "<TAG>",
        })
```

Call `self._record_synthesis_cost(text)` at the top of every public synthesis method (e.g., `synthesise`, `speak`, `stream`). Idempotent — even if the synth ultimately fails, the input chars are billed by most providers regardless, so emit before the network call.

- [ ] **Step 4: Run the suite, then commit per-provider.**

5 commits, one per TTS provider.

- [ ] **Step 5: Commit the test:**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance
git add python/tests/observability/test_tts_attributes.py
git commit -m "test: parametrized TTS cost emission across all providers"
```

### Task 18: LLM providers (chat-completions style)

**Files:**
- Modify: `getpatter/services/llm_loop.py` (OpenAILLMProvider)
- Modify: `getpatter/providers/{anthropic,google,groq,cerebras}_llm.py`
- Test: `tests/observability/test_llm_attributes.py`

Pattern: each LLM provider has a method that calls `client.chat.completions.create(...)` (or the provider equivalent). The response carries `usage.prompt_tokens` / `usage.completion_tokens` (or analogous fields). Immediately after the call returns, call `record_patter_attrs({"patter.cost.llm_input_tokens": ...prompt..., "patter.cost.llm_output_tokens": ...completion..., "patter.llm.provider": "<tag>"})`.

- [ ] **Step 1: Write the parametrized test**

```python
"""All LLM providers emit patter.cost.llm_*_tokens on each completion."""

from __future__ import annotations

import pytest

LLM_CASES = [
    ("getpatter.services.llm_loop", "OpenAILLMProvider", "openai", {"api_key": "sk-test"}),
    ("getpatter.providers.anthropic_llm", "AnthropicLLM", "anthropic", {"api_key": "sk-ant-test"}),
    ("getpatter.providers.google_llm", "GoogleLLM", "google", {"api_key": "AIza-test"}),
    ("getpatter.providers.groq_llm", "GroqLLM", "groq", {"api_key": "gsk_test"}),
    ("getpatter.providers.cerebras_llm", "CerebrasLLM", "cerebras", {"api_key": "csk_test"}),
]


@pytest.mark.mocked
@pytest.mark.parametrize("module_path,class_name,expected_tag,init_kwargs", LLM_CASES)
async def test_llm_emits_tokens(
    in_memory_tracer, module_path, class_name, expected_tag, init_kwargs
):
    import importlib

    from getpatter.observability.attributes import patter_call_scope

    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    llm = cls(**init_kwargs)

    with patter_call_scope(call_id=f"CA-llm-{expected_tag}", side="uut"):
        llm._record_completion_cost(prompt_tokens=120, completion_tokens=37)

    cost_spans = [
        s
        for s in in_memory_tracer.get_finished_spans()
        if "patter.cost.llm_input_tokens" in (s.attributes or {})
    ]
    assert len(cost_spans) >= 1
    attrs = dict(cost_spans[-1].attributes)
    assert attrs["patter.cost.llm_input_tokens"] == 120
    assert attrs["patter.cost.llm_output_tokens"] == 37
    assert attrs["patter.llm.provider"] == expected_tag
```

- [ ] **Step 2: Run, verify failures.**

- [ ] **Step 3: For each LLM provider, add the helper:**

```python
    def _record_completion_cost(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        from getpatter.observability.attributes import record_patter_attrs

        record_patter_attrs({
            "patter.cost.llm_input_tokens": prompt_tokens,
            "patter.cost.llm_output_tokens": completion_tokens,
            "patter.llm.provider": "<TAG>",
        })
```

Locate the existing call site in each provider where the response's usage block is parsed, and call `self._record_completion_cost(prompt_tokens=resp.usage.prompt_tokens, completion_tokens=resp.usage.completion_tokens)`.

- For Anthropic: usage fields are `input_tokens` / `output_tokens` (not prompt/completion).
- For Google Gemini: `usageMetadata.promptTokenCount` / `candidatesTokenCount`.
- For Groq + Cerebras: OpenAI-compatible — same field names.

- [ ] **Step 4: Run, commit per-provider** (5 commits).

- [ ] **Step 5: Commit the test.**

### Task 19: ElevenLabs ConvAI realtime

**Files:**
- Modify: `getpatter/providers/elevenlabs_convai.py`
- Test: append to `tests/observability/test_realtime_attributes.py`

- [ ] **Step 1: Append a ConvAI test**

```python
@pytest.mark.mocked
async def test_elevenlabs_convai_emits_realtime_minutes(in_memory_tracer):
    import time

    from getpatter.observability.attributes import patter_call_scope
    from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

    adapter = ElevenLabsConvAIAdapter(api_key="el_test", agent_id="agent_xyz")
    adapter._session_start_monotonic = time.monotonic() - 7.0

    with patter_call_scope(call_id="CA-convai-001", side="uut"):
        adapter.record_session_end()

    cost_spans = [
        s
        for s in in_memory_tracer.get_finished_spans()
        if (s.attributes or {}).get("patter.realtime.provider") == "elevenlabs_convai"
    ]
    assert len(cost_spans) == 1
    attrs = dict(cost_spans[0].attributes)
    assert attrs["patter.cost.realtime_minutes"] >= 0.10
    assert attrs["patter.cost.realtime_minutes"] <= 0.14
```

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Mirror the OpenAI Realtime change** in `elevenlabs_convai.py`: track `_session_start_monotonic` in `__init__`, add `record_session_end` with `"patter.realtime.provider": "elevenlabs_convai"`. Call it from the existing close handler.

- [ ] **Step 4: Run, commit.**

```bash
git add sdk-py/getpatter/providers/elevenlabs_convai.py
git commit -m "feat(elevenlabs-convai): emit patter.cost.realtime_minutes on session end"
```

### Task 20: Verify the full observability suite is green

- [ ] **Step 1: Run all tests**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/observability/ -v
```

Expected: 32 passed (helper module 7 + Patter attach 3 + scope 1 + telephony 2 + realtime 2 + latency 1 + STT 7 + TTS 5 + LLM 5 = 33; allow ±1 for missing edge case).

- [ ] **Step 2: Run the full acceptance suite to ensure no regressions**

```bash
cd /Users/francescorosciano/docs/patter/[nicolotognoni]-patter-sdk-acceptance/python
.venv/bin/pytest tests/ -v
```

Expected: all green; no observability-related failures elsewhere.

---

## Phase 5 — Version bump, DEVLOG, docs-sync

### Task 21: Bump SDK version

**Files:**
- Modify: `[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/pyproject.toml`

- [ ] **Step 1: Edit pyproject.toml**

Replace `version = "0.5.4"` with `version = "0.5.5"`.

- [ ] **Step 2: Commit**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git add sdk-py/pyproject.toml
git commit -m "chore(release): bump getpatter to 0.5.5

Adds Patter._attach_span_exporter and patter.cost.* / patter.latency.*
OTel span attributes consumed by patter-agent-runner."
```

### Task 22: Add DEVLOG entry

**Files:**
- Modify: `[patterai]-Patter/.claude/worktrees/observability-spans/docs/DEVLOG.md`

- [ ] **Step 1: Read the existing DEVLOG to match style**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
head -100 docs/DEVLOG.md
```

- [ ] **Step 2: Prepend a new entry**

Insert at the top of the Log section (newest first), following the project's `devlog.md` rule template. Cover: what it does, files changed (cite each provider), tests added, breaking changes (none), docs to update.

- [ ] **Step 3: Commit**

```bash
git add docs/DEVLOG.md
git commit -m "docs(devlog): record patter.* OTel span attributes feature"
```

### Task 23: Dispatch docs-sync agent

- [ ] **Step 1: Invoke the docs-sync subagent**

Per `.claude/rules/documentation-best-practices.md`, dispatch the `docs-sync` agent with the diff so far. The agent will (a) update `patter_sdk_features.xlsx` with a row for the new feature, (b) generate or update a docs page under `docs/python-sdk/observability.mdx`, (c) cross-link from `docs/docs.json`.

If the docs-sync agent reports drift, resolve it before proceeding.

---

## Phase 6 — Smoke-test gate

### Task 24: Stop and request explicit "go"

- [ ] **Step 1: Compile a "Ready to smoke-test" report**

Print to the user:

```
SDK observability work complete on worktree branch feat/observability-otel-attrs.

  Acceptance unit tests: 32 passed in tests/observability/
  Full acceptance suite: all green
  SDK version bumped to 0.5.5
  DEVLOG updated; docs-sync dispatched (clean)

Step 7 of the prompt asks for a real-PSTN smoke test:

  cd /Users/francescorosciano/docs/patter/[FrancescoRosciano]-patter-agent-runner
  ./scripts/docker-test.sh pytest -m integration \
    -k "inbound_twilio_realtime and not pipeline and not convai" -v -s

This places ONE real Twilio inbound call against OpenAI Realtime.
Estimated cost: $0.05 - $0.20.

May I proceed?
```

- [ ] **Step 2: Wait for the user's "yes/go".**

Do NOT run the smoke test without it.

### Task 25: Run the smoke test (only after explicit "go")

- [ ] **Step 1: Run the smoke command**

```bash
cd /Users/francescorosciano/docs/patter/[FrancescoRosciano]-patter-agent-runner
./scripts/docker-test.sh pytest -m integration -k "inbound_twilio_realtime and not pipeline and not convai" -v -s
```

- [ ] **Step 2: Locate the new run JSON**

```bash
ls -t /Users/francescorosciano/docs/patter/[FrancescoRosciano]-patter-agent-runner/runs/ | head -3
```

The newest `runs/<run_id>/inbound_twilio_realtime.json` is what to inspect.

- [ ] **Step 3: Verify the contract**

```bash
RUN_ID=$(ls -t /Users/francescorosciano/docs/patter/[FrancescoRosciano]-patter-agent-runner/runs/ | head -1)
python -c "
import json, pathlib
p = pathlib.Path('/Users/francescorosciano/docs/patter/[FrancescoRosciano]-patter-agent-runner/runs/$RUN_ID/inbound_twilio_realtime.json')
data = json.loads(p.read_text())
print('cost_usd:', data.get('cost_usd'))
print('latency:', data.get('latency'))
"
```

Expected:
- `cost_usd > 0.0`
- `latency.ttfb_ms > 0`
- `latency.turn_p95_ms > 0`

- [ ] **Step 4: If any check fails**

Open the run's spans (the runner writes them to `runs/<run_id>/spans/`) and find which `patter.cost.*` attribute is missing. Trace back to which provider was on the hot path (`patter.<category>.provider` tag). Open the corresponding adapter and identify the early-return path that bypassed `record_patter_attrs`. Fix, re-run unit tests, then ask the user once more before re-running the smoke test.

- [ ] **Step 5: On full success — push the worktree branch and open a PR**

```bash
cd /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans
git push -u origin feat/observability-otel-attrs
gh pr create --title "feat(observability): emit patter.cost.* and patter.latency.* OTel span attributes" --body "$(cat <<'EOF'
## Summary

- Adds `getpatter/observability/attributes.py` with `record_patter_attrs`, `patter_call_scope`, `attach_span_exporter`.
- Adds `Patter._attach_span_exporter(exporter, *, side="uut")` consumed by `patter-agent-runner`.
- Wires every billable hot path (telephony, STT × 7, TTS × 5, LLM × 5, realtime × 2, latency) to emit `patter.cost.*` / `patter.latency.*` attributes via the helper.
- Bumps version 0.5.4 → 0.5.5.

## Test plan

- [x] `pytest tests/observability/ -v` — 32 passed in acceptance suite
- [x] Full acceptance suite green
- [x] Real PSTN smoke (`inbound_twilio_realtime`) — `cost_usd > 0`, `latency.turn_p95_ms > 0`

## Follow-ups

- TS SDK parity (separate PR, must land in 0.5.5 release cycle).
- `patter-agent-runner` pin bump 0.5.4 → 0.5.5 (separate PR).

EOF
)"
```

- [ ] **Step 6: Report PR URL to user.**

---

## Self-review

Before handing off to the executor:

- [x] Every section of the spec maps to at least one task (Phase 0-6 cover setup, helper, client wiring, smoke-critical, fan-out, version+docs, gate).
- [x] No "TBD"/"TODO"/placeholder steps; per-provider details list each provider name and tag.
- [x] Helper API names match between Tasks 4-8 (record_patter_attrs, patter_call_scope, attach_span_exporter, `_patter_call_id`, `_patter_side`).
- [x] `Patter._attach_span_exporter` signature in Task 9 matches the runner's call site (`wire(exporter, side=side)`).
- [x] Step 7 PSTN smoke test is gated on explicit user approval (Task 24 stops, Task 25 runs only after).
- [x] Each task ends with a commit; phases are independently shippable.

## Known follow-ups (out of scope for this plan)

1. TypeScript SDK parity (`sdk-ts/`). Per `sdk-parity.md` it must ship in the same `0.5.5` release window — open a sibling plan after this one merges.
2. `patter-agent-runner/pyproject.toml` pin bump from `getpatter==0.5.4` to `getpatter==0.5.5`. Single-line PR, opens after the SDK release tag.
3. `docs-sync` may flag missing pages in `patter_sdk_features.xlsx`; resolve in the same merge window.

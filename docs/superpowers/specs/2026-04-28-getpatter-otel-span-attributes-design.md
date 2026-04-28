# `getpatter` OTel `patter.*` Span Attributes — Design Spec

**Date:** 2026-04-28
**Status:** Approved by Francesco — handing off to writing-plans.
**Working directory:** `[patterai]-Patter` (the published `getpatter` SDK monorepo, `sdk-py/`).
**Source prompt:** `[FrancescoRosciano]-patter-agent-runner/docs/PROMPT_observability.md`

## 1. Goal & non-goals

### Goal

Add `patter.*` OpenTelemetry span attributes to `getpatter==0.5.4` so that the `patter-agent-runner` acceptance suite — which already aggregates cost and latency from spans — can report **non-zero** `cost_usd`, `latency.ttfb_ms`, `latency.turn_p50_ms`, and `latency.turn_p95_ms` after a real PSTN call.

The current state: `runs/int-1777392487/inbound_twilio_realtime.json` reports `cost_usd=0.00` and zero latencies because no provider in `getpatter` emits `patter.cost.*` or `patter.latency.*` attributes today.

After this spec ships, the same smoke test produces a JSON artifact with positive cost and positive `turn_p95_ms`.

### Non-goals

- **Not a TypeScript change in this spec.** Parity follow-up tracked separately; per `sdk-parity.md` it must land in the same release cycle (one of `0.5.5`'s SDK pair).
- **Not a pricing change.** The consumer (`patter-agent-runner/telemetry/cost.py`) holds the price table. We only emit raw usage numbers.
- **Not a span-tree restructure.** We attach to existing spans where present and open one minimal span where none exists. We do not refactor the existing `services/llm_loop.py` / `services/tool_executor.py` / `handlers/stream_handler.py` instrumentation.

## 2. Consumer contract (verified)

`patter-agent-runner/src/patter_agent_runner/telemetry/cost.py` and `runner.py` dispatch on these attributes per span:

| Category | Required attributes | Allowed values |
|---|---|---|
| Routing (every span) | `patter.call_id` (str), `patter.side` (str) | side ∈ {"driver", "uut"} |
| Telephony | `patter.cost.telephony_minutes` (float), `patter.telephony` (str), `patter.direction` (str) | telephony ∈ {"twilio","telnyx"}; direction ∈ {"inbound","outbound"} |
| STT | `patter.cost.stt_seconds` (float), `patter.stt.provider` (str) | provider ∈ {"deepgram","assemblyai","whisper","soniox","speechmatics","cartesia"} |
| TTS | `patter.cost.tts_chars` (int), `patter.tts.provider` (str) | provider ∈ {"elevenlabs","openai_tts","cartesia_tts","lmnt","rime"} |
| LLM | `patter.cost.llm_input_tokens` (int), `patter.cost.llm_output_tokens` (int), `patter.llm.provider` (str) | provider ∈ {"anthropic","openai","google","groq","cerebras"} |
| Realtime | `patter.cost.realtime_minutes` (float), `patter.realtime.provider` (str) | provider ∈ {"openai_realtime","elevenlabs_convai"} |
| Latency | `patter.latency.ttfb_ms` (float), `patter.latency.turn_ms` (float) | per completed agent turn |

Spans missing `patter.call_id` or `patter.side` are silently dropped by `PatterSpanExporter.export()`. Therefore both routing tags MUST appear on every cost/latency span.

## 3. Architecture

### 3.1 Module layout

```
[patterai]-Patter/sdk-py/getpatter/observability/
├── __init__.py              (existing — re-exports new public names)
├── tracing.py               (existing — leave alone)
└── attributes.py            (NEW — ≤120 lines, holds the helper API)
```

The prompt called the new file `_observability.py` at the package root. We deviate: an `observability/` package already exists with `tracing.py`, and the new helpers belong inside it.

### 3.2 Helper API (`getpatter/observability/attributes.py`)

```python
"""patter.* span attribute helpers.

Version decision (2026-04-28): we are bumping getpatter 0.5.4 -> 0.5.5
because this module adds Patter._attach_span_exporter, a new opt-in
public method consumed by patter-agent-runner. During development the
acceptance suite installs from a worktree editable install; the version
bump only matters for the published artifact.
"""

# Lazy OTel guard
try:
    from opentelemetry import trace as _trace
    from opentelemetry.trace import Span as _Span, Tracer as _Tracer
    _OTEL = True
except ImportError:
    _OTEL = False

_patter_call_id: ContextVar[str | None] = ContextVar("patter.call_id", default=None)
_patter_side:    ContextVar[str]         = ContextVar("patter.side",   default="uut")

def record_patter_attrs(attrs: Mapping[str, object]) -> None:
    """Stamp patter.* attributes (plus call_id + side from ContextVars) on the
    currently active span. If no span is active, opens a transient
    'patter.billable' span just to carry the attributes. No-op if the
    [tracing] extra is not installed or call_id is unset."""

@contextmanager
def patter_call_scope(*, call_id: str, side: str = "uut") -> Iterator[None]:
    """Bind call_id and side to the current asyncio task tree."""

def attach_span_exporter(patter_instance, exporter, *, side: str = "uut") -> None:
    """Wire `exporter` into the global TracerProvider via SimpleSpanProcessor.
    Stores side on the Patter instance. Idempotent on repeated attach."""
```

### 3.3 `Patter` class wiring (`getpatter/client.py`)

- `Patter.__init__` stores `self._patter_side: str = "uut"`.
- New public method `Patter._attach_span_exporter(self, exporter, *, side: str = "uut")` calls `attach_span_exporter(self, exporter, side=side)`.
- At the top of the per-call handler in `getpatter/handlers/stream_handler.py`, the call enters `patter_call_scope(call_id=..., side=self._patter_side)` for the call's lifetime. The exact `call_id` source on the per-call session object (likely `session.call_id` or `state.call_sid`) is identified during plan writing by reading `stream_handler.py`. All asyncio child tasks inherit both ContextVars.

### 3.4 Side / call_id propagation — why ContextVar over kwargs

The prompt directs threading `call_id` and `side` as kwargs through every billable method. We deviate to ContextVar because:

1. Asyncio task isolation guarantees `patter-agent-runner`'s driver and UUT instances (running in the same process) see distinct ContextVar values.
2. We avoid changing ~18 method signatures.
3. Provider implementations stay decoupled from call-context plumbing.

The `Patter` instance is the sole writer of both ContextVars — at call start.

### 3.5 Per-provider instrumentation map (corrected from prompt)

The prompt's path list points at thin facade re-exports in `stt/`, `tts/`, `llm/`, `engines/`. The actual implementations live in `providers/*` and `services/llm_loop.py`. Real targets:

| Category | File | Hook | Attributes |
|---|---|---|---|
| Telephony | `providers/twilio_adapter.py` | `call.end` / `hangup` event handler | `cost.telephony_minutes`, `telephony="twilio"`, `direction` |
| Telephony | `providers/telnyx_adapter.py` | `call.hangup` event handler | same shape, `telephony="telnyx"` |
| STT | `providers/deepgram_stt.py` | final transcript callback | `cost.stt_seconds`, `stt.provider="deepgram"` |
| STT | `providers/assemblyai_stt.py` | final transcript | `"assemblyai"` |
| STT | `providers/whisper_stt.py` | synthesis return | `"whisper"` |
| STT | `providers/openai_transcribe_stt.py` | final transcript | `"whisper"` (cost-table parity) |
| STT | `providers/soniox_stt.py` | final transcript | `"soniox"` |
| STT | `providers/speechmatics_stt.py` | final transcript | `"speechmatics"` |
| STT | `providers/cartesia_stt.py` | final transcript | `"cartesia"` |
| TTS | `providers/elevenlabs_tts.py` | after synthesis returns | `cost.tts_chars`, `tts.provider="elevenlabs"` |
| TTS | `providers/openai_tts.py` | same | `"openai_tts"` |
| TTS | `providers/cartesia_tts.py` | same | `"cartesia_tts"` |
| TTS | `providers/lmnt_tts.py` | same | `"lmnt"` |
| TTS | `providers/rime_tts.py` | same | `"rime"` |
| LLM | `services/llm_loop.py` (`OpenAILLMProvider`) | after `chat.completions.create` returns, before yield | `cost.llm_input_tokens`, `cost.llm_output_tokens`, `llm.provider="openai"` |
| LLM | `providers/anthropic_llm.py` | after completion | `"anthropic"` |
| LLM | `providers/google_llm.py` | after completion | `"google"` |
| LLM | `providers/groq_llm.py` | after completion | `"groq"` |
| LLM | `providers/cerebras_llm.py` | after completion | `"cerebras"` |
| Realtime | `providers/openai_realtime.py` | `response.done` event with usage | `cost.realtime_minutes`, `realtime.provider="openai_realtime"` |
| Realtime | `providers/elevenlabs_convai.py` | session end (wall-clock) | `"elevenlabs_convai"` |
| Latency | `services/pipeline_hooks.py` | TTS first byte after user end-of-speech | `latency.ttfb_ms` |
| Latency | `services/pipeline_hooks.py` | turn complete | `latency.turn_ms` |

For each file the change pattern is identical: import `record_patter_attrs`, call it once after the billable work completes with the appropriate dict. No constructor signature changes.

### 3.6 Auto-span fallback inside `record_patter_attrs`

Most provider files have no active span at the hook site. Rather than add `tracer.start_as_current_span(...)` boilerplate to ~18 files, the helper itself opens a transient `patter.billable` span when `_trace.get_current_span()` returns the no-op span. This keeps provider-side changes to a single line.

## 4. Test strategy

### 4.1 Where & how

File: `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_span_attributes.py`
plus sibling `__init__.py` and `conftest.py` for the `in_memory_tracer` fixture.

Marker: `@pytest.mark.mocked` — tests mock the external WebSocket / HTTP boundary of providers. `@pytest.mark.unit` is incorrect here per the project's authentic-tests principle (boundary mocks must be tagged); see `[nicolotognoni]-Patter/.claude/rules/authentic-tests.md` for the full rule, which expresses the same convention the acceptance suite follows.

If `mocked` is missing from `python/pyproject.toml`'s `[tool.pytest.ini_options].markers`, add it.

### 4.2 What is real vs mocked

**Real:** the helper module, OpenTelemetry SDK (`TracerProvider`, `InMemorySpanExporter`, `SimpleSpanProcessor`), `Patter` class, every provider class under test, all attribute plumbing.

**Mocked at the boundary:** the provider's outermost network call (Deepgram WS, OpenAI HTTPS, Twilio carrier). Replaced with an async test double that fires the same callbacks (`on_transcript`, `response.done`, `call.end`) with realistic payloads.

### 4.3 Test cases

```python
@pytest.mark.mocked
class TestSpanAttributes:
    async def test_telephony_attributes_emitted(in_memory_tracer): ...
    async def test_realtime_attributes_emitted(in_memory_tracer): ...
    @pytest.mark.parametrize("stt_cls,expected_provider", [...])
    async def test_stt_attributes_emitted(stt_cls, expected_provider, ...): ...
    @pytest.mark.parametrize("tts_cls,expected_provider", [...])
    async def test_tts_attributes_emitted(tts_cls, expected_provider, ...): ...
    @pytest.mark.parametrize("llm_cls,expected_provider", [...])
    async def test_llm_attributes_emitted(llm_cls, expected_provider, ...): ...
    async def test_latency_attributes_emitted(in_memory_tracer): ...
    async def test_routing_tags_present(in_memory_tracer): ...
    async def test_driver_side_override(in_memory_tracer): ...
    async def test_no_otel_installed_is_noop(monkeypatch): ...
```

The test file imports cleanly with no credentials in the environment, places no real PSTN calls, and uses placeholder API keys throughout.

## 5. Cross-repo orchestration

### 5.1 Worktree

```
[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py/
```

Created off `main`, branch `feat/observability-otel-attrs`. All SDK edits land here.

### 5.2 Acceptance suite wiring (local-only)

`[nicolotognoni]-patter-sdk-acceptance/python/requirements.txt` has a stale `-e` line pointing at `/Users/nicolotognoni/...`. Rewrite it on a local branch (`local/observability-spans-frosci`) to point at this machine's worktree:

```
-e /Users/francescorosciano/docs/patter/[patterai]-Patter/.claude/worktrees/observability-spans/sdk-py[scheduling,assemblyai,soniox,speechmatics,cartesia,rime,lmnt,anthropic,groq,cerebras,google,gemini-live,ultravox,background-audio,ivr,silero,telnyx-ai]
```

Branch is **not pushed**. Once `0.5.5` ships, both machines update `requirements.txt` to pin `getpatter==0.5.5` instead.

### 5.3 Version

`[patterai]-Patter/sdk-py/pyproject.toml`: bump `version = "0.5.4"` → `version = "0.5.5"`.
`[FrancescoRosciano]-patter-agent-runner/pyproject.toml`: bump `getpatter==0.5.4` → `getpatter==0.5.5` in a **separate** PR after the SDK ships.

### 5.4 Step 7 PSTN smoke test — gated on explicit authorization

When the acceptance unit tests in Step 6 are green and the build passes, **stop and present a "Ready to smoke-test" report**. Include the exact command:

```
cd /Users/francescorosciano/docs/patter/[FrancescoRosciano]-patter-agent-runner
./scripts/docker-test.sh pytest -m integration -k "inbound_twilio_realtime and not pipeline and not convai" -v -s
```

Estimated cost ~$0.05–$0.20 for a sub-minute Twilio + OpenAI Realtime call. Wait for explicit "go" before running.

After the run, parse `runs/<run_id>/inbound_twilio_realtime.json` and confirm `cost_usd > 0`, `latency.turn_p95_ms > 0`, `latency.ttfb_ms > 0`. If any fail, return to Section 3.5 and find the un-instrumented hot path by inspecting span exporter output for missing `patter.cost.*` attributes.

## 6. Constraints (from prompt + project rules)

1. Use `_trace.get_current_span().set_attribute(...)`. Open new spans only inside the helper's auto-span fallback path.
2. OTel imports are lazy (try/except at module load). `import getpatter` works without the `[tracing]` extra.
3. Do not recompute pricing in the SDK. Emit raw usage; consumer applies the price table.
4. No mutation of frozen dataclasses. Per-call state lives on mutable adapter instances.
5. Files stay under 400 lines. The helper module is under 120.
6. No `print()`. Use `logging.getLogger("getpatter.observability")`.
7. `_attach_span_exporter` is opt-in with a safe default (`side="uut"`). Existing `Patter(...)` callers see zero behaviour change. (Per `opt-in-config.md`.)
8. Add a `[patterai]-Patter/docs/DEVLOG.md` entry before commit. (Per `devlog.md`.)
9. After implementation, dispatch `docs-sync` to update `patter_sdk_features.xlsx` and `docs/`. (Per `documentation-best-practices.md`.)
10. TS parity is **deferred to a follow-up PR**, must ship in the same `0.5.5` release cycle. (Per `sdk-parity.md`.)

## 7. Deliverables

PR title: `feat(observability): emit patter.cost.* and patter.latency.* OTel span attributes`

Files created in the worktree:
- `getpatter/observability/attributes.py`
- `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/__init__.py`
- `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/conftest.py`
- `[nicolotognoni]-patter-sdk-acceptance/python/tests/observability/test_span_attributes.py`

Files modified in the worktree:
- `getpatter/observability/__init__.py` (re-export `record_patter_attrs`, `patter_call_scope`)
- `getpatter/client.py` (add `_attach_span_exporter`, `_patter_side` instance attr)
- `getpatter/handlers/stream_handler.py` (enter `patter_call_scope` per call)
- `getpatter/providers/twilio_adapter.py`
- `getpatter/providers/telnyx_adapter.py`
- `getpatter/providers/{deepgram,assemblyai,whisper,openai_transcribe,soniox,speechmatics,cartesia}_stt.py`
- `getpatter/providers/{elevenlabs,openai,cartesia,lmnt,rime}_tts.py`
- `getpatter/providers/{anthropic,google,groq,cerebras}_llm.py`
- `getpatter/services/llm_loop.py` (OpenAI LLM)
- `getpatter/providers/openai_realtime.py`
- `getpatter/providers/elevenlabs_convai.py`
- `getpatter/services/pipeline_hooks.py`
- `sdk-py/pyproject.toml` (version bump 0.5.4 → 0.5.5)
- `docs/DEVLOG.md` (entry for this change)

Local-only file edit (not pushed):
- `[nicolotognoni]-patter-sdk-acceptance/python/requirements.txt`

Two acceptance gates:
- [ ] `cd python && pytest tests/observability/ -v` — all assertions pass with zero real calls.
- [ ] After explicit user "go", `patter-agent-runner` smoke test produces `cost_usd > 0` and `latency.turn_p95_ms > 0` in the JSON artifact.

## 8. Risks & rollback

- **Risk:** Provider class hierarchies have unexpected re-entrance (one Patter instance kicking off provider work that triggers a span in another instance's context). **Mitigation:** ContextVar reads in `record_patter_attrs` always reflect the current task's binding; no shared state. Verified by `test_driver_side_override`.
- **Risk:** The `[tracing]` extra is not installed in the smoke environment. **Mitigation:** `runner` already requires it (it instantiates `PatterSpanExporter`); for SDK consumers without the extra, all helpers no-op silently.
- **Risk:** Some provider doesn't actually invoke `record_patter_attrs` because of an early-return path we missed. **Mitigation:** test_*_attributes_emitted parametrizes over every listed provider; missing-instrumentation fails the test before the smoke test runs.
- **Rollback:** revert the worktree branch. The only externally visible change is `Patter._attach_span_exporter`; no caller in published code relies on it.

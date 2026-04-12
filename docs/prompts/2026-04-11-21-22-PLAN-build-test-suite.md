# Patter Test Suite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a comprehensive test suite across 6 categories (unit, integration, E2E, soak/stress, security, cross-SDK parity) for the Patter Python and TypeScript SDKs, raising coverage to 80%+ and adding CI jobs for every category.

**Architecture:** Agent-team-driven execution with 3 phases: infrastructure bootstrap, parallel test authoring (6 workers), then CI integration and review. Each worker owns a disjoint set of files — no merge conflicts.

**Tech Stack:** pytest + pytest-asyncio + pytest-cov (Python), vitest + Playwright (TypeScript), psutil (Python soak), GitHub Actions CI

---

## Context

Patter is a voice-AI platform connecting AI agents to live phone calls via Twilio/Telnyx. The codebase has two parallel SDKs:

- **Python SDK** (`sdk/`): ~9K LOC in `sdk/patter/`, 27 test files (417 tests, 31 known failures from missing deps + Python 3.14 compat)
- **TypeScript SDK** (`sdk-ts/`): ~9.7K LOC in `sdk-ts/src/`, 22 test files (286 tests, all passing)

Current gaps: no E2E, soak, security, or parity tests; no coverage gates in CI; empty `conftest.py`; no shared vitest setup.

### Critical Corrections from Prompt

The prompt references `patter_sdk` in several places, but the actual Python package is **`patter`** (source at `sdk/patter/`, not `sdk/patter_sdk/`). Coverage command must be `--cov=patter`, not `--cov=patter_sdk`.

The prompt references `.claude/rules/testing.md`, `.claude/rules/py-testing.md`, `.claude/rules/ts-testing.md` as repo files — these exist only in the user's **global** rules (`~/.claude/rules/`), not in the repo. No `CLAUDE.md` exists at the repo root. Testing conventions come from `CONTRIBUTING.md` and the user's global rules.

---

## File Structure

### New files to create

```
sdk/tests/
  conftest.py                          (populate — currently empty)
  unit/
    test_client_unit.py                (client.py unit tests)
    test_models_unit.py                (models.py E.164, serialization)
    test_stream_handler_unit.py        (stream_handler.py audio, SSE, tools)
    test_twilio_handler_unit.py        (twilio TwiML, WebSocket)
    test_telnyx_handler_unit.py        (telnyx events, HMAC)
    test_llm_loop_unit.py             (turn detection, tool extraction)
    test_metrics_unit.py              (circular buffer, aggregation)
    test_dashboard_store_unit.py       (pub/sub fan-out)
  integration/
    test_twilio_realtime.py
    test_twilio_convai.py
    test_twilio_pipeline.py
    test_telnyx_realtime.py
    test_telnyx_convai.py
    test_telnyx_pipeline.py
  soak/
    __init__.py
    conftest.py                        (soak fixtures, memory measurement)
    test_soak.py                       (S1-S6 scenarios)
  security/
    __init__.py
    test_security.py                   (SEC-1 through SEC-5)

sdk-ts/tests/
  setup.ts                             (shared mocks — currently missing)
  unit/
    client.test.ts
    stream-handler.test.ts
    server.test.ts
    llm-loop.test.ts
    metrics.test.ts
    dashboard-store.test.ts
  integration/
    twilio-realtime.test.ts
    twilio-convai.test.ts
    twilio-pipeline.test.ts
    telnyx-realtime.test.ts
    telnyx-convai.test.ts
    telnyx-pipeline.test.ts
  e2e/
    inbound-call.spec.ts
    outbound-call.spec.ts
    tool-calling.spec.ts
    call-transfer.spec.ts
    recording.spec.ts
    machine-detection.spec.ts
  soak/
    soak.test.ts                       (S1-S6 scenarios)
  security/
    security.test.ts                   (SEC-1 through SEC-5)

tests/parity/                          (repo root)
  __init__.py
  run.py                               (entry point)
  ts_shim.js                           (Node.js shim for TS SDK)
  scenarios/
    call_init.json
    audio_frame.json
    llm_turn.json
    metric_record.json
    store_pubsub.json
    tool_webhook.json
    model_e164.json
    call_status_enum.json
    voice_mode_enum.json

sdk-ts/playwright.config.ts            (new)
docs/2026-04-11-PLAN-build-test-suite.md  (ExecPlan)
.github/workflows/test.yml             (modify)
```

### Files to modify (not create)

```
sdk/pyproject.toml                     (add pytest markers, psutil dep)
sdk-ts/vitest.config.ts                (add setupFiles, tag filtering)
sdk-ts/package.json                    (add playwright, @vitest/coverage-v8 devDeps)
.github/workflows/test.yml             (add e2e, security, soak jobs)
```

### Source files: NO changes

Per the prompt's acceptance criteria, zero source files are modified. All tests mock at the network boundary.

---

## Task 0: Infrastructure Bootstrap

### Task 0A: Python Test Infrastructure (`sdk/tests/conftest.py`)

**Files:**
- Modify: `sdk/tests/conftest.py`
- Modify: `sdk/pyproject.toml` (add markers + psutil)

- [ ] **Step 1: Register pytest markers in `pyproject.toml`**

Add to `[tool.pytest.ini_options]`:
```toml
markers = [
    "unit: Unit tests",
    "integration: Integration tests",
    "soak: Soak/stress tests (slow)",
    "security: Security tests",
]
```

Add `psutil>=5.9.0` to `[project.optional-dependencies].dev`.

- [ ] **Step 2: Write `conftest.py` shared fixtures**

Populate `sdk/tests/conftest.py` with:
- `mock_ws_server` — async context manager yielding a mock WebSocket that records sent/received messages
- `mock_http_client` — `httpx.AsyncClient` mock with configurable responses
- `mock_twilio_webhook` — sends a POST with Twilio signature headers and TwiML body
- `mock_telnyx_webhook` — sends a POST with Telnyx event envelope
- `fake_pcm_frame(duration_ms=20, sample_rate=16000)` — returns bytes of silence at given rate
- `fake_mulaw_frame(duration_ms=20)` — returns mulaw-encoded silence
- `make_agent(**overrides)` — returns a `patter.Agent` with sensible defaults
- `make_config(**overrides)` — returns a config dict for `Patter()` init

- [ ] **Step 3: Verify existing tests still pass**

Run: `cd sdk && python3 -m pytest tests/ -v --tb=short -x`
Expected: same pass/fail count as before (383+ pass, known failures unchanged).

- [ ] **Step 4: Commit**

```bash
git add sdk/tests/conftest.py sdk/pyproject.toml
git commit -m "test(py): add shared pytest fixtures and markers"
```

### Task 0B: TypeScript Test Infrastructure (`sdk-ts/tests/setup.ts`)

**Files:**
- Create: `sdk-ts/tests/setup.ts`
- Modify: `sdk-ts/vitest.config.ts`
- Modify: `sdk-ts/package.json` (add devDeps)
- Create: `sdk-ts/playwright.config.ts`

- [ ] **Step 1: Add devDependencies**

```bash
cd sdk-ts && npm install -D @vitest/coverage-v8 playwright @playwright/test
```

- [ ] **Step 2: Create `tests/setup.ts`**

Shared vitest setup with:
- `vi.mock('ws')` — mock WebSocket constructor
- `vi.mock('express')` — mock Express app
- `mockFetch(responses: Record<string, Response>)` — configurable fetch mock
- `makeAgent(overrides?)` — returns `AgentOptions` with defaults
- `makeConfig(overrides?)` — returns `LocalOptions` with defaults
- `fakeAudioBuffer(durationMs=20, sampleRate=16000)` — returns Buffer of silence
- `fakeMulawBuffer(durationMs=20)` — returns mulaw-encoded Buffer

- [ ] **Step 3: Update `vitest.config.ts`**

```typescript
export default defineConfig({
  test: {
    globals: true,
    include: ["tests/**/*.test.ts"],
    exclude: ["tests/e2e/**"],
    setupFiles: ["tests/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      exclude: ["src/dashboard/ui.ts"],
    },
  },
});
```

- [ ] **Step 4: Create `playwright.config.ts`**

```typescript
import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests/e2e",
  webServer: {
    command: "npx tsx tests/e2e/test-server.ts",
    port: 8765,
    reuseExistingServer: !process.env.CI,
  },
});
```

- [ ] **Step 5: Verify existing tests still pass**

Run: `cd sdk-ts && npm test`
Expected: 286 tests pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add sdk-ts/tests/setup.ts sdk-ts/vitest.config.ts sdk-ts/package.json sdk-ts/package-lock.json sdk-ts/playwright.config.ts
git commit -m "test(ts): add shared vitest setup, playwright config, coverage"
```

---

## Task A: Python Unit + Integration Tests

**Files:**
- Create: `sdk/tests/unit/test_client_unit.py`, `test_models_unit.py`, `test_stream_handler_unit.py`, `test_twilio_handler_unit.py`, `test_telnyx_handler_unit.py`, `test_llm_loop_unit.py`, `test_metrics_unit.py`, `test_dashboard_store_unit.py`
- Create: `sdk/tests/integration/test_twilio_realtime.py`, `test_twilio_convai.py`, `test_twilio_pipeline.py`, `test_telnyx_realtime.py`, `test_telnyx_convai.py`, `test_telnyx_pipeline.py`
- Reference: `sdk/patter/client.py` (780 lines), `sdk/patter/handlers/stream_handler.py` (1054 lines), `sdk/patter/handlers/twilio_handler.py` (458 lines), `sdk/patter/handlers/telnyx_handler.py` (347 lines), `sdk/patter/services/llm_loop.py` (293 lines), `sdk/patter/services/metrics.py` (329 lines), `sdk/patter/dashboard/store.py` (254 lines), `sdk/patter/models.py` (199 lines)

### Unit test coverage targets (from prompt Section 3):

**`client.py`**: Happy path for each public async method (mock `websockets.connect`, `httpx.AsyncClient`). Error paths: connection refused, timeout, mid-stream disconnect. Async cancellation: `asyncio.CancelledError` propagation. State machine: idle -> connecting -> connected -> disconnected.

**`stream_handler.py`**: Audio frame queuing under backpressure. PCM/mulaw codec selection. Tool webhook: 3x retry, 10s timeout, 10-iteration cap. Concurrent instances don't share state. SSE broadcast + rapid subscribe/unsubscribe deadlock regression.

**`twilio_handler.py`**: TwiML response parsing/generation. WebSocket upgrade. Error from Twilio webhook (non-200, malformed).

**`telnyx_handler.py`**: Event envelope parsing. Call control commands (answer, hangup, transfer). Webhook signature verification (valid + invalid HMAC).

**`llm_loop.py`**: Turn completion detection. Streaming token accumulation. Tool-call extraction from partial JSON. Cost/token precision over 1000 turns.

**`metrics.py`**: 500-call circular buffer wrap-around. Concurrent write safety. Metric aggregation (mean, p95, p99) correctness.

**`dashboard/store.py`**: Read/write isolation. Pub/sub fan-out: 10 subscribers each get same update exactly once.

**`models.py`**: Pydantic validation (required, optional, discriminated unions). Reject invalid E.164. Round-trip JSON serialization.

### Integration matrix (from prompt Section 5):

| Provider | Voice Mode | Test File |
|----------|-----------|-----------|
| Twilio | Realtime | `sdk/tests/integration/test_twilio_realtime.py` |
| Twilio | ConvAI | `sdk/tests/integration/test_twilio_convai.py` |
| Twilio | Pipeline | `sdk/tests/integration/test_twilio_pipeline.py` |
| Telnyx | Realtime | `sdk/tests/integration/test_telnyx_realtime.py` |
| Telnyx | ConvAI | `sdk/tests/integration/test_telnyx_convai.py` |
| Telnyx | Pipeline | `sdk/tests/integration/test_telnyx_pipeline.py` |

Each integration test mocks at the network boundary (websockets.connect, httpx.AsyncClient, webhook HTTP). No real calls.

### TDD workflow per file:

- [ ] For each test file: write failing tests (RED), run `pytest <file> -v` to confirm failure, then confirm they pass against existing source (GREEN). All tests must exercise existing code — no source changes allowed.

- [ ] **After all unit + integration tests written, verify coverage:**

Run: `cd sdk && python3 -m pytest tests/ -v --cov=patter --cov-report=term-missing`
Expected: `TOTAL ... 80%` or higher.

- [ ] **Commit**

```bash
git add sdk/tests/unit/ sdk/tests/integration/
git commit -m "test(py): add unit and integration tests for 80%+ coverage"
```

---

## Task B: TypeScript Unit + Integration Tests

**Files:**
- Create: `sdk-ts/tests/unit/client.test.ts`, `stream-handler.test.ts`, `server.test.ts`, `llm-loop.test.ts`, `metrics.test.ts`, `dashboard-store.test.ts`
- Create: `sdk-ts/tests/integration/twilio-realtime.test.ts`, `twilio-convai.test.ts`, `twilio-pipeline.test.ts`, `telnyx-realtime.test.ts`, `telnyx-convai.test.ts`, `telnyx-pipeline.test.ts`
- Reference: `sdk-ts/src/client.ts` (449 lines), `sdk-ts/src/stream-handler.ts` (681 lines), `sdk-ts/src/server.ts` (893 lines), `sdk-ts/src/llm-loop.ts` (358 lines), `sdk-ts/src/metrics.ts` (369 lines), `sdk-ts/src/dashboard/store.ts` (192 lines)

### Coverage targets (from prompt Section 4):

**`client.ts`**: Same as Python — happy path, error paths, AbortController abort, state transitions.

**`stream-handler.ts`**: Audio buffer management. Tool webhook retry/timeout/cap parity with Python. SSE deadlock regression.

**`server.ts`**: Route registration, middleware ordering. Request/response serialization. Error middleware JSON shape.

**`llm-loop.ts`**: Same targets as Python `llm_loop.py`.

**`metrics.ts`**: Same circular-buffer and aggregation targets as Python `metrics.py`.

**`dashboard/store.ts`**: Same pub/sub targets as Python `dashboard/store.py`.

### Integration matrix (from prompt Section 5):

| Provider | Voice Mode | Test File |
|----------|-----------|-----------|
| Twilio | Realtime | `sdk-ts/tests/integration/twilio-realtime.test.ts` |
| Twilio | ConvAI | `sdk-ts/tests/integration/twilio-convai.test.ts` |
| Twilio | Pipeline | `sdk-ts/tests/integration/twilio-pipeline.test.ts` |
| Telnyx | Realtime | `sdk-ts/tests/integration/telnyx-realtime.test.ts` |
| Telnyx | ConvAI | `sdk-ts/tests/integration/telnyx-convai.test.ts` |
| Telnyx | Pipeline | `sdk-ts/tests/integration/telnyx-pipeline.test.ts` |

- [ ] TDD per file: write failing, run `npx vitest run <file>`, confirm fail, then confirm pass.

- [ ] **After all tests written, verify coverage:**

Run: `cd sdk-ts && npx vitest run --coverage`
Expected: `All files | ... | 80%` or higher.

- [ ] **Commit**

```bash
git add sdk-ts/tests/unit/ sdk-ts/tests/integration/
git commit -m "test(ts): add unit and integration tests for 80%+ coverage"
```

---

## Task C: Playwright E2E Tests

**Files:**
- Create: `sdk-ts/tests/e2e/test-server.ts` (helper: starts EmbeddedServer with mocked telephony)
- Create: `sdk-ts/tests/e2e/inbound-call.spec.ts`
- Create: `sdk-ts/tests/e2e/outbound-call.spec.ts`
- Create: `sdk-ts/tests/e2e/tool-calling.spec.ts`
- Create: `sdk-ts/tests/e2e/call-transfer.spec.ts`
- Create: `sdk-ts/tests/e2e/recording.spec.ts`
- Create: `sdk-ts/tests/e2e/machine-detection.spec.ts`
- Reference: `sdk-ts/src/server.ts` (routes), `sdk-ts/src/dashboard/ui.ts` (dashboard HTML), `sdk-ts/src/dashboard/routes.ts`

### Scenarios (from prompt Section 6):

1. **Inbound call**: POST to `/webhooks/twilio/voice`, assert dashboard updates, call reaches "connected"
2. **Outbound call**: Trigger via dashboard UI, assert status transitions visible
3. **Tool calling**: Inject tool-call event mid-conversation, assert webhook dispatched, response in transcript
4. **Call transfer**: Trigger transfer, assert request sent, call reaches "transferred"
5. **Recording**: Enable recording, assert start/stop events in dashboard
6. **Machine detection**: Inject AMD event, assert dashboard reflects detection result

Each scenario independently runnable. The `test-server.ts` helper spawns a real Express server on port 8765 with mocked Twilio/Telnyx providers.

- [ ] Write test-server helper
- [ ] Write each spec file (RED first — verify Playwright can navigate to dashboard)
- [ ] Run: `cd sdk-ts && npx playwright install --with-deps && npx playwright test`
- [ ] Expected: N passed (N >= 3 for acceptance: inbound, outbound, tool-calling)
- [ ] **Commit**

```bash
git add sdk-ts/tests/e2e/
git commit -m "test(ts): add Playwright E2E tests for dashboard scenarios"
```

---

## Task D: Soak/Stress Harness

**Files:**
- Create: `sdk/tests/soak/__init__.py`, `sdk/tests/soak/conftest.py`, `sdk/tests/soak/test_soak.py`
- Create: `sdk-ts/tests/soak/soak.test.ts`

### Scenarios (from prompt Section 7):

| ID | Scenario | Success Criteria |
|----|----------|-----------------|
| S1 | 100 concurrent calls, 10 min | RSS growth < 10% |
| S2 | 1000-turn conversation | Cost/token arithmetically correct, no memory leak |
| S3 | 20 disconnect/reconnect cycles (50ms gap) | All frames flushed or explicitly dropped, reconnect < 500ms |
| S4 | 50 SSE subscribers churn (100ms each), 10 events | Each connected subscriber gets each event exactly once, no deadlock (30s timeout) |
| S5 | 501 calls to metrics buffer | Oldest evicted, newest 500 present and ordered |
| S6 | 1000-turn cost precision | Final cost == analytic sum within 1e-9 |

Python uses `psutil` for RSS measurement. TypeScript uses `process.memoryUsage()`.

All tests marked `@pytest.mark.soak` (Python) / tagged in vitest config (TypeScript).

- [ ] Write Python soak tests with `psutil.Process().memory_info().rss` measurements
- [ ] Write TypeScript soak tests with `process.memoryUsage().rss` measurements
- [ ] Run Python: `cd sdk && python3 -m pytest tests/soak/ -v -m soak`
- [ ] Run TypeScript: `cd sdk-ts && npx vitest run tests/soak --reporter=verbose`
- [ ] Expected output lines: `Memory growth: X.X% (threshold: 10.0%) PASS`
- [ ] **Commit**

```bash
git add sdk/tests/soak/ sdk-ts/tests/soak/
git commit -m "test: add soak/stress harness (S1-S6) for both SDKs"
```

---

## Task E: Security Test Suite

**Files:**
- Create: `sdk/tests/security/__init__.py`, `sdk/tests/security/test_security.py`
- Create: `sdk-ts/tests/security/security.test.ts`

### Scenarios (from prompt Section 8):

Each scenario has 1 positive test (attack blocked) + 1 negative test (legit input accepted):

| ID | Scenario | Positive (blocked) | Negative (accepted) |
|----|----------|-------------------|-------------------|
| SEC-1 | SSRF on webhook URLs | `http://169.254.169.254/...` and `http://localhost:8080/internal` rejected | Valid public HTTPS URL accepted |
| SEC-2 | XSS in dashboard fields | `<script>alert(1)</script>` as caller ID/agent name — rejected or escaped | Normal text stored correctly |
| SEC-3 | E.164 fuzzing | Empty, `+`, `+1`, too-long, alpha, `null`, `undefined`, 10MB string — all rejected | `+14155552671` accepted |
| SEC-4 | TwiML injection | `<Redirect>http://attacker.example/evil</Redirect>` — rejected or stripped | Valid TwiML processed correctly |
| SEC-5 | Secret leakage | Trigger error with log capture — no API keys/tokens in logs matching `[A-Za-z0-9+/]{20,}` | Error message provides useful info without secrets |

All tests marked `@pytest.mark.security` (Python).

- [ ] Write Python security tests
- [ ] Write TypeScript security tests
- [ ] Run: `cd sdk && python3 -m pytest tests/security/ -v -m security`
- [ ] Run: `cd sdk-ts && npx vitest run tests/security`
- [ ] Expected: all pass, 10+ tests total across both SDKs
- [ ] **Commit**

```bash
git add sdk/tests/security/ sdk-ts/tests/security/
git commit -m "test: add security suite (SEC-1 through SEC-5) for both SDKs"
```

---

## Task F: Cross-SDK Parity Suite

**Files:**
- Create: `tests/parity/__init__.py`
- Create: `tests/parity/run.py`
- Create: `tests/parity/ts_shim.js`
- Create: `tests/parity/scenarios/*.json` (9+ scenario files)
- Reference: `sdk/patter/__init__.py` (exports), `sdk-ts/src/index.ts` (exports)

### Parity scenario JSON schema (from prompt Section 9):

```json
{
  "scenario_id": "string",
  "description": "string",
  "input": { "method": "string", "args": {}, "kwargs": {} },
  "expected_output": { "type": "string", "value": {} },
  "sdk_methods": {
    "python": "module.path.method_name",
    "typescript": "ClassName.methodName"
  }
}
```

### Minimum parity scenarios:

1. Call initialization
2. Audio frame submission
3. LLM turn completion detection
4. Metric recording
5. Dashboard store subscribe/publish
6. Tool webhook dispatch
7. Model serialization (E.164 phone)
8. Call status enum values
9. Voice mode enum values

### `run.py` logic:

1. Load all JSON scenario files from `scenarios/`
2. Invoke Python SDK method with scenario input
3. Invoke TypeScript SDK method via `subprocess` running `ts_shim.js`
4. Compare outputs, report pass/fail with diff on divergence
5. Output: `Parity: N/M methods matched (X.X%) PASS` (target >= 80%)

### Public API surface comparison:

Python exports (from `sdk/patter/__init__.py`): `Patter`, `Agent`, `CallControl`, `CallEvent`, `CallMetrics`, `CostBreakdown`, `Guardrail`, `IncomingMessage`, `LatencyBreakdown`, `STTConfig`, `TTSConfig`, `TurnMetrics`, + 4 exceptions.

TypeScript exports (from `sdk-ts/src/index.ts`): `Patter`, `Agent`, `CallMetrics`, `CostBreakdown`, `Guardrail`, `IncomingMessage`, `LatencyBreakdown`, `STTConfig`, `TTSConfig`, `TurnMetrics`, `CallControl`, + 4 exceptions, + `MetricsStore`, `LLMLoop`, `OpenAILLMProvider`, providers, transcoding, dashboard utilities.

Shared surface (types + main class): ~16 names. Parity suite should cover >= 80% of these.

- [ ] Create directory structure and `__init__.py`
- [ ] Write `ts_shim.js` (Node.js script that imports the TS SDK, reads scenario JSON from stdin, executes method, outputs result JSON)
- [ ] Write `run.py` entry point
- [ ] Write 9+ scenario JSON files
- [ ] Run: `cd /path/to/repo && python3 tests/parity/run.py`
- [ ] Expected: `Parity: N/M methods matched (X.X%) PASS` with X.X >= 80
- [ ] **Commit**

```bash
git add tests/parity/
git commit -m "test: add cross-SDK parity suite with 9+ scenarios"
```

---

## Task G: ExecPlan Document

**Files:**
- Create: `docs/2026-04-11-PLAN-build-test-suite.md`

Write the ExecPlan following the format in the prompt (Section 2), including:
- Every module covered with full relative file path
- Test matrix: providers x voice modes
- All soak scenarios with success criteria
- All security scenarios with positive/negative cases
- All parity scenarios with JSON schema
- Milestones (independently verifiable)
- Empty sections for: Progress, Decision Log, Surprises & Discoveries, Outcomes & Retrospective

- [ ] Write ExecPlan
- [ ] **Commit**

```bash
git add docs/2026-04-11-PLAN-build-test-suite.md
git commit -m "docs: add ExecPlan for comprehensive test suite"
```

---

## Task H: CI Workflow Update

**Files:**
- Modify: `.github/workflows/test.yml`

### Changes (from prompt Section 11):

Add 3 new jobs to the existing workflow WITHOUT modifying the existing `python` and `typescript` matrix jobs:

1. **`e2e` job** — Node 20 only (not a matrix):
   ```yaml
   e2e:
     name: E2E Tests
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v6
       - uses: actions/setup-node@v6
         with: { node-version: '20' }
       - run: cd sdk-ts && npm install
       - run: cd sdk-ts && npx playwright install --with-deps
       - run: cd sdk-ts && npx playwright test
   ```

2. **`security` job** — Python 3.11 only:
   ```yaml
   security:
     name: Security Tests
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v6
       - uses: actions/setup-python@v6
         with: { python-version: '3.11' }
       - run: cd sdk && pip install -e ".[local,dev]"
       - run: cd sdk && pytest tests/security/ -v -m security
   ```

3. **`soak` job** — manual trigger only (`workflow_dispatch`), dedicated runner:
   ```yaml
   soak:
     name: Soak Tests
     runs-on: [self-hosted, soak-runner]
     if: github.event_name == 'workflow_dispatch'
     steps:
       - uses: actions/checkout@v6
       - uses: actions/setup-python@v6
         with: { python-version: '3.13' }
       - uses: actions/setup-node@v6
         with: { node-version: '20' }
       - run: cd sdk && pip install -e ".[local,dev]" && pip install psutil
       - run: cd sdk && pytest tests/soak/ -v -m soak 2>&1 | tee py-soak.txt
       - run: cd sdk-ts && npm install && npx vitest run tests/soak --reporter=verbose 2>&1 | tee ts-soak.txt
       - uses: actions/upload-artifact@v4
         with:
           name: soak-results
           path: |
             sdk/py-soak.txt
             sdk-ts/ts-soak.txt
   ```

Also add `workflow_dispatch` to the top-level `on:` triggers (only for the soak job).

- [ ] Update `.github/workflows/test.yml`
- [ ] **Commit**

```bash
git add .github/workflows/test.yml
git commit -m "ci: add e2e, security, and soak jobs to test workflow"
```

---

## Verification

Run this sequence end-to-end after all test files are written. Do not commit final until every check passes.

| Step | Command | Expected |
|------|---------|----------|
| 1 | `cd sdk && python3 -m pytest tests/ -v --cov=patter --cov-report=term-missing` | `TOTAL ... 80%` or higher |
| 2 | `cd sdk && python3 -m pytest tests/soak/ -v -m soak` | `Memory growth: X.X% (threshold: 10.0%) PASS` |
| 3 | `cd sdk && python3 -m pytest tests/security/ -v -m security` | All pass, no `FAILED` |
| 4 | `cd sdk-ts && npm test && npm run lint && npm run build` | Clean |
| 5 | `cd sdk-ts && npx vitest run --coverage` | `All files ... 80%` or higher |
| 6 | `cd sdk-ts && npx playwright install --with-deps && npx playwright test` | N >= 3 passed |
| 7 | `cd sdk-ts && npx vitest run tests/soak --reporter=verbose` | `Memory growth: X.X% (threshold: 10.0%) PASS` |
| 8 | `python3 tests/parity/run.py` | `Parity: N/M methods matched (X.X%) PASS` with X.X >= 80 |
| 9 | `git diff --name-only main` | Only test files, infra, CI, ExecPlan — no source files |

### Existing test regression check:

- 28+ existing Python tests pass (from original 28; currently 417 total with 383 passing)
- 22+ existing TypeScript tests pass (from original 22; currently 286 total)

---

## Agent Team Execution Strategy

This section defines how to dispatch an agent team to execute the plan above. This is **mandatory** per project rules and the prompt's Section "Mandatory: Use an Agent Team".

### Team Topology

```
                    ┌─────────────────────┐
                    │      LEAD           │
                    │  (main thread)      │
                    │  Orchestrates via   │
                    │  TeamCreate +       │
                    │  TaskCreate/Update  │
                    └──────┬──────────────┘
                           │
              Phase 0      │      Phase 1          Phase 2           Phase 3
           (parallel)      │    (parallel)        (parallel)        (sequential)
         ┌─────────────┐   │  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐
         │ infra-py     │   │  │ author-py    │  │ author-e2e   │  │ ci-author     │
         │ (Task 0A)    │   │  │ (Task A)     │  │ (Task C)     │  │ (Task H)      │
         ├─────────────┤   │  ├──────────────┤  ├──────────────┤  ├───────────────┤
         │ infra-ts     │   │  │ author-ts    │  │ author-soak  │  │ code-reviewer │
         │ (Task 0B)    │   │  │ (Task B)     │  │ (Task D)     │  │ (final gate)  │
         ├─────────────┤   │  └──────────────┘  ├──────────────┤  └───────────────┘
         │ plan-author  │   │                    │ author-sec   │
         │ (Task G)     │   │                    │ (Task E)     │
         └─────────────┘   │                    ├──────────────┤
                           │                    │ author-parity│
                           │                    │ (Task F)     │
                           │                    └──────────────┘
```

### Phase 0: Bootstrap (3 agents in parallel)

```
Agent 1: "infra-py"     — Task 0A (conftest.py + pyproject.toml markers)
Agent 2: "infra-ts"     — Task 0B (setup.ts + vitest config + playwright config)
Agent 3: "plan-author"  — Task G (ExecPlan document)
```

**Gate**: All 3 complete. Existing tests still pass in both SDKs.

### Phase 1: Core Tests (2 agents in parallel)

```
Agent 4: "author-py"    — Task A (Python unit + integration tests)
Agent 5: "author-ts"    — Task B (TypeScript unit + integration tests)
```

These are the largest tasks. Each agent reads the relevant source files, writes test files following TDD (RED then GREEN), and runs coverage.

**Gate**: Coverage >= 80% in both SDKs. All existing tests still pass.

### Phase 2: Specialized Tests (4 agents in parallel)

```
Agent 6: "author-e2e"    — Task C (Playwright E2E)
Agent 7: "author-soak"   — Task D (soak/stress for BOTH SDKs — single agent, disjoint dirs)
Agent 8: "author-sec"    — Task E (security for BOTH SDKs — single agent, disjoint dirs)
Agent 9: "author-parity" — Task F (cross-SDK parity at repo root)
```

Tasks D and E are kept as single agents (not split per SDK) to ensure identical test scenario design across both SDKs. Each writes to completely disjoint directories — no conflicts.

**Gate**: All specialized tests pass. Soak output includes memory growth lines. Security has 10+ tests. Parity >= 80%.

### Phase 3: CI + Review (2 agents, sequential)

```
Agent 10: "ci-author"     — Task H (CI workflow update)
Agent 11: "code-reviewer" — Final review of all changes (read-only)
```

The code-reviewer runs after ALL test files and CI changes are complete. It checks: test quality, no hardcoded secrets in fixtures, proper async cleanup, no flaky patterns, proper markers/tags, and alignment with the prompt's acceptance criteria.

**Gate**: Code reviewer approves. Full verification sequence passes.

### Dispatch Commands

**Step 1 — Create team:**
```
TeamCreate(team_name: "patter-test-suite", description: "Build comprehensive test suite for Patter Python and TypeScript SDKs")
```

**Step 2 — Create all tasks (A-H + 0A + 0B):**
```
TaskCreate for each: 0A, 0B, G, A, B, C, D, E, F, H
```

**Step 3 — Phase 0: Spawn 3 agents in parallel:**
```
Agent(name: "infra-py",    team_name: "patter-test-suite", prompt: "Execute Task 0A...")
Agent(name: "infra-ts",    team_name: "patter-test-suite", prompt: "Execute Task 0B...")
Agent(name: "plan-author", team_name: "patter-test-suite", prompt: "Execute Task G...")
```

**Step 4 — Phase 1: After Phase 0 gate, spawn 2 agents:**
```
Agent(name: "author-py", team_name: "patter-test-suite", prompt: "Execute Task A...")
Agent(name: "author-ts", team_name: "patter-test-suite", prompt: "Execute Task B...")
```

**Step 5 — Phase 2: After Phase 1 gate, spawn 4 agents:**
```
Agent(name: "author-e2e",    team_name: "patter-test-suite", prompt: "Execute Task C...")
Agent(name: "author-soak",   team_name: "patter-test-suite", prompt: "Execute Task D...")
Agent(name: "author-sec",    team_name: "patter-test-suite", prompt: "Execute Task E...")
Agent(name: "author-parity", team_name: "patter-test-suite", prompt: "Execute Task F...")
```

**Step 6 — Phase 3: After Phase 2 gate, spawn 2 agents sequentially:**
```
Agent(name: "ci-author",     team_name: "patter-test-suite", prompt: "Execute Task H...")
Agent(name: "code-reviewer", team_name: "patter-test-suite", subagent_type: "code-reviewer", prompt: "Review all changes...")
```

**Step 7 — Verification + commit:**
Run the full verification sequence from the Verification section above. If all pass, commit.

### Conflict Prevention

Every agent writes to a unique directory prefix:
- `infra-py` / `author-py`: `sdk/tests/`
- `infra-ts` / `author-ts`: `sdk-ts/tests/` (excluding `e2e/`, `soak/`, `security/`)
- `author-e2e`: `sdk-ts/tests/e2e/`
- `author-soak`: `sdk/tests/soak/` + `sdk-ts/tests/soak/`
- `author-sec`: `sdk/tests/security/` + `sdk-ts/tests/security/`
- `author-parity`: `tests/parity/` (repo root)
- `ci-author`: `.github/workflows/`
- `plan-author`: `docs/`

No two agents write to the same directory. The only shared dependency is the infrastructure from Phase 0 (conftest.py and setup.ts), which is completed before any authoring agents start.

### Total Agent Count: 11

| # | Name | Phase | Tasks | Type |
|---|------|-------|-------|------|
| 1 | infra-py | 0 | 0A | general-purpose |
| 2 | infra-ts | 0 | 0B | general-purpose |
| 3 | plan-author | 0 | G | general-purpose |
| 4 | author-py | 1 | A | general-purpose |
| 5 | author-ts | 1 | B | general-purpose |
| 6 | author-e2e | 2 | C | general-purpose |
| 7 | author-soak | 2 | D | general-purpose |
| 8 | author-sec | 2 | E | general-purpose |
| 9 | author-parity | 2 | F | general-purpose |
| 10 | ci-author | 3 | H | general-purpose |
| 11 | code-reviewer | 3 | review | code-reviewer |

Peak concurrency: 4 agents (Phase 2). Each phase gates on all agents completing before the next phase starts, preventing conflicts and ensuring shared infrastructure is available.

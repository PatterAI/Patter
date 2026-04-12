# Build Comprehensive Test Suite — ExecPlan

**Date:** 2026-04-11
**Goal:** Build a test suite across 6 categories (unit, integration, E2E, soak, security, parity) raising coverage to 80%+ for both Python and TypeScript SDKs.
**Scope:** Python SDK (`sdk/`), TypeScript SDK (`sdk-ts/`), CI pipeline (`.github/workflows/`)

---

## 1. Module Coverage Map

### Python SDK (~3,714 LOC across core modules)

| Module | Path | Lines | What Will Be Tested |
|--------|------|-------|---------------------|
| Client | `sdk/patter/client.py` | 780 | Async methods, state machine transitions, error paths, config validation |
| Stream Handler | `sdk/patter/handlers/stream_handler.py` | 1054 | Audio queuing, SSE fan-out, tool webhook retry, backpressure |
| Twilio Handler | `sdk/patter/handlers/twilio_handler.py` | 458 | TwiML generation, WebSocket lifecycle, Twilio signature validation |
| Telnyx Handler | `sdk/patter/handlers/telnyx_handler.py` | 347 | Event dispatch, call control commands, HMAC verification |
| LLM Loop | `sdk/patter/services/llm_loop.py` | 293 | Turn detection, tool call extraction, cost accumulation precision |
| Metrics | `sdk/patter/services/metrics.py` | 329 | Circular buffer eviction, p95/p99 computation, concurrent write safety |
| Dashboard Store | `sdk/patter/dashboard/store.py` | 254 | Pub/sub fan-out, read/write isolation, subscriber lifecycle |
| Models | `sdk/patter/models.py` | 199 | E.164 validation, Pydantic serialization round-trip, enum coverage |

**Supporting modules** (tested via integration/E2E, not dedicated unit files):

| Module | Path | Notes |
|--------|------|-------|
| Server | `sdk/patter/server.py` | Route registration, middleware |
| API Routes | `sdk/patter/api_routes.py` | REST endpoints |
| Connection | `sdk/patter/connection.py` | WebSocket management |
| Call Orchestrator | `sdk/patter/services/call_orchestrator.py` | Call lifecycle coordination |
| Session Manager | `sdk/patter/services/session_manager.py` | Session state |
| Tool Executor | `sdk/patter/services/tool_executor.py` | Tool dispatch |
| Transcoding | `sdk/patter/services/transcoding.py` | Audio format conversion |
| Remote Message | `sdk/patter/services/remote_message.py` | Inter-service messaging |
| Providers | `sdk/patter/providers/*.py` | Twilio, Telnyx, OpenAI, ElevenLabs, Deepgram, Whisper adapters |

### TypeScript SDK (~2,942 LOC across core modules)

| Module | Path | Lines | What Will Be Tested |
|--------|------|-------|---------------------|
| Client | `sdk-ts/src/client.ts` | 449 | Async methods, state machine, error paths, config validation |
| Stream Handler | `sdk-ts/src/stream-handler.ts` | 681 | Audio queuing, tool webhook dispatch, SSE streaming |
| Server | `sdk-ts/src/server.ts` | 893 | Route registration, middleware chain, error handling |
| LLM Loop | `sdk-ts/src/llm-loop.ts` | 358 | Turn detection, tool extraction, cost precision |
| Metrics | `sdk-ts/src/metrics.ts` | 369 | Circular buffer, p95/p99, concurrent writes |
| Dashboard Store | `sdk-ts/src/dashboard/store.ts` | 192 | Pub/sub fan-out, read/write isolation |

**Supporting modules** (tested via integration/E2E):

| Module | Path | Notes |
|--------|------|-------|
| Types | `sdk-ts/src/types.ts` | Type definitions, enum values |
| Connection | `sdk-ts/src/connection.ts` | WebSocket management |
| Handler Utils | `sdk-ts/src/handler-utils.ts` | Shared handler utilities |
| Transcoding | `sdk-ts/src/transcoding.ts` | Audio format conversion |
| Remote Message | `sdk-ts/src/remote-message.ts` | Inter-service messaging |
| Providers | `sdk-ts/src/providers/*.ts` | OpenAI Realtime, ElevenLabs, Deepgram, Whisper, OpenAI TTS |
| Dashboard Routes | `sdk-ts/src/dashboard/routes.ts` | Dashboard API endpoints |
| Dashboard Auth | `sdk-ts/src/dashboard/auth.ts` | Dashboard authentication |
| Dashboard UI | `sdk-ts/src/dashboard/ui.ts` | Dashboard rendering |
| Dashboard Export | `sdk-ts/src/dashboard/export.ts` | Data export |

---

## 2. Test Matrix: Providers x Voice Modes

Each cell represents a dedicated integration test file that exercises the full call flow for that provider+mode combination.

### Python SDK (`sdk/tests/`)

| Provider | Realtime | ConvAI | Pipeline |
|----------|----------|--------|----------|
| **Twilio** | `test_twilio_realtime.py` | `test_twilio_convai.py` | `test_twilio_pipeline.py` |
| **Telnyx** | `test_telnyx_realtime.py` | `test_telnyx_convai.py` | `test_telnyx_pipeline.py` |

### TypeScript SDK (`sdk-ts/tests/`)

| Provider | Realtime | ConvAI | Pipeline |
|----------|----------|--------|----------|
| **Twilio** | `twilio-realtime.test.ts` | `twilio-convai.test.ts` | `twilio-pipeline.test.ts` |
| **Telnyx** | `telnyx-realtime.test.ts` | `telnyx-convai.test.ts` | `telnyx-pipeline.test.ts` |

**What each cell tests:**
- Call initialization with correct provider config
- WebSocket/event connection establishment
- Audio frame round-trip (send raw -> receive processed)
- LLM turn completion with tool calls
- Metric recording during active call
- Graceful disconnect and resource cleanup

---

## 3. Soak Scenarios

Soak tests validate stability, memory safety, and correctness under sustained load. Each scenario runs against both SDKs.

| ID | Scenario | Duration | Success Criteria |
|----|----------|----------|-----------------|
| S1 | 100 concurrent calls | 10 min | RSS memory growth < 10% from baseline |
| S2 | 1000-turn conversation | Until complete | Cost/token accumulation correct, no memory leak |
| S3 | 20 rapid disconnect/reconnect cycles (50ms gap) | Until complete | All audio frames flushed, reconnect latency < 500ms |
| S4 | 50 SSE subscriber churn (connect/disconnect) | 30s timeout | Each event delivered exactly once per active subscriber, no deadlock |
| S5 | 501 calls pushed to 500-slot circular buffer | Until complete | Oldest call evicted, 500 calls retained in insertion order |
| S6 | 1000-turn cost accumulation | Until complete | Running sum matches individual-turn sum within 1e-9 |

**Harness requirements:**
- Configurable concurrency level and turn count
- RSS memory sampling at 1s intervals (S1, S2)
- Latency histogram output (S3)
- Event delivery audit log (S4)
- Buffer state snapshot assertions (S5)
- Decimal precision comparison (S6)

---

## 4. Security Scenarios

Security tests validate input sanitization, authentication, and data protection boundaries.

| ID | Scenario | Positive Test (must be blocked) | Negative Test (must be accepted) |
|----|----------|--------------------------------|----------------------------------|
| SEC-1 | SSRF via webhook URL | `169.254.169.254`, `localhost`, `127.0.0.1`, `[::1]`, `file://` rejected | Public HTTPS URL (e.g., `https://example.com/hook`) accepted |
| SEC-2 | XSS via dashboard input | `<script>alert(1)</script>`, `<img onerror=...>` rejected or escaped | Normal text (`"Hello World"`) stored and rendered safely |
| SEC-3 | E.164 phone number fuzzing | `abc`, `+0000`, `12345`, `+1-415-555-2671` (hyphens), empty string rejected | `+14155552671` accepted |
| SEC-4 | TwiML injection | `<Redirect>` verb injection, XML entity expansion stripped | Valid TwiML verbs (`<Say>`, `<Gather>`) generated correctly |
| SEC-5 | Secret leakage in error output | API keys, auth tokens, connection strings absent from logs and error responses | Error messages remain useful and actionable |

**Implementation notes:**
- SEC-1: Test both Python (`urllib.parse`) and TS (`URL`) URL resolution to catch DNS rebinding edge cases
- SEC-2: Test both raw store writes and SSE-serialized output
- SEC-3: Use property-based testing (Hypothesis / fast-check) for fuzz inputs
- SEC-4: Twilio handler only; verify XML output is well-formed and contains no injected verbs
- SEC-5: Capture stderr/stdout during intentional failures and grep for secret patterns

---

## 5. Parity Scenarios

Parity tests ensure the Python and TypeScript SDKs behave identically for equivalent operations. Each scenario is defined as a JSON fixture consumed by both test suites.

### Scenario JSON Schema

```json
{
  "id": "string",
  "name": "string",
  "description": "string",
  "input": { "...scenario-specific input..." },
  "expected": {
    "output": "any",
    "side_effects": ["list of observable effects"],
    "error": "null | { code: string, message_pattern: string }"
  }
}
```

### Scenarios

| # | ID | Name | What It Validates |
|---|-----|------|-------------------|
| 1 | `call_init` | Call initialization | Given identical config, both SDKs produce the same initial call state |
| 2 | `audio_frame` | Audio frame submission | Same raw audio input yields identical processed output |
| 3 | `llm_turn` | LLM turn completion detection | Same transcript triggers turn detection at the same point |
| 4 | `metric_record` | Metric recording | Same call events produce identical metric snapshots |
| 5 | `store_pubsub` | Dashboard store subscribe/publish | Same publish sequence delivers identical events to subscribers |
| 6 | `tool_webhook` | Tool webhook dispatch | Same tool call produces identical HTTP request shape |
| 7 | `model_e164` | Model serialization (E.164) | Same phone number input serializes/deserializes identically |
| 8 | `call_status_enum` | Call status enum values | Both SDKs define the same set of call status values |
| 9 | `voice_mode_enum` | Voice mode enum values | Both SDKs define the same set of voice mode values |

**Fixture location:** `tests/parity/fixtures/*.json` (shared between both SDKs)

---

## 6. Milestones

- [ ] **M1:** Infrastructure bootstrap — `conftest.py`, `setup.ts`, pytest markers, vitest config, coverage thresholds
- [ ] **M2:** Python unit tests — 8 core module test files, 80%+ line coverage per module
- [ ] **M3:** TypeScript unit tests — 6 core module test files, 80%+ line coverage per module
- [ ] **M4:** Integration tests — 6 provider x mode cells per SDK, all matrix cells covered
- [ ] **M5:** E2E tests — 6 Playwright scenarios (call init, audio flow, dashboard, tool webhook, disconnect, multi-turn), 3+ passing
- [ ] **M6:** Soak harness — S1-S6 implemented for both SDKs, runnable via `npm run test:soak` / `pytest -m soak`
- [ ] **M7:** Security suite — SEC-1 through SEC-5 implemented for both SDKs, runnable via `npm run test:security` / `pytest -m security`
- [ ] **M8:** Parity suite — 9 scenario fixtures, runner for both SDKs, 80%+ field match rate
- [ ] **M9:** CI pipeline updated — `.github/workflows/test.yml` includes e2e, security, and soak jobs
- [ ] **M10:** All acceptance criteria pass — 80%+ coverage, all matrix cells green, soak/security/parity suites passing

---

## 7. Progress

_Updated as milestones are completed._

- [ ] M1
- [ ] M2
- [ ] M3
- [ ] M4
- [ ] M5
- [ ] M6
- [ ] M7
- [ ] M8
- [ ] M9
- [ ] M10

---

## 8. Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| | | |

---

## 9. Surprises & Discoveries

_Notable findings during test implementation._

---

## 10. Outcomes & Retrospective

_To be written after M10 is complete._

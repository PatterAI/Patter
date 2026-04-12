# Build a comprehensive test suite for the Patter Python and TypeScript SDKs covering unit, integration, end-to-end, soak/stress, security, and cross-SDK parity tests.

## Context

Patter is an open-source voice-AI platform that connects AI agents to live phone calls via Twilio and Telnyx, supporting three voice modes (OpenAI Realtime, ElevenLabs ConvAI, Pipeline STT→LLM→TTS) and three deployment modes (Cloud, Self-hosted, Local). Because Patter mediates real phone calls in production, reliability failures surface immediately as dropped or corrupted calls — making a robust test suite a prerequisite for safe development. The codebase currently has 28 Python tests and 22 TypeScript tests, with no end-to-end, soak, security, or cross-SDK parity coverage and no E2E or stress jobs in CI. The project's `.claude/rules/testing.md` mandates 80% coverage, TDD RED→GREEN→IMPROVE discipline, and the AAA (Arrange-Act-Assert) pattern.

## Project

Working directory: `/Users/francescorosciano/docs/patter/Patter/`

Read the project's CLAUDE.md first, then read `.claude/rules/testing.md`, `.claude/rules/py-testing.md`, and `.claude/rules/ts-testing.md` before writing a single line of test code.

| SDK | Language | Runtime | Test Framework | Build |
|-----|----------|---------|---------------|-------|
| `sdk/` | Python 3.11+ | CPython | pytest + pytest-asyncio (`asyncio_mode = "auto"`) + pytest-cov | n/a |
| `sdk-ts/` | TypeScript 5.0+ | Node 18+ | vitest, Playwright (E2E) | tsup |

Exact test commands (copy-paste ready):

Python unit/integration:
  cd /Users/francescorosciano/docs/patter/Patter/sdk && python3 -m pytest tests/ -v --cov=patter_sdk --cov-report=term-missing

Python soak:
  cd /Users/francescorosciano/docs/patter/Patter/sdk && python3 -m pytest tests/soak/ -v -m soak

Python security:
  cd /Users/francescorosciano/docs/patter/Patter/sdk && python3 -m pytest tests/security/ -v -m security

TypeScript unit/integration:
  cd /Users/francescorosciano/docs/patter/Patter/sdk-ts && npm test && npm run lint && npm run build

TypeScript coverage:
  cd /Users/francescorosciano/docs/patter/Patter/sdk-ts && npx vitest run --coverage

TypeScript E2E (Playwright):
  cd /Users/francescorosciano/docs/patter/Patter/sdk-ts && npx playwright test

TypeScript soak:
  cd /Users/francescorosciano/docs/patter/Patter/sdk-ts && npx vitest run tests/soak --reporter=verbose

Cross-SDK parity:
  cd /Users/francescorosciano/docs/patter/Patter && python3 tests/parity/run.py

## Mandatory: Use an Agent Team

This task touches both SDKs across six test categories and must be executed using a parallel agent team. Do not attempt to write all tests sequentially in a single session. Follow these steps exactly:

1. Call `TeamCreate` with `team_name: "patter-test-suite"` and description: "Build comprehensive test suite for Patter Python and TypeScript SDKs."

2. Call `TaskCreate` for each of the following work units (one task per logical unit — create all tasks before spawning any workers):
   - Task A: Python unit + integration tests for `sdk/`
   - Task B: TypeScript unit + integration tests for `sdk-ts/`
   - Task C: Playwright E2E tests for `sdk-ts/` (local-mode dashboard + call simulation)
   - Task D: Soak/stress/load harness for both SDKs
   - Task E: Security test suite (SSRF, XSS, E.164 fuzz, TwiML injection, secret leakage)
   - Task F: Cross-SDK parity test suite (`tests/parity/`)
   - Task G: ExecPlan authoring (`docs/2026-04-11-PLAN-build-test-suite.md`)
   - Task H: CI workflow update (`.github/workflows/test.yml`)

3. Spawn the following workers in parallel using the `Agent` tool. Pass `team_name: "patter-test-suite"` and a distinct `name` to each. Assign each worker its task(s) via `TaskUpdate` (set `owner` to the worker's name and `status` to `in_progress`):
   - `python-test-author` (`subagent_type: general-purpose`) — owns Task A
   - `ts-test-author` (`subagent_type: general-purpose`) — owns Task B
   - `e2e-author` (`subagent_type: general-purpose`) — owns Task C
   - `soak-stress-author` (`subagent_type: general-purpose`) — owns Task D
   - `security-test-author` (`subagent_type: general-purpose`) — owns Task E
   - `parity-test-author` (`subagent_type: general-purpose`) — owns Task F
   - `tdd-guide` (`subagent_type: tdd-guide`) — supervises RED-first discipline across all authoring workers; reviews each test file before it is marked done
   - `code-reviewer` (`subagent_type: code-reviewer`) — owns a final review pass (Task H's review gate) after all test files are written

4. Each authoring worker must send a `SendMessage` to the team lead when its task is complete or when it is blocked. Do not proceed to verification until all authoring workers have reported completion.

5. After all workers report completion, run the full verification sequence in the "## Verification" section below. Once all acceptance criteria pass, send `SendMessage` with `type: "shutdown_request"` to each worker by name, then commit.

## Instructions

1. Read `/Users/francescorosciano/docs/patter/Patter/CLAUDE.md`, `.claude/rules/testing.md`, `.claude/rules/py-testing.md`, and `.claude/rules/ts-testing.md` in full before writing any code.

2. Author an ExecPlan at `docs/2026-04-11-PLAN-build-test-suite.md` following the format in `.claude/rules/plans.md`. The ExecPlan must include:
   - Every module to be covered with the full relative file path
   - A test matrix: providers (Twilio, Telnyx) × voice modes (Realtime, ConvAI, Pipeline) showing which integration test covers each cell
   - All soak scenarios with success criteria (duration, memory bound, concurrency level)
   - All security scenarios with a positive and a negative test case per scenario
   - All parity scenarios with the shared scenario JSON schema
   - Milestones, each independently verifiable
   - Progress, Decision Log, Surprises & Discoveries, and Outcomes & Retrospective sections (initially empty, to be filled in)
   Wait for explicit user approval of the ExecPlan before writing any test code.

3. For each Python hot file, write unit tests that cover the following. All Python tests must be marked with `@pytest.mark.unit` or `@pytest.mark.integration` per `py-testing.md`. Tests go in `sdk/tests/` under a subdirectory mirroring the source layout.

   `sdk/patter_sdk/client.py` (31 KB, 44+ async methods):
   - Happy path for each public async method (mock network boundary at `websockets.connect` and `httpx.AsyncClient`)
   - Error path: connection refused, timeout, mid-stream disconnect
   - Async cancellation: `asyncio.CancelledError` propagation without resource leak
   - State-machine transitions (idle → connecting → connected → disconnected)

   `sdk/patter_sdk/handlers/stream_handler.py` (1054 lines):
   - Audio frame queuing and ordering under backpressure
   - PCM and mulaw codec path selection
   - Tool webhook dispatch: 3× retry logic, 10-second timeout enforcement, 10-iteration cap
   - Concurrent `StreamHandler` instances do not share state
   - SSE broadcast correctness and the deadlock regression fixed in v0.3.0 (simulate rapid subscribe/unsubscribe during active broadcast)

   `sdk/patter_sdk/handlers/twilio_handler.py`:
   - TwiML response parsing and generation
   - WebSocket upgrade handshake
   - Error from Twilio webhook (non-200, malformed body)

   `sdk/patter_sdk/handlers/telnyx_handler.py`:
   - Telnyx-specific event envelope parsing
   - Call control commands (answer, hangup, transfer)
   - Webhook signature verification (valid and invalid HMAC)

   `sdk/patter_sdk/services/llm_loop.py`:
   - Turn completion detection
   - Streaming token accumulation
   - Tool-call extraction from partial JSON
   - Cost and token accumulation (precision over 1000 synthetic turns)

   `sdk/patter_sdk/services/metrics.py`:
   - 500-call circular buffer: wrap-around correctness (501st entry overwrites oldest)
   - Concurrent write safety (two coroutines writing simultaneously)
   - Metric aggregation (mean latency, p95, p99) correctness against known values

   `sdk/patter_sdk/dashboard/store.py`:
   - Read/write isolation between subscribers
   - Pub/sub fan-out: 10 simultaneous subscribers each receive the same update exactly once

   `sdk/patter_sdk/models.py`:
   - Pydantic validation: required fields, optional fields, discriminated unions
   - Reject invalid E.164 phone numbers
   - Round-trip serialization (model → JSON → model produces identical object)

4. For each TypeScript hot file, write unit tests using vitest. All tests go in `sdk-ts/tests/` mirroring the source layout. Use `sdk-ts/tests/setup.ts` for shared mocks.

   `sdk-ts/src/client.ts` (17 KB):
   - Same coverage targets as the Python client: happy path, error paths, async abort via `AbortController`, state transitions

   `sdk-ts/src/stream-handler.ts` (681 lines):
   - Audio buffer management
   - Tool webhook retry/timeout/cap parity with Python handler
   - SSE deadlock regression scenario (rapid subscribe/unsubscribe)

   `sdk-ts/src/server.ts` (33 KB):
   - Route registration and middleware ordering
   - Request/response serialization
   - Error middleware produces expected JSON shape

   `sdk-ts/src/services/llm-loop.ts`:
   - Same coverage targets as Python `llm_loop.py`

   `sdk-ts/src/services/metrics.ts`:
   - Same circular-buffer and aggregation targets as Python `metrics.py`

   `sdk-ts/src/dashboard/store.ts`:
   - Same pub/sub targets as Python `dashboard/store.py`

5. Integration matrix — write at least one integration test per cell below, mocking at the network boundary (no real calls). Network boundaries to mock: `websockets.connect` / `ws.WebSocket` constructors, `httpx.AsyncClient` / `fetch`, Twilio and Telnyx webhook HTTP endpoints.

   | Provider | Voice Mode | Python test file | TS test file |
   |----------|-----------|-----------------|--------------|
   | Twilio | Realtime | `sdk/tests/integration/test_twilio_realtime.py` | `sdk-ts/tests/integration/twilio-realtime.test.ts` |
   | Twilio | ConvAI | `sdk/tests/integration/test_twilio_convai.py` | `sdk-ts/tests/integration/twilio-convai.test.ts` |
   | Twilio | Pipeline | `sdk/tests/integration/test_twilio_pipeline.py` | `sdk-ts/tests/integration/twilio-pipeline.test.ts` |
   | Telnyx | Realtime | `sdk/tests/integration/test_telnyx_realtime.py` | `sdk-ts/tests/integration/telnyx-realtime.test.ts` |
   | Telnyx | ConvAI | `sdk/tests/integration/test_telnyx_convai.py` | `sdk-ts/tests/integration/telnyx-convai.test.ts` |
   | Telnyx | Pipeline | `sdk/tests/integration/test_telnyx_pipeline.py` | `sdk-ts/tests/integration/telnyx-pipeline.test.ts` |

6. E2E scenarios using Playwright against the `sdk-ts` local-mode dashboard (start the local server before running). Write tests in `sdk-ts/tests/e2e/`. Each scenario must be independently runnable. Cover:
   - Inbound call: simulate a Twilio webhook POST to `/incoming`, assert the dashboard updates, assert the call reaches "connected" state
   - Outbound call: trigger outbound call via dashboard UI, assert status transitions visible in the dashboard
   - Tool calling mid-conversation: inject a tool-call event, assert the tool webhook is dispatched and the response is reflected in the transcript view
   - Call transfer: trigger transfer, assert transfer request is sent and original call reaches "transferred" state
   - Recording: enable recording, assert recording start/stop events appear in the dashboard
   - Machine detection: inject an AMD (answering machine detection) event, assert the dashboard reflects the detection result

7. Soak/stress harness — write in `sdk/tests/soak/test_soak.py` (Python) and `sdk-ts/tests/soak/soak.test.ts` (TypeScript). Use `psutil` for Python memory measurement and `process.memoryUsage()` for Node.

   Scenario S1 — 100 concurrent calls for 10 minutes:
   Spawn 100 mock call coroutines/workers simultaneously, each sending 1 audio frame per second. Measure RSS (resident set size) at start and at 10 minutes. Assert growth < 10% of initial. Assert no unhandled exceptions or dropped frames.

   Scenario S2 — 1000-turn conversation:
   Simulate a single call with 1000 LLM turns. Assert total accumulated cost and token count are arithmetically correct (compare against hand-computed expected values for 10 turns, then scale). Assert no memory leak across turns.

   Scenario S3 — WebSocket reconnection under network flapping:
   Simulate 20 consecutive disconnect/reconnect cycles with a 50 ms gap. Assert all in-flight audio frames are flushed or explicitly dropped (no silent loss), and the handler returns to "connected" state within 500 ms of each reconnect.

   Scenario S4 — SSE subscriber churn:
   Spin up 50 subscribers that each subscribe and unsubscribe within 100 ms, during which the broadcaster emits 10 events. Assert every subscriber that was connected during an event received it exactly once, and no deadlock occurs (timeout the whole scenario at 30 seconds).

   Scenario S5 — 500-call buffer wrap:
   Write 501 call records to the metrics store. Assert the oldest record (index 0) has been evicted and the newest 500 are present and in order.

   Scenario S6 — Cost precision over 1000 turns:
   Accumulate fractional token costs over 1000 synthetic turns with known per-turn values. Assert the final accumulated cost equals the analytically correct sum within a tolerance of 1e-9.

8. Security test suite — write in `sdk/tests/security/test_security.py` (Python) and `sdk-ts/tests/security/security.test.ts` (TypeScript). Each scenario needs one positive test (attack blocked) and one negative test (legitimate input accepted).

   Scenario SEC-1 — SSRF on user-supplied webhook URLs:
   Attempt to register a webhook URL pointing to `http://169.254.169.254/latest/meta-data/` (AWS metadata endpoint) and to `http://localhost:8080/internal`. Assert both are rejected with a clear error. Assert a valid public HTTPS URL is accepted.

   Scenario SEC-2 — XSS injection in dashboard fields:
   Submit `<script>alert(1)</script>` as a caller ID, agent name, and transcript entry. Assert the stored value is either rejected or stored as escaped plain text. Assert no raw `<script>` tag appears in any API response body.

   Scenario SEC-3 — E.164 phone number fuzzing:
   Submit the following inputs as phone numbers and assert each is rejected: empty string, `+`, `+1`, `+0000000000000000` (too long), `+123abc456`, `null`, `undefined` (as a string), a 10 MB string. Assert a valid E.164 number (`+14155552671`) is accepted.

   Scenario SEC-4 — TwiML payload injection:
   Submit a TwiML body containing `<Redirect>http://attacker.example/evil</Redirect>` nested inside a `<Response>`. Assert the payload is either rejected or the injected verb is stripped before processing.

   Scenario SEC-5 — Secret leakage in logs and error messages:
   Trigger an intentional error (invalid credentials, missing env var) while a log capture fixture is active. Assert no API keys, tokens, or passwords matching the pattern `[A-Za-z0-9+/]{20,}` appear in any log line or error message body.

9. Cross-SDK parity suite — write in `tests/parity/` at the repo root. Create `tests/parity/scenarios/` containing JSON files that define shared input/output contracts. Create `tests/parity/run.py` as the entry point.

   Parity scenario JSON schema (one file per scenario):

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

   Write parity scenarios covering at minimum: call initialization, audio frame submission, LLM turn completion detection, metric recording, dashboard store subscribe/publish, tool webhook dispatch, model serialization (E.164 phone, call status enum, voice mode enum). Target ≥ 80% of the shared public API surface (compare `sdk/patter_sdk/__init__.py` exports against `sdk-ts/src/index.ts` exports to determine the surface).

   `run.py` must: load all JSON scenario files, invoke the Python SDK method with the scenario's input, invoke the TypeScript SDK method via `subprocess` running a small Node.js shim (`tests/parity/ts_shim.js`), compare outputs, and report pass/fail with a diff when they diverge.

10. Test infrastructure to add (these are prerequisites — add them before authoring any tests that need them):
    - `sdk/tests/conftest.py`: fixtures for mock WebSocket server (asyncio-based), mock LLM that returns scripted responses, mock Twilio webhook sender, mock Telnyx webhook sender, fake PCM and mulaw audio frame generators
    - `sdk-ts/tests/setup.ts`: shared vitest setup with `vi.mock` stubs for `ws`, `express`, the Twilio and Telnyx clients, and a fake audio frame helper
    - `sdk/tests/soak/` directory with `__init__.py`
    - `sdk/tests/security/` directory with `__init__.py`
    - `tests/parity/` directory at repo root with `__init__.py`, `run.py`, `ts_shim.js`, and `scenarios/` subdirectory

11. Update `.github/workflows/test.yml`:
    - Add an `e2e` job that runs `npx playwright install --with-deps && npx playwright test` on Node 20 only (not a matrix, to keep CI fast)
    - Add a `security` job that runs `python3 -m pytest tests/security/ -v -m security` on Python 3.11 only
    - Add a `soak` job with `on: workflow_dispatch` (manual trigger only, not on push/PR) that runs both the Python and TypeScript soak suites on a dedicated runner with label `soak-runner`; the job must export memory measurements as a workflow artifact
    - Do not change the existing `test` matrix jobs

12. Run the `tdd-guide` agent on every test file immediately after it is authored, before the file is considered done. Each test must fail (RED) before the corresponding implementation is touched. Run the `code-reviewer` agent on the complete diff of all new test files and CI changes before committing.

## Acceptance Criteria

- [ ] Unit + integration coverage ≥ 80% as reported by `pytest --cov=patter_sdk --cov-report=term-missing` (Python) and `npx vitest run --coverage` (TypeScript). Paste the coverage summary lines in the PR description.
- [ ] All 28 previously existing Python tests and 22 previously existing TypeScript tests still pass unchanged after the new tests are added.
- [ ] At least 1 passing E2E Playwright scenario each for: inbound call, outbound call, and tool-calling mid-conversation.
- [ ] Soak harness S1 completes 100 concurrent calls for 10 minutes with resident memory growth < 10% (measured by `psutil` / `process.memoryUsage()`). Output line must read: `Memory growth: X.X% (threshold: 10.0%) PASS`.
- [ ] Security suite SEC-1 through SEC-5 each have at least one positive test (attack blocked) and one negative test (legitimate input accepted); all 10+ tests pass.
- [ ] Cross-SDK parity suite reports ≥ 80% of shared public API methods produce identical outputs for all defined scenarios. Output line must read: `Parity: N/M methods matched (X.X%) PASS`.
- [ ] CI pipeline is green on the full Python matrix (3.11, 3.12, 3.13) and the full Node matrix (18, 20, 22) for the existing `test` job. The new `e2e` and `security` jobs are present and green. The `soak` job exists with `on: workflow_dispatch`.
- [ ] ExecPlan at `docs/2026-04-11-PLAN-build-test-suite.md` has been updated with completed Progress checkboxes, a populated Decision Log, and a filled-in Outcomes & Retrospective section.
- [ ] `git diff --name-only main` shows only: new test files, updated conftest/setup infrastructure, updated `.github/workflows/test.yml`, the ExecPlan file, and any necessary `pytest.ini` / `vitest.config.ts` marker registrations. No source files changed.

## Commit

test: add comprehensive test suite (unit/integration/e2e/soak/security/parity)

## Verification

Run this sequence end-to-end after all test files are written. Do not commit until every check passes.

Step 1 — Python unit/integration coverage:
  cd /Users/francescorosciano/docs/patter/Patter/sdk
  python3 -m pytest tests/ -v --cov=patter_sdk --cov-report=term-missing 2>&1 | tee /tmp/py-cov.txt
  grep "TOTAL" /tmp/py-cov.txt

Expected output (line ending may vary): `TOTAL   ... 80%` or higher. If below 80%, add tests before proceeding.

Step 2 — Python soak:
  cd /Users/francescorosciano/docs/patter/Patter/sdk
  python3 -m pytest tests/soak/ -v -m soak 2>&1 | tee /tmp/py-soak.txt
  grep "Memory growth" /tmp/py-soak.txt

Expected: `Memory growth: X.X% (threshold: 10.0%) PASS` for each soak scenario.

Step 3 — Python security:
  cd /Users/francescorosciano/docs/patter/Patter/sdk
  python3 -m pytest tests/security/ -v -m security

Expected: all tests pass; no `FAILED` lines; output ends with `N passed`.

Step 4 — TypeScript unit/integration + coverage:
  cd /Users/francescorosciano/docs/patter/Patter/sdk-ts
  npm test && npm run lint && npm run build
  npx vitest run --coverage 2>&1 | tee /tmp/ts-cov.txt
  grep "All files" /tmp/ts-cov.txt

Expected: `All files | ... | 80%` or higher in the Stmts column.

Step 5 — TypeScript E2E (Playwright):
  cd /Users/francescorosciano/docs/patter/Patter/sdk-ts
  npx playwright install --with-deps
  npx playwright test

Expected output ending: `N passed (Xs)` with N ≥ 3 (inbound, outbound, tool-calling).

Step 6 — TypeScript soak:
  cd /Users/francescorosciano/docs/patter/Patter/sdk-ts
  npx vitest run tests/soak --reporter=verbose 2>&1 | tee /tmp/ts-soak.txt
  grep "Memory growth" /tmp/ts-soak.txt

Expected: `Memory growth: X.X% (threshold: 10.0%) PASS`.

Step 7 — Cross-SDK parity:
  cd /Users/francescorosciano/docs/patter/Patter
  python3 tests/parity/run.py 2>&1 | tee /tmp/parity.txt
  grep "Parity:" /tmp/parity.txt

Expected: `Parity: N/M methods matched (X.X%) PASS` with X.X ≥ 80.

Step 8 — Confirm no source files changed:
  cd /Users/francescorosciano/docs/patter/Patter
  git diff --name-only main | grep -v "^sdk/tests" | grep -v "^sdk-ts/tests" | grep -v "^tests/parity" | grep -v "^docs/2026-04-11-PLAN" | grep -v "^\.github/workflows/test\.yml" | grep -v "conftest\.py" | grep -v "setup\.ts" | grep -v "pytest\.ini" | grep -v "vitest\.config"

Expected: no output. Any unexpected file listed here means source code was inadvertently modified — revert before committing.

Step 9 — Commit only after all checks pass:
  cd /Users/francescorosciano/docs/patter/Patter
  git add sdk/tests/ sdk-ts/tests/ tests/parity/ docs/2026-04-11-PLAN-build-test-suite.md .github/workflows/test.yml
  git commit -m "test: add comprehensive test suite (unit/integration/e2e/soak/security/parity)"

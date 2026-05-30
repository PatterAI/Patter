# Testing Rule

> **Read first**: [authentic-tests.md](./authentic-tests.md) — tests must verify real behaviour, not mock-on-mock. Coverage below is meaningless if tests aren't authentic.

## Minimum coverage: 80%

Applies to both SDKs. Measured by:
- Python: `pytest --cov=patter --cov-report=term-missing`
- TypeScript: `npm test -- --coverage` (vitest + `@vitest/coverage-v8`)

## Required test types

| Layer | Python location | TS location | Notes |
|-------|-----------------|-------------|-------|
| Unit | `sdk/tests/test_*.py` | `sdk-ts/tests/*.test.ts` | Real code, no external boundary mocked |
| Integration | `sdk/tests/integration/` | `sdk-ts/tests/*.integration.test.ts` | Real local services (FastAPI, websockets.serve) |
| Mocked | `@pytest.mark.mocked` | `sdk-ts/tests/*.mocked.test.ts` | ONLY when mocking paid/external boundaries — MUST be tagged |
| E2E / Cross-SDK | `tests/` (repo root) | `tests/` (repo root) | Real customer use cases end-to-end |

## Conventions

### Python (pytest)
- `asyncio_mode = "auto"` — do NOT add `@pytest.mark.asyncio`; it's the default.
- Use `pytest.fixture` for setup, not class `setUp`.
- Mark slow tests with `@pytest.mark.soak` — excluded by default CI run.
- Never hit real network. Mock WebSocket servers with `pytest-asyncio` + `websockets.serve`.

### TypeScript (vitest)
- Use `describe` / `it` / `expect` — no jest imports.
- Mock WebSockets with test doubles, never hit provider endpoints in CI.
- `beforeEach` / `afterEach` for setup/teardown, async where needed.

## TDD expectation

For new features:
1. Write the failing test first (RED).
2. Implement to pass (GREEN).
3. Refactor (IMPROVE).
4. Parity: add the same test in the other SDK.

## Never test

- Real phone calls (examples do that, CI doesn't).
- Real provider APIs (rate limits, flakiness, cost).
- File system state outside `tests/fixtures/`.

## Fixing a failure

1. Read the error carefully — tests are usually right, implementation is usually wrong.
2. If the test is wrong (asserts old behaviour after intentional change), update BOTH SDK's equivalent test.
3. Never delete a failing test to "get CI green". If a test is flaky, mark it `soak` or fix the race condition.

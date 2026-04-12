# TypeScript Dashboard — Test Report

**Run at:** 2026-04-12T03:23:25Z
**Working dir:** /Users/francescorosciano/docs/patter/Patter/sdk-ts
**Command:** `npx vitest run tests/dashboard-auth.test.ts tests/dashboard-export.test.ts tests/dashboard-store.test.ts --reporter=verbose`
**Runtime:** 22s

## Summary
- Test files: 3 passed / 0 failed
- Tests: 25 passed / 0 failed / 0 skipped
- Total: 25
- Exit code: 0

## Failures
None.

## Full output (tail)
```
The CJS build of Vite's Node API is deprecated. See https://vite.dev/guide/troubleshooting.html#vite-cjs-node-api-deprecated for more details.

 RUN  v2.1.9 /Users/francescorosciano/docs/patter/Patter/sdk-ts

 ✓ tests/dashboard-export.test.ts > callsToCsv > produces valid CSV with headers
 ✓ tests/dashboard-export.test.ts > callsToCsv > handles empty call list
 ✓ tests/dashboard-export.test.ts > callsToCsv > handles calls without metrics
 ✓ tests/dashboard-export.test.ts > callsToJson > produces valid JSON
 ✓ tests/dashboard-export.test.ts > callsToJson > handles empty list
 ✓ tests/dashboard-auth.test.ts > makeAuthMiddleware > allows all requests when no token configured
 ✓ tests/dashboard-auth.test.ts > makeAuthMiddleware > allows valid bearer token
 ✓ tests/dashboard-auth.test.ts > makeAuthMiddleware > allows valid query token
 ✓ tests/dashboard-auth.test.ts > makeAuthMiddleware > rejects invalid token
 ✓ tests/dashboard-auth.test.ts > makeAuthMiddleware > rejects missing token when required
 ✓ tests/dashboard-store.test.ts > MetricsStore > records call start and tracks active calls
 ✓ tests/dashboard-store.test.ts > MetricsStore > records call end and moves to completed
 ✓ tests/dashboard-store.test.ts > MetricsStore > records turns on active calls
 ✓ tests/dashboard-store.test.ts > MetricsStore > enforces max calls limit
 ✓ tests/dashboard-store.test.ts > MetricsStore > getCalls returns newest first
 ✓ tests/dashboard-store.test.ts > MetricsStore > getCall returns specific call
 ✓ tests/dashboard-store.test.ts > MetricsStore > computes aggregates
 ✓ tests/dashboard-store.test.ts > MetricsStore > returns empty aggregates for no calls
 ✓ tests/dashboard-store.test.ts > MetricsStore > emits SSE events
 ✓ tests/dashboard-store.test.ts > MetricsStore > filters calls by time range
 ✓ tests/dashboard-store.test.ts > MetricsStore > getCallsInRange returns all calls when no bounds given
 ✓ tests/dashboard-store.test.ts > MetricsStore > SSE subscriber receives correct event data
 ✓ tests/dashboard-store.test.ts > MetricsStore > max calls cap keeps only the newest entries
 ✓ tests/dashboard-store.test.ts > MetricsStore > ignores recordCallStart with empty call_id
 ✓ tests/dashboard-store.test.ts > MetricsStore > ignores recordTurn with missing turn data

 Test Files  3 passed (3)
      Tests  25 passed (25)
   Start at  20:22:55
   Duration  8.50s (transform 56ms, setup 0ms, collect 64ms, tests 10ms, environment 0ms, prepare 133ms)
```

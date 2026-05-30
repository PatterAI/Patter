# Async Everywhere Rule

All I/O is async. The SDK services real-time phone calls — blocking the event loop drops audio.

## Python

- Every public method that does I/O is `async def`.
- Use `asyncio` primitives: `asyncio.create_task`, `asyncio.Queue`, `asyncio.Event`.
- `asyncio.gather(...)` for parallel work; never chain with `.then()` patterns.
- No `time.sleep` — always `await asyncio.sleep(...)`.
- HTTP: `httpx.AsyncClient`, not `requests`. WebSocket: `websockets.connect`, not `websocket-client`.
- Tests: `asyncio_mode = "auto"` (already configured) — just write `async def test_*`.

## TypeScript

- Every public method returning I/O returns `Promise<T>`.
- Prefer `async/await`; no raw `.then()` chains except for fire-and-forget at the edges.
- No `setTimeout`-based polling — use Promise-returning abstractions.
- HTTP: global `fetch` (Node 18+). WebSocket: `ws` package.
- Tests: `vitest` runs async tests natively.

## Never block

### Python

- Do NOT call `.result()` or `.wait()` on a future from inside the event loop.
- Do NOT hold the GIL for >10ms with CPU work — move to `asyncio.to_thread` or a process pool.
- Audio transcoding: `services/transcoding.py` uses numpy; bulk ops are fast — profile before moving.

### TypeScript

- Do NOT use sync filesystem APIs in request-handling paths (`fs.readFileSync` etc.).
- Do NOT use `for (const x of await array)` with a large array — use `Promise.all` for concurrency.

## Cancellation

Every long-running task must be cancellable:
- Python: check `asyncio.CancelledError` in finally; clean up WebSocket.
- TS: use `AbortController` / `AbortSignal`; forward signal to `fetch` and `WebSocket` close handlers.

## Error propagation

- Errors in background tasks MUST be surfaced — do not fire-and-forget without a `try/except` that at minimum `logger.error`s.
- Python: wrap `asyncio.create_task(...)` results; add a `done_callback` that logs exceptions.
- TS: `.catch()` every floating promise at the edge.

## Verification

- Python: `asyncio.get_event_loop().slow_callback_duration = 0.1`; warn on slow callbacks in dev.
- TS: `npm run lint` with `@typescript-eslint/no-floating-promises` if ESLint is later added.

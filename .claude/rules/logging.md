# Logging Rule

Logs must be safe (no PII), structured, and consistent across SDKs.

## Python: `logging` module only

```python
import logging

logger = logging.getLogger("patter")   # One namespace, always.
# NEVER use `print()` in library code — it breaks users' output capture.
```

Sub-namespaces allowed:
- `patter.handlers`, `patter.providers`, `patter.metrics`, `patter.dashboard`

## TypeScript: `console.*`

```ts
console.log(...)    // info-level
console.warn(...)   // recoverable issue
console.error(...)  // error we didn't handle gracefully
```

No `console.debug` in shipped code (noisy; use explicit guard if needed).

## Levels

| Level | When |
|-------|------|
| `debug` / not used in prod | Internal state inspection during dev |
| `info` / `console.log` | Call lifecycle events (call start, call end, transfer) |
| `warning` / `console.warn` | Retryable error, fallback taken, soft-limit hit |
| `error` / `console.error` | Unrecoverable for this call; call ended abnormally |

## PII: never log

**NEVER log:**
- Full audio frames (too large + privacy).
- Full transcripts at INFO. Only redacted summaries (`"transcript_len=140"`).
- API keys, webhook secrets, session tokens.
- Full phone numbers at INFO. Last-4 only: `+1***5678`.

**OK to log:**
- Call ID (UUID), session ID.
- Provider name, model name, voice name.
- Latency metrics, token counts, cost estimates.
- Error class + message (without sensitive data).

## Structured messages

Prefer key=value pairs within a single log line:

```python
logger.info("call_start call_id=%s provider=%s carrier=%s", call_id, "openai_realtime", "twilio")
```

```ts
console.log(`call_start call_id=${callId} provider=openai_realtime carrier=twilio`);
```

This keeps logs grep-friendly without pulling in a structured logging dep.

## Dashboard has its own path

The in-memory `MetricsStore` and the dashboard UI are NOT logs — they're structured metrics. Don't duplicate: log the event once, and push the metric object once. See `dashboard/store.py` / `dashboard/store.ts`.

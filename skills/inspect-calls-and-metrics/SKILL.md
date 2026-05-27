---
name: inspect-calls-and-metrics
description: >
  Inspect Patter call data — mount the live dashboard, read CallMetrics
  (duration, cost, latency, transcript), export call history to CSV/JSON,
  and track per-call provider costs. Use when the user wants to debug a
  call, see which calls cost the most, audit transcripts, monitor a live
  agent, find a recording URL, check latency p99, export call history for
  reporting, or watch the dashboard during a demo. Covers Patter 0.6.2's
  in-memory MetricsStore + 500-call ring buffer, the FastAPI/Express
  dashboard mount, the REST API and SSE stream — in both Python and
  TypeScript.
license: MIT
compatibility: >
  Requires Patter >= 0.6.2 and a running Patter server (`phone.serve(...)`).
  Dashboard auth (basic auth) is recommended when serving on anything
  other than 127.0.0.1.
metadata:
  author: patter
  version: "0.1.0"
  parity: both
---

# Inspect calls and metrics with Patter

Patter persists every call to an in-memory `MetricsStore` (500-call ring
buffer by default), optionally backed by disk. The dashboard surfaces
live calls, transcripts, latency breakdowns, per-leg cost, and recordings.
You can also pull `CallMetrics` programmatically and export to CSV/JSON
for offline analysis.

## Mount the live dashboard

Patter ships a dashboard route you can mount on the same server as your
agent. Visit `http://localhost:8000/dashboard` to see live calls.

### Python

```python
import asyncio
from getpatter import Patter, Twilio, OpenAIRealtime2

async def main():
    phone = Patter(carrier=Twilio(), phone_number="+15550001234")
    agent = phone.agent(
        engine=OpenAIRealtime2(),
        system_prompt="...",
        first_message="Hi!",
    )
    # dashboard=True mounts /dashboard (UI) + /api/calls (REST) + /sse (live stream)
    await phone.serve(agent, tunnel=True, dashboard=True)

asyncio.run(main())
```

### TypeScript

```typescript
import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  engine: new OpenAIRealtime2(),
  systemPrompt: "...",
  firstMessage: "Hi!",
});

await phone.serve({ agent, tunnel: true, dashboard: true });
```

Open `http://localhost:8000/dashboard`. Live calls appear at the top, with
real-time transcript, current cost, and latency p50/p90/p95/p99.

## Read metrics in code

`CallMetrics` is the canonical model — every finished call produces one.
The hook is **`on_call_end`** passed as a kwarg to `phone.serve(...)`. It
receives a `dict` (the `CallMetrics` serialized form), and is async.

### Python

```python
import asyncio
from getpatter import Patter, Twilio, OpenAIRealtime2

phone = Patter(carrier=Twilio(), phone_number="+15550001234")

async def on_end(metrics: dict) -> None:
    print(f"Call {metrics['call_id']} | {metrics['duration_seconds']:.1f}s")
    cost = metrics.get("cost", {})
    print(f"  Cost ${cost.get('total_usd', 0):.4f}: "
          f"STT ${cost.get('stt_usd', 0):.4f} · "
          f"LLM ${cost.get('llm_usd', 0):.4f} · "
          f"TTS ${cost.get('tts_usd', 0):.4f} · "
          f"Realtime ${cost.get('realtime_usd', 0):.4f} · "
          f"Telephony ${cost.get('telephony_usd', 0):.4f}")
    print(f"  Latency p99: {metrics.get('latency_p99', 0):.0f} ms")

agent = phone.agent(engine=OpenAIRealtime2(), system_prompt="...", first_message="Hi!")
asyncio.run(phone.serve(agent, tunnel=True, on_call_end=on_end))
```

### TypeScript

```typescript
import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });

const agent = phone.agent({
  engine: new OpenAIRealtime2(),
  systemPrompt: "...",
  firstMessage: "Hi!",
});

await phone.serve({
  agent,
  tunnel: true,
  onCallEnd: async (metrics) => {
    console.log(`Call ${metrics.call_id} | ${metrics.duration_seconds.toFixed(1)}s`);
    console.log(`  Cost $${metrics.cost?.total_usd?.toFixed(4) ?? "0"}`);
    console.log(`  Latency p99: ${metrics.latency_p99?.toFixed(0) ?? "0"} ms`);
  },
});
```

You can also pull live data programmatically from the `MetricsStore`
attached to the server — `phone.metrics_store` (Python) /
`phone.metricsStore` (TypeScript). It returns `None`/`null` until
`serve()` has bound; once the server is live, you can iterate calls.

## Export call history

`calls_to_csv` and `calls_to_json` operate on the `MetricsStore` exposed on
the running server.

### Python

```python
from getpatter import calls_to_csv, calls_to_json

store = phone.metrics_store          # available after serve() has bound
if store is not None:
    with open("calls.csv", "w") as f:
        f.write(calls_to_csv(store))
    import json
    with open("calls.json", "w") as f:
        json.dump(calls_to_json(store), f, indent=2)
```

### TypeScript

```typescript
import { callsToCsv, callsToJson } from "getpatter";
import { writeFileSync } from "fs";

const store = phone.metricsStore;     // available after serve() has bound
if (store) {
  writeFileSync("calls.csv", callsToCsv(store));
  writeFileSync("calls.json", JSON.stringify(callsToJson(store), null, 2));
}
```

## REST API (when dashboard is mounted)

| Route | Purpose |
|---|---|
| `GET /api/calls` | List recent calls (newest first). |
| `GET /api/calls/{call_id}` | Detailed metrics + transcript for one call. |
| `GET /sse` | Server-Sent Events stream of live call updates. |

Example with curl:

```bash
curl http://localhost:8000/api/calls | jq '.[] | {id: .call_id, cost: .cost.total_usd, duration: .duration_seconds}'
```

## Persist to disk (survive restarts)

By default the ring buffer is in-memory. Pass `persist=True` (or a path) to
mirror to disk:

### Python

```python
phone = Patter(
    carrier=Twilio(),
    phone_number="+15550001234",
    persist=True,                  # writes to ./.patter/calls.db
    # persist="/var/patter/calls.db",  # or a custom path
)
```

### TypeScript

```typescript
const phone = new Patter({
  carrier: new Twilio(),
  phoneNumber: "+15550001234",
  persist: true,                   // writes to ./.patter/calls.db
});
```

## Lock the dashboard down (bearer token)

When exposing the dashboard beyond `127.0.0.1`, gate it with a bearer token.
Patter ships token auth out of the box via the `dashboard_token` kwarg —
all `/dashboard`, `/api/calls`, and `/sse` routes then require
`Authorization: Bearer <token>`.

### Python

```python
await phone.serve(agent, tunnel=True, dashboard_token="hunter2-rotate-me")
```

### TypeScript

```typescript
await phone.serve({ agent, tunnel: true, dashboardToken: "hunter2-rotate-me" });
```

For programmatic FastAPI/Express embedding (not just `phone.serve`), the
lower-level `make_auth_dependency(...)` (Python) / `makeAuthMiddleware(...)`
(TypeScript) factories are also exported — useful when you mount the
dashboard onto your own app and need a custom auth scheme.

Without auth, **do not bind to `0.0.0.0`** — call transcripts are PII.

## What's inside `CallMetrics`

Key fields (see `models.py` / `metrics.ts` for the full list):

| Field | Meaning |
|---|---|
| `call_id` | UUID, persistent across the call. |
| `duration_seconds` | Total call wall time. |
| `turns` | List of `TurnMetrics` (user/agent each). |
| `cost` | `CostBreakdown(stt_usd, llm_usd, tts_usd, realtime_usd, telephony_usd, total_usd)`. |
| `latency_avg / p50 / p90 / p95 / p99` | Turn-time percentiles in ms. |
| `provider_mode` | `"openai_realtime"`, `"elevenlabs_convai"`, `"pipeline"`. |
| `stt_provider / llm_provider / tts_provider / telephony_provider` | Provider identifiers. |
| `stt_model / llm_model / tts_model` | Model strings. |
| `transcript` | List of `(role, text)` tuples — set when `input_audio_transcription_model` is configured. |

Recording URLs live in the carrier's payload, surfaced in the
`call.recording.saved` log line — they are not exposed as a typed
`CallMetrics` field in 0.6.2. Enable recording with `phone.serve(..., recording=True)`.

## Gotchas

- **Ring buffer is 500 calls by default**. Older calls are evicted from
  memory (still on disk if `persist=True`). For long-running production,
  use the on-disk SQLite to query history.
- **Dashboard exposes transcripts (PII)** — always gate with auth + HTTPS
  in production.
- **`on_call_end` runs synchronously in the event loop**. Don't block —
  push to a queue for slow exporters.
- **Latency p99 == infinity for short calls** with < 100 turns. The
  percentile estimator needs ~100 samples; for shorter calls use
  `latency_avg`.
- **Cost breakdown precision**: provider price tables are baked into
  `pricing.py` at SDK release time. If a provider changes pricing
  mid-quarter, your `cost.*_usd` will drift. Use `merge_pricing({...})`
  to override.

## Common errors

| Symptom | Fix |
|---|---|
| Dashboard 404s | You passed `dashboard=False` to `phone.serve(...)`. The default is `True` — drop the kwarg. |
| Dashboard shows no calls | Server restarted with persistence off. `persist=True` is the default in 0.6.2; verify you didn't override it. |
| Transcript is empty | Realtime mode without `input_audio_transcription_model` set, or guardrail blocked the response. |
| Recording URL is missing from logs | `recording=True` wasn't passed to `phone.serve(...)`, or the carrier doesn't have recording enabled. |
| `cost.total_usd` is 0 | The provider isn't in Patter's pricing table. Override per-provider rates via `merge_pricing({...})` on the `Patter` constructor. |

## Related skills

- [`build-voice-agent`](../build-voice-agent/) — agents must exist before they produce metrics.
- [`add-tools-and-handoffs`](../add-tools-and-handoffs/) — tools' per-call latency shows in the dashboard.

## References

- Patter Dashboard guide: <https://docs.getpatter.com/python-sdk/dashboard> · <https://docs.getpatter.com/typescript-sdk/dashboard>
- Metrics reference: <https://docs.getpatter.com/python-sdk/metrics> · <https://docs.getpatter.com/typescript-sdk/metrics>
- Call logging: <https://docs.getpatter.com/python-sdk/call-logging>
- Pricing module: <https://github.com/PatterAI/Patter/blob/main/libraries/python/getpatter/pricing.py>

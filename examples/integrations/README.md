# Patter integrations — agent-as-tool examples

The `PatterTool` adapter wraps a live `Patter` instance so that **external
agent frameworks** (OpenAI Assistants, Anthropic Claude tool-use, LangChain,
Hermes Agent, MCP) can place real phone calls as a single tool invocation.

## When to use this

Two integration patterns are supported by Patter; this folder shows the
second one.

| Pattern | Where the brain lives | Where Patter sits | Use this when… |
|---|---|---|---|
| **A — Bring-your-own agent** (HTTP/WS endpoint) | Customer's existing service | Patter does STT + LLM proxy + TTS | Customer already runs a voice agent and wants to swap the voice transport but keep their conversation logic. Use `serve({ onMessage: 'https://my-agent.example.com/respond' })` — already supported natively. |
| **B — Phone as a tool** | Customer's text-based agent (LangChain, OpenAI Assistant, Claude, Hermes) | Patter is a tool the agent calls | Customer's agent is text-driven but needs to make phone calls during a conversation. The `PatterTool` adapter lives here. |

## Files

- [`hermes_phone_tool.py`](./hermes_phone_tool.py) — drop-in `tools/patter.py` for [Hermes Agent](https://hermes-agent.nousresearch.com). Auto-registers with the Hermes registry; the agent gets a `make_phone_call` tool that returns `{call_id, status, duration_seconds, transcript, cost_usd}` as JSON.
- [`openai_assistant_phone_tool.ts`](./openai_assistant_phone_tool.ts) — minimal OpenAI Assistants run that dispatches `tool.execute(args)` when the assistant emits a `make_phone_call` tool_call.

## Quick reference

### Schema exporters

```ts
tool.openaiSchema()      // { type: 'function', function: { name, description, parameters } }
tool.anthropicSchema()   // { name, description, input_schema }
tool.hermesSchema()      // { name, description, parameters }   // same JSON-schema as Anthropic's input_schema
```

```py
tool.openai_schema()      # { "type": "function", "function": { ... } }
tool.anthropic_schema()   # { "name", "description", "input_schema": ... }
tool.hermes_schema()      # { "name", "description", "parameters": ... }
tool.register_hermes(registry)   # one-line registration with Hermes' tools.registry
```

### Tool args (JSON-schema, identical across all three)

| field | type | required | notes |
|---|---|---|---|
| `to` | string | ✓ | E.164 phone number |
| `goal` | string | — | becomes the in-call agent's system prompt |
| `first_message` | string | — | first thing the agent speaks on answer |
| `max_duration_sec` | integer | — | hard timeout, default 180s, max 1800s |

### Result envelope

```json
{
  "call_id": "CA…",
  "status": "completed | no-answer | busy | failed | timeout",
  "duration_seconds": 23.1,
  "cost_usd": 0.018,
  "transcript": [
    { "role": "agent", "text": "Hi, I'm calling to…" },
    { "role": "user",  "text": "Sure, go ahead." }
  ],
  "metrics": { "p95_latency_ms": 1576, "cost": { "stt": ..., "tts": ..., "llm": ..., "telephony": ... } }
}
```

### Hermes Agent contract

Hermes' registry expects handlers that take `(args: dict, **kw) -> str` and
return a JSON string (errors as `{"error": "..."}`). `tool.hermes_handler()`
returns exactly that. `register_hermes(registry)` does the wire-up in one
line — no need to call it manually.

## Production deployment

`PatterTool` boots `phone.serve()` once (lazy on first `execute()` or
explicitly via `tool.start()`). Make sure the Patter instance was constructed
with a **stable webhook hostname** in `webhookUrl` — never a tunnel —
because Twilio/Telnyx need a long-lived HTTPS URL.

For local development, use the `tunnel: true` shortcut on the `Patter`
constructor and let it spawn a cloudflared. For production deploys (Fly.io
/ Cloud Run / Fargate behind ALB), set `webhookUrl` to your public hostname
and skip the tunnel entirely.

See `PRODUCTION-RESEARCH-2026-04-26.md` (in `patter-sdk-acceptance/`) for the
full production deployment walkthrough.

---
name: build-voice-agent
description: >
  Build a production voice agent with the Patter SDK — receive or place real
  phone calls answered by an AI agent. Use when the user wants to make a
  phone bot, IVR replacement, voice receptionist, outbound caller, survey
  bot, customer-support voice agent, voice-AI demo, AI cold-caller, or any
  flow where an LLM talks over the telephone. Covers the three Patter modes
  — OpenAI Realtime / Realtime2 (GA, lowest latency), ElevenLabs ConvAI
  (turn-taking), and the STT→LLM→TTS Pipeline (mix any providers) — in both
  Python and TypeScript. Trigger this skill even if the user only says
  "phone bot", "voice AI", "call my customers", or "answer the phone with
  AI" without naming Patter.
license: MIT
compatibility: >
  Requires Patter >= 0.6.2, a configured carrier (Twilio or Telnyx) and
  provider key (OPENAI_API_KEY for Realtime; ELEVENLABS_API_KEY for ConvAI;
  STT+LLM+TTS keys for Pipeline). Use the `setup-patter` skill first if the
  user hasn't installed and configured Patter.
metadata:
  author: patter
  version: "0.1.0"
  parity: both
---

# Build a voice agent with Patter

Patter is an open-source SDK that turns any AI agent into a phone agent. Pick
one of three architectures (Realtime, ConvAI, Pipeline), describe the
behaviour in a system prompt, and run a local server that connects to your
Twilio or Telnyx number — no Patter Cloud, no managed service. The four-line
pattern below is the entire surface.

## Architecture in one diagram

```
┌────────────────────────────────────────────────────────────────────┐
│                          Patter server (local)                      │
│   ┌────────────────┐    ┌───────────────────┐    ┌───────────────┐ │
│   │ Carrier WS     │───▶│  Agent (engine /  │───▶│  Carrier WS   │ │
│   │ in (audio in)  │    │  pipeline) loop   │    │ out (TTS out) │ │
│   └────────────────┘    └───────────────────┘    └───────────────┘ │
│           ▲                                              │          │
│           │                Built-in tools:               │          │
│           │             transfer_call · end_call         │          │
└───────────┼──────────────────────────────────────────────┼──────────┘
            │                                              │
            │ WSS (mulaw 8kHz / pcm 16kHz)                 │
            ▼                                              ▼
       ┌──────────┐                                   ┌──────────┐
       │  Twilio  │  ────────  real phone call  ───── │   User   │
       │  Telnyx  │                                   └──────────┘
       └──────────┘
```

**You write**: system prompt, optional tools, optional guardrails, choice of engine.
**Patter writes**: WebSocket framing, audio transcoding, barge-in, VAD, cost tracking, tunnel, dashboard.

## Decide the mode

| Mode | When to use | Latency | Cost |
|---|---|---|---|
| **`OpenAIRealtime2`** (GA, default) | Default for everything. Lowest latency, single key. | ~200 ms turn | $$$ |
| **`OpenAIRealtime`** | Legacy `gpt-realtime-mini`; pick only if explicitly required. | ~200 ms turn | $$ |
| **`ElevenLabsConvAI`** | Turn-taking conversations (slower but better at long pauses). | ~600 ms turn | $$ |
| **`Pipeline`** | Custom STT/LLM/TTS combinations. Use when the user wants Anthropic Claude / Cerebras / Whisper / a specific TTS voice. | ~800 ms turn | $-$$$$ |

**Default pick**: `OpenAIRealtime2`. Switch only if the user explicitly asks for
custom voice/LLM, lower cost, or a non-OpenAI provider.

## Quick start

### Python

```python
import asyncio
from getpatter import Patter, Twilio, OpenAIRealtime2

async def main():
    phone = Patter(carrier=Twilio(), phone_number="+15550001234")
    agent = phone.agent(
        engine=OpenAIRealtime2(),
        system_prompt=(
            "You are Mia, the AI receptionist for Acme Plumbing. "
            "Greet the caller warmly. Help them book a service visit. "
            "Keep replies under two sentences."
        ),
        first_message="Hi, this is Mia at Acme Plumbing — how can I help?",
    )
    await phone.serve(agent, tunnel=True)

asyncio.run(main())
```

### TypeScript

```typescript
import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";

const phone = new Patter({
  carrier: new Twilio(),
  phoneNumber: "+15550001234",
});

const agent = phone.agent({
  engine: new OpenAIRealtime2(),
  systemPrompt:
    "You are Mia, the AI receptionist for Acme Plumbing. " +
    "Greet the caller warmly. Help them book a service visit. " +
    "Keep replies under two sentences.",
  firstMessage: "Hi, this is Mia at Acme Plumbing — how can I help?",
});

await phone.serve({ agent, tunnel: true });
```

Both expose a tunnel URL on startup (`tunnel ready: https://abc.trycloudflare.com`).
Point your Twilio number's voice webhook at that URL (see `configure-telephony`),
call the number, talk to Mia.

## Pick the right mode

The default works for 80% of agents. Switch when:

| Want this | Pick this mode | Reference |
|---|---|---|
| Lowest possible latency, OpenAI voice | `OpenAIRealtime2` (default) | [references/realtime-mode.md](references/realtime-mode.md) |
| Better long-pause handling, ElevenLabs voice | `ElevenLabsConvAI` | [references/convai-mode.md](references/convai-mode.md) |
| Mix custom STT / LLM / TTS (Anthropic, Deepgram, Cartesia, …) | Pipeline | [references/pipeline-mode.md](references/pipeline-mode.md) |
| Save cost on high-volume outbound | Pipeline with Cerebras LLM + Deepgram STT + ElevenLabs Turbo | [references/pipeline-mode.md](references/pipeline-mode.md) |

Read the corresponding reference only when the user picks that mode — do not
preload all three.

## Outbound calls

Same `Patter` instance does outbound. After `phone.serve(...)`, call:

```python
# Python — machine_detection defaults to True in 0.6.2
await phone.call(
    to="+14155551234",
    agent=agent,
    first_message="Hi, this is Mia from Acme.",
    voicemail_message="Sorry we missed you. Call back at +1...",
)
```

```typescript
// TypeScript
await phone.call({
  to: "+14155551234",
  agent,
  firstMessage: "Hi, this is Mia from Acme.",
  voicemailMessage: "Sorry we missed you. Call back at +1...",
});
```

`machine_detection` is on by default in 0.6.2. Patter auto-detects voicemail
and plays the call's `voicemail_message` before hanging up. A non-empty
`voicemail_message` implicitly enables AMD even if you pass
`machine_detection=False`. Otherwise the live person hears `first_message`
and the agent starts the conversation. See `configure-telephony` for the
full set of outbound options.

## System prompt — what matters for voice

Voice prompts are different from chat prompts. Patter doesn't add hidden
instructions, so be explicit:

1. **Identity**: "You are <name>, calling on behalf of <company>."
2. **Goal**: "Help the caller book a service visit." (one line)
3. **Length rule**: "Keep every reply under two sentences." (LLMs over-explain on voice.)
4. **Stop conditions**: "If the caller wants a human, call `transfer_call`."
5. **Persona**: "Warm, professional, never robotic. Say 'mm-hm' when listening."

The `first_message` is what the agent says immediately on pickup — keep it
to 1 sentence under 8 seconds of audio.

## Gotchas

- **Patter has no Patter Cloud in 0.6.2** — `Patter(api_key=...)` raises
  `NotImplementedError`. Always use `carrier=...` + `phone_number=...`.
- **Built-in tools are always present**: every agent gets `transfer_call` and
  `end_call` for free. You don't have to register them.
- **Mulaw vs PCM audio**: Twilio carrier = mulaw 8 kHz, Telnyx = PCM 16 kHz.
  Patter transcodes both transparently — you never touch raw audio unless
  you write a pipeline hook.
- **`tunnel=True` is dev-only**. For production, use a static webhook URL
  (your own subdomain or paid ngrok). Cloudflare quick tunnels work but
  occasionally have a ~3 s WSS upgrade race on first call.
- **`first_message` is played before the LLM responds**. If you omit it,
  there's an awkward 1–2 s pause while the LLM warms up.
- **Barge-in works by default** in 0.6.2 (VAD activation 0.8, deactivation
  0.65 — tuned for room noise). If your callers are interrupting at the
  wrong moments, see [`inspect-calls-and-metrics`](../inspect-calls-and-metrics/)
  to read the VAD events from the call log.
- **`prewarm_first_message=False` is the default** in 0.6.2 — it was briefly
  flipped to `True` mid-release and reverted because it conflicted with
  barge-in.

## Common errors

| Symptom | Fix |
|---|---|
| `RuntimeError: Patter Cloud is not implemented` | Don't pass `api_key=`. Use `carrier=Twilio()` + `phone_number="..."`. |
| Agent says nothing on pickup | Missing `first_message=`. Add one. |
| LLM responses are 4 paragraphs long | Add "Keep replies under two sentences" to system prompt. |
| Caller hears garbled audio | Wrong audio rate. Check you're using the right carrier (mulaw 8 kHz Twilio, PCM 16 kHz Telnyx). Patter handles this; user code only breaks it via custom pipeline hooks. |
| Connection drops after 30 s | Either the tunnel died (use static URL) or barge-in hung. Check `MetricsStore` for the last event. |

## Related skills

- [`setup-patter`](../setup-patter/) — install + env vars (run this first if user hasn't).
- [`configure-telephony`](../configure-telephony/) — set up the Twilio/Telnyx webhook.
- [`add-tools-and-handoffs`](../add-tools-and-handoffs/) — custom tools, `transfer_call`, guardrails.
- [`inspect-calls-and-metrics`](../inspect-calls-and-metrics/) — live dashboard, cost per call, transcript.

## References

- Patter overview: <https://docs.getpatter.com/python-sdk/overview> · <https://docs.getpatter.com/typescript-sdk/overview>
- Concepts: <https://docs.getpatter.com/concepts>
- Agent configuration: <https://docs.getpatter.com/python-sdk/agents> · <https://docs.getpatter.com/typescript-sdk/agents>
- Examples: <https://docs.getpatter.com/examples>

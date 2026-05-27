# Realtime mode (`OpenAIRealtime2` / `OpenAIRealtime`)

Speech-to-speech architecture: audio in → OpenAI Realtime API → audio out, over
a single bidirectional WebSocket. Lowest latency (~200 ms turn time), single
provider, simplest setup.

## When to use

- **Default for any new voice agent**.
- Real-time conversation where interruption needs to feel natural.
- Short prompts and few tools.
- One-key setup (`OPENAI_API_KEY` only).

## When NOT to use

- You want a non-OpenAI voice → use ConvAI or Pipeline.
- You want Anthropic Claude / Cerebras / a custom LLM → use Pipeline.
- You're trying to minimize cost per minute → Pipeline with Cerebras + Deepgram
  is significantly cheaper at scale.

## Engine choice

| Engine | Model | Status | When to pick |
|---|---|---|---|
| `OpenAIRealtime2` | `gpt-realtime-2` | **GA in 0.6.2 (recommended)** | New code |
| `OpenAIRealtime` | `gpt-realtime-mini` | Maintained for compat | Existing 0.5.x → 0.6.x upgrades |

## Python

```python
import asyncio
from getpatter import Patter, Twilio, OpenAIRealtime2

async def main():
    phone = Patter(carrier=Twilio(), phone_number="+15550001234")
    agent = phone.agent(
        engine=OpenAIRealtime2(
            api_key="sk-...",            # optional; defaults to OPENAI_API_KEY env
            model="gpt-realtime-2",      # default
            voice="alloy",               # alloy / echo / fable / onyx / nova / shimmer / coral / sage / verse
            reasoning_effort="medium",   # low / medium / high
            input_audio_transcription_model="whisper-1",  # logs the user's words
        ),
        system_prompt="You are a friendly receptionist.",
        first_message="Hi, how can I help?",
    )
    await phone.serve(agent, tunnel=True)

asyncio.run(main())
```

## TypeScript

```typescript
import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });

const agent = phone.agent({
  engine: new OpenAIRealtime2({
    apiKey: process.env.OPENAI_API_KEY,
    model: "gpt-realtime-2",
    voice: "alloy",
    reasoningEffort: "medium",
    inputAudioTranscriptionModel: "whisper-1",
  }),
  systemPrompt: "You are a friendly receptionist.",
  firstMessage: "Hi, how can I help?",
});

await phone.serve({ agent, tunnel: true });
```

## Tuning notes

- **`reasoning_effort`**: `low` is fast and good for chitchat; `medium` is the
  default and balances quality+speed; `high` is for complex multi-step reasoning
  (booking, troubleshooting). Higher values add 100–400 ms latency.
- **`voice`**: try a few — `alloy` is the safest default, `verse` is more
  expressive. Check OpenAI's docs for current options.
- **`input_audio_transcription_model="whisper-1"`** logs what the caller said
  to the dashboard. Without it, only the assistant's text is captured.

## Costs (rough, see <https://openai.com/api/pricing>)

OpenAI Realtime is billed per audio second in + out. Expect $0.06–$0.24 / min
end-to-end depending on input/output mix. Use `MetricsStore` to track exact
per-call cost (`inspect-calls-and-metrics` skill).

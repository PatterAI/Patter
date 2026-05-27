# ConvAI mode (`ElevenLabsConvAI`)

Turn-taking voice agent powered by ElevenLabs ConversationAI. The agent
listens, then speaks — more conventional phone flow with longer pauses
tolerated. Use when you specifically want ElevenLabs voices (50+ voice
clones, multilingual, expressive) or smoother long-pause behaviour.

## When to use

- ElevenLabs voice is required (voice cloning, multilingual).
- Turn-taking is preferred over fluid interruption.
- Caller demographics include long pauses (older callers, complex IVR).

## When NOT to use

- You want sub-300 ms latency → Realtime.
- You want the cheapest tier → Pipeline with Cerebras + ElevenLabs Turbo.

## Python

```python
import asyncio
from getpatter import Patter, Twilio, ElevenLabsConvAI

async def main():
    phone = Patter(carrier=Twilio(), phone_number="+15550001234")
    agent = phone.agent(
        engine=ElevenLabsConvAI(
            api_key="xi-...",          # defaults to ELEVENLABS_API_KEY
            voice="rachel",            # ElevenLabs voice ID or preset name
            model="eleven_turbo_v2_5", # default
        ),
        system_prompt="You are a friendly receptionist.",
        first_message="Hi, how can I help?",
    )
    await phone.serve(agent, tunnel=True)

asyncio.run(main())
```

## TypeScript

```typescript
import { Patter, Twilio, ElevenLabsConvAI } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });

const agent = phone.agent({
  engine: new ElevenLabsConvAI({
    apiKey: process.env.ELEVENLABS_API_KEY,
    voice: "rachel",
    model: "eleven_turbo_v2_5",
  }),
  systemPrompt: "You are a friendly receptionist.",
  firstMessage: "Hi, how can I help?",
});

await phone.serve({ agent, tunnel: true });
```

## Tuning notes

- **Voice cloning**: pass any ElevenLabs voice ID (24-char string) — the agent
  will speak in that voice if your account has access to it.
- **Multi-language**: ElevenLabs Turbo v2.5 handles 32 languages. Set
  `language="es"` on the agent to bias the LLM toward Spanish (Patter
  forwards this to the engine).
- **Long-pause tolerance**: ConvAI doesn't aggressively barge in. Callers who
  pause to think aren't interrupted as often as in Realtime mode.

## Costs

ElevenLabs charges per character of generated TTS + a flat per-conversation
fee on ConvAI plans. See <https://elevenlabs.io/pricing>. Patter's `MetricsStore`
breaks down `cost.tts_usd` and `cost.realtime_usd` separately so you can audit
spend.

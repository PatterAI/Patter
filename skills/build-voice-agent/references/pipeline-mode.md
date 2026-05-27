# Pipeline mode (STT → LLM → TTS)

Modular voice agent that wires together a Speech-to-Text provider, an LLM
provider, and a Text-to-Speech provider. Highest flexibility, every component
swappable. Use when you want a non-OpenAI voice, a custom LLM (Anthropic
Claude, Cerebras for speed, Groq for cost), or to mix-and-match providers
for cost optimization.

## When to use

- You want Anthropic Claude / Cerebras / Groq / Gemini as the brain.
- You want a specific voice (Cartesia Sonic, Rime, LMNT) not available in
  Realtime/ConvAI.
- You want the cheapest stack: Cerebras LLM + Deepgram STT + ElevenLabs Turbo
  TTS can run at < $0.05 / minute.

## When NOT to use

- You want lowest latency — Pipeline is ~600–800 ms turn time vs ~200 ms for
  Realtime. The three round-trips (STT→LLM→TTS) add up.
- You want the simplest setup — needs three keys minimum.

## Python (canonical low-cost stack)

```python
import asyncio
from getpatter import (
    Patter, Twilio,
    DeepgramSTT, CerebrasLLM, ElevenLabsTTS,
)

async def main():
    phone = Patter(carrier=Twilio(), phone_number="+15550001234")
    agent = phone.agent(
        # No engine= → pipeline mode
        stt=DeepgramSTT(model="nova-3"),
        llm=CerebrasLLM(model="gpt-oss-120b"),         # default in 0.5.4+
        tts=ElevenLabsTTS(voice_id="rachel"),
        system_prompt="You are a friendly receptionist.",
        first_message="Hi, how can I help?",
    )
    await phone.serve(agent, tunnel=True)

asyncio.run(main())
```

## TypeScript (canonical low-cost stack)

```typescript
import {
  Patter, Twilio,
  DeepgramSTT, CerebrasLLM, ElevenLabsTTS,
} from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });

const agent = phone.agent({
  // No engine → pipeline mode
  stt: new DeepgramSTT({ model: "nova-3" }),
  llm: new CerebrasLLM({ model: "gpt-oss-120b" }),
  tts: new ElevenLabsTTS({ voiceId: "rachel" }),
  systemPrompt: "You are a friendly receptionist.",
  firstMessage: "Hi, how can I help?",
});

await phone.serve({ agent, tunnel: true });
```

## Provider matrix

### STT (Speech-to-Text)

| Provider | Class | Env | Notes |
|---|---|---|---|
| Deepgram | `DeepgramSTT` | `DEEPGRAM_API_KEY` | Default. Nova-3 is fast + accurate. |
| Whisper (OpenAI hosted) | `WhisperSTT` | `OPENAI_API_KEY` | Slower, batch-style. |
| OpenAI Transcribe | `OpenAITranscribeSTT` | `OPENAI_API_KEY` | Realtime transcribe API. |
| Cartesia | `CartesiaSTT` | `CARTESIA_API_KEY` | Sonic voice ecosystem. |
| Soniox | `SonioxSTT` | `SONIOX_API_KEY` | Multilingual specialist. |
| Speechmatics | `SpeechmaticsSTT` | `SPEECHMATICS_API_KEY` | Enterprise telephony. |
| AssemblyAI | `AssemblyAISTT` | `ASSEMBLYAI_API_KEY` | Universal model. |

### LLM

| Provider | Class | Env | Notes |
|---|---|---|---|
| OpenAI | `OpenAILLM` | `OPENAI_API_KEY` | `gpt-4o-mini` default. |
| Anthropic | `AnthropicLLM` | `ANTHROPIC_API_KEY` | Claude 3.5 Sonnet by default. |
| Cerebras | `CerebrasLLM` | `CEREBRAS_API_KEY` | `gpt-oss-120b` default (0.5.4+). Fastest. |
| Groq | `GroqLLM` | `GROQ_API_KEY` | `mixtral-8x7b` default. |
| Google | `GoogleLLM` | `GOOGLE_API_KEY` | Gemini 2.0 Flash. |

### TTS (Text-to-Speech)

| Provider | Class | Env | Notes |
|---|---|---|---|
| ElevenLabs (WebSocket) | `ElevenLabsTTS` / `ElevenLabsWebSocketTTS` | `ELEVENLABS_API_KEY` | Default in 0.6.1+. Streaming. |
| ElevenLabs (HTTP REST) | `ElevenLabsRestTTS` | `ELEVENLABS_API_KEY` | Slower, simpler. |
| OpenAI | `OpenAITTS` | `OPENAI_API_KEY` | `tts-1` default. |
| Cartesia | `CartesiaTTS` | `CARTESIA_API_KEY` | Sonic — sub-100ms first byte. |
| Rime | `RimeTTS` | `RIME_API_KEY` | English casual. |
| LMNT | `LMNTTTS` | `LMNT_API_KEY` | Fast, low-cost. |
| Inworld | `InworldTTS` | `INWORLD_API_KEY` | Game voice. |

## Tuning notes

- **Default low-latency stack**: Deepgram + Cerebras + ElevenLabs WebSocket.
  Cerebras runs at ~3000 tok/s on WSE-3 hardware, so `gpt-oss-120b` saturates
  the downstream TTS rate just as well as a smaller model — no latency
  penalty for using the bigger brain.
- **Cost-optimized stack**: Deepgram + Cerebras + LMNT — ~$0.03/min observed.
- **Quality-optimized stack**: AssemblyAI + Anthropic Claude 3.5 Sonnet +
  Cartesia Sonic — slower, more expensive, more natural.
- **Watch the per-leg latency** in `CallMetrics.latency_breakdown` to identify
  which leg is the bottleneck (e.g. if TTS first-byte is 400 ms, swap to
  Cartesia).

## Common errors

| Symptom | Fix |
|---|---|
| `RuntimeError: no engine and no STT/LLM/TTS configured` | You forgot all three provider args. Add `stt=...`, `llm=...`, `tts=...`. |
| Agent talks but doesn't hear | STT provider key missing or wrong. Check `DEEPGRAM_API_KEY`. |
| Agent hears but doesn't talk | TTS provider key missing or wrong. Check `ELEVENLABS_API_KEY`. |
| Agent speaks slowly | Switch LLM to Cerebras or Groq (faster than OpenAI for short replies). |
| First TTS byte is slow | Switch TTS to Cartesia or ElevenLabs WebSocket (avoid REST/HTTP). |

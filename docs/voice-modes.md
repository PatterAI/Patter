# Voice Modes

Patter supports three voice modes that determine how audio is processed on the backend. Your `on_message` handler works identically regardless of which mode is active — the difference is entirely in the audio pipeline.

## Overview

| Mode | STT | TTS | Latency | Best For |
|---|---|---|---|---|
| OpenAI Realtime | OpenAI RT | OpenAI RT | Lowest (~300 ms) | Fluid, natural conversations |
| Deepgram + ElevenLabs | Deepgram Nova | ElevenLabs | Low (~500 ms) | Independent STT/TTS control |
| ElevenLabs ConvAI | ElevenLabs | ElevenLabs | Low (~500 ms) | ElevenLabs-managed flow |

## Availability Matrix

| Mode | Cloud | Local Python | Local TypeScript |
|------|-------|-------------|-----------------|
| OpenAI Realtime | ✅ | ✅ | ✅ |
| Pipeline (Deepgram+ElevenLabs) | ✅ | ✅ | ✅ |
| ElevenLabs ConvAI | ✅ | ✅ | ✅ |

Latency figures are round-trip from speech end to audio start, measured over a typical connection. Actual numbers vary with network and provider load.

---

## Mode 1 — OpenAI Realtime

OpenAI's Realtime API handles both speech recognition and voice synthesis in a single streaming session. Audio flows directly to OpenAI over WebSocket without leaving the audio domain for transcription — OpenAI processes the stream natively.

### How it works

```
Phone audio (mulaw/PCM)
    │
    ▼
Backend WebSocket
    │
    ▼
OpenAI Realtime API (WebSocket)
    │  ← streams text tokens
    ▼
Your on_message handler
    │
    ▼
OpenAI Realtime API (synthesizes response)
    │
    ▼
Backend → Phone
```

The backend maintains a WebSocket session to OpenAI's Realtime endpoint. When the caller speaks, OpenAI transcribes incrementally and calls your handler with the completed utterance. The response is synthesized by OpenAI and streamed directly back.

### Configuration

In self-hosted mode, set `stt_config.provider = "openai_realtime"` and supply your OpenAI key. There is no separate TTS config — OpenAI handles both.

```python
# Self-hosted — OpenAI Realtime mode
# Currently configured at the backend level, not via SDK provider helpers
# Set the stt_config on the phone number registration with provider="openai_realtime"
```

### Pros

- Lowest latency — no separate STT/TTS round trips
- Most natural interruption handling
- Single API key to manage (OpenAI)

### Cons

- No voice customization beyond OpenAI's built-in voices
- Tied to OpenAI pricing and availability
- Less control over transcription parameters

### Pricing estimate

OpenAI Realtime API is billed per minute of audio input plus per character of output. Roughly $0.06/min for audio in and $0.024/min for audio out at current OpenAI rates.

---

## Mode 2 — Deepgram + ElevenLabs Pipeline

A two-stage pipeline: Deepgram Nova transcribes incoming audio to text, your handler processes it, and ElevenLabs synthesizes the response. Each provider handles its specialty independently.

### How it works

```
Phone audio (PCM 16kHz)
    │
    ▼
Backend WebSocket
    │
    ▼
Deepgram Nova (streaming STT)
    │  ← transcribed text
    ▼
Your on_message handler
    │
    ▼
ElevenLabs TTS API
    │  ← audio stream
    ▼
Backend → Phone
```

Deepgram streams partial transcripts; the backend accumulates them into complete utterances before calling your handler. The response text goes to ElevenLabs, which streams back MP3/PCM that the backend resamples and forwards to the phone.

### Configuration

```python
await phone.connect(
    on_message=handler,
    provider="telnyx",
    provider_key="KEY4...",
    number="+14155550000",
    stt=Patter.deepgram(api_key="dg_...", language="en"),
    tts=Patter.elevenlabs(api_key="el_...", voice="rachel"),
)
```

```typescript
await phone.connect({
  onMessage: handler,
  provider: "telnyx",
  providerKey: "KEY4...",
  number: "+14155550000",
  stt: Patter.deepgram({ apiKey: "dg_...", language: "en" }),
  tts: Patter.elevenlabs({ apiKey: "el_...", voice: "rachel" }),
});
```

### Pros

- Maximum voice quality control — choose any ElevenLabs voice
- Independent language selection on the STT side
- Swap STT or TTS without changing the other
- Deepgram Nova is fast and accurate on phone audio

### Cons

- Two API keys to manage
- Slightly higher latency than Realtime (two sequential API calls)
- Two separate billing relationships

### Pricing estimate

Deepgram Nova: ~$0.0043/min. ElevenLabs: from $0.003/1000 characters (varies by plan and usage). A 5-minute call with moderate speech is roughly $0.03–0.06 combined.

---

## Mode 3 — ElevenLabs Conversational AI

ElevenLabs ConvAI is a managed conversation service from ElevenLabs that handles the entire interaction loop — speech recognition, turn detection, and synthesis — using ElevenLabs' own infrastructure.

### How it works

```
Phone audio (PCM)
    │
    ▼
Backend WebSocket
    │
    ▼
ElevenLabs ConvAI WebSocket
    │  ← text events
    ▼
Your on_message handler
    │
    ▼
ElevenLabs ConvAI (synthesizes)
    │  ← audio events
    ▼
Backend → Phone
```

The backend proxies the audio stream to ElevenLabs ConvAI and maps ConvAI events to Patter's standard WebSocket protocol. Your handler still receives `IncomingMessage` and returns a string.

### Configuration

In self-hosted mode, configure the phone number's STT config with `provider = "elevenlabs_convai"` and supply your ElevenLabs API key. The TTS config is managed by ConvAI.

### Pros

- Single provider for both STT and TTS
- ElevenLabs handles advanced turn detection
- High-quality ElevenLabs voices

### Cons

- No independent STT provider selection
- ElevenLabs ConvAI pricing applies
- Less transparency into the STT layer

### Pricing estimate

ElevenLabs ConvAI is billed per minute of conversation. Check the ElevenLabs pricing page for current rates.

---

## Choosing a mode

**Use OpenAI Realtime when:**
- Latency is critical (call center, real-time assistance)
- You are already using OpenAI and want one vendor
- You don't need a specific voice from another provider

**Use Deepgram + ElevenLabs when:**
- You need a specific ElevenLabs voice (brand consistency, character)
- You need accurate multilingual transcription (Deepgram supports 30+ languages)
- You want to optimize STT and TTS costs independently

**Use ElevenLabs ConvAI when:**
- You want a simple single-provider setup with ElevenLabs voices
- You prefer ElevenLabs' built-in conversation management
- You are already an ElevenLabs customer

---

## Audio technical details

| | Twilio | Telnyx |
|---|---|---|
| Input format | mulaw 8 kHz | PCM 16 kHz |
| Backend resampling | 8 kHz → 16 kHz | None (native) |
| OpenAI TTS output | 24 kHz PCM → resampled to 16 kHz | Same |

Telnyx's native 16 kHz PCM format means zero transcoding overhead and lower latency than Twilio, making it the preferred provider when latency matters.

---

## Voice mode and your handler

The voice mode is transparent to your application code. No matter which mode is active:

- Your `on_message` handler receives `IncomingMessage(text, call_id, caller)`
- Your handler returns a `str`
- The backend routes that string to the appropriate TTS engine

You can switch voice modes by updating the backend configuration or phone number's STT/TTS config without changing any application code.

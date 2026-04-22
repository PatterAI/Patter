<p align="center">
  <h1 align="center">Patter Python SDK</h1>
  <p align="center">Connect AI agents to phone numbers in four lines of code</p>
</p>

<p align="center">
  <a href="https://pypi.org/project/getpatter/"><img src="https://img.shields.io/pypi/v/getpatter?logo=pypi&logoColor=white&label=pip%20install%20getpatter" alt="PyPI" /></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white" alt="Python 3.11+" />
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> •
  <a href="#features">Features</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#voice-modes">Voice Modes</a> •
  <a href="#api-reference">API Reference</a> •
  <a href="#contributing">Contributing</a>
</p>

---

Patter is the open-source SDK that gives your AI agent a phone number. Point it at any function that returns a string, and Patter handles the rest: telephony, speech-to-text, text-to-speech, and real-time audio streaming. You build the agent — we connect it to the phone.

## Quickstart

```bash
pip install getpatter
```

Set the env vars your carrier and engine need:

```bash
export TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export TWILIO_AUTH_TOKEN=your_auth_token
export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

Four lines of Python:

```python
from getpatter import Patter, Twilio, OpenAIRealtime

phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(engine=OpenAIRealtime(), system_prompt="You are a friendly receptionist for Acme Corp.", first_message="Hello! How can I help?")
await phone.serve(agent, tunnel=True)
```

`tunnel=True` spawns a Cloudflare tunnel and points your Twilio number at it. In production, pass `webhook_url="api.prod.example.com"` to the constructor instead.

## Features

| Feature | Method | Example |
|---|---|---|
| Inbound calls | `phone.serve(agent)` | Answer calls as an AI |
| Outbound calls + AMD | `phone.call(to, machine_detection=True)` | Place calls with voicemail detection |
| Tool calling | `agent(tools=[Tool(...)])` | Agent calls external APIs mid-conversation |
| Custom STT + TTS | `agent(stt=DeepgramSTT(), tts=ElevenLabsTTS())` | Bring your own voice providers |
| Dynamic variables | `agent(variables={...})` | Personalize prompts per caller |
| Custom LLM (any model) | `serve(on_message=handler)` | Claude, Mistral, LLaMA, etc. |
| Call recording | `serve(recording=True)` | Record all calls |
| Call transfer | `transfer_call` (auto-injected) | Transfer to a human |
| Voicemail drop | `call(voicemail_message="...")` | Play message on voicemail |

## Configuration

### Environment variables

Every provider reads its credentials from the environment by default. Pass `api_key="..."` to any constructor to override.

| Variable | Used by |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | `Twilio()` carrier |
| `TELNYX_API_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_PUBLIC_KEY` (optional) | `Telnyx()` carrier |
| `OPENAI_API_KEY` | `OpenAIRealtime`, `getpatter.stt.whisper.STT`, `getpatter.tts.openai.TTS` |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID` | `ElevenLabsConvAI`, `getpatter.tts.elevenlabs.TTS` |
| `DEEPGRAM_API_KEY` | `getpatter.stt.deepgram.STT` |
| `CARTESIA_API_KEY` | `getpatter.stt.cartesia.STT`, `getpatter.tts.cartesia.TTS` |
| `RIME_API_KEY` | `getpatter.tts.rime.TTS` |
| `LMNT_API_KEY` | `getpatter.tts.lmnt.TTS` |
| `SONIOX_API_KEY` | `getpatter.stt.soniox.STT` |
| `SPEECHMATICS_API_KEY` | `getpatter.stt.speechmatics.STT` |
| `ASSEMBLYAI_API_KEY` | `getpatter.stt.assemblyai.STT` |

```bash
cp .env.example .env
# Edit .env with your API keys
```

> **Telnyx:** Telnyx is a fully supported telephony provider alternative to Twilio. Both carriers receive equal support for DTMF, transfer, recording, and metrics.

## Voice Modes

| Mode | Latency | Quality | Best For |
|---|---|---|---|
| **OpenAI Realtime** | Lowest | High | Fluid, low-latency conversations |
| **Pipeline** (STT + LLM + TTS) | Low | High | Independent control over STT and TTS |
| **ElevenLabs ConvAI** | Low | High | ElevenLabs-managed conversation flow |

## API Reference

### `Patter` constructor

```python
Patter(
    carrier: Twilio | Telnyx,
    phone_number: str,
    webhook_url: str = "",         # Public hostname (no scheme). Mutually exclusive with tunnel=...
    tunnel: CloudflareTunnel | Static | Ngrok | None = None,
    pricing: dict | None = None,
)
```

| Parameter | Type | Description |
|---|---|---|
| `carrier` | `Twilio` / `Telnyx` | Carrier instance. Reads env vars by default. |
| `phone_number` | `str` | Your phone number in E.164 format. |
| `webhook_url` | `str` | Public hostname your local server is reachable on. Use instead of `tunnel=`. |
| `tunnel` | instance | `CloudflareTunnel()`, `Static(hostname=...)`, or `Ngrok()`. |

### `phone.agent()`

```python
phone.agent(
    system_prompt: str,
    engine: OpenAIRealtime | ElevenLabsConvAI | None = None,   # default OpenAIRealtime()
    stt: STTProvider | None = None,                            # e.g. DeepgramSTT()
    tts: TTSProvider | None = None,                            # e.g. ElevenLabsTTS()
    voice: str = "alloy",
    model: str = "gpt-4o-mini-realtime-preview",
    language: str = "en",
    first_message: str = "",
    tools: list[Tool] | None = None,
    guardrails: list[Guardrail] | None = None,
    variables: dict | None = None,
    ...,
)
```

Pass `engine=` for end-to-end mode, `stt=` + `tts=` for pipeline mode. Both arguments may take plain adapter instances (e.g. `DeepgramSTT()`) that read their API key from the environment.

### `phone.serve()`

```python
await phone.serve(
    agent: Agent,
    port: int = 8000,
    tunnel: bool = False,          # shortcut for Patter(tunnel=CloudflareTunnel())
    dashboard: bool = True,
    recording: bool = False,
    on_call_start: Callable | None = None,
    on_call_end: Callable | None = None,
    on_transcript: Callable | None = None,
    on_message: Callable | str | None = None,
    voicemail_message: str = "",
    dashboard_token: str = "",
)
```

### `phone.call()`

```python
await phone.call(
    to: str,
    agent: Agent | None = None,            # required in local mode
    from_number: str = "",
    first_message: str = "",
    machine_detection: bool = False,
    voicemail_message: str = "",
    ring_timeout: int | None = None,
)
```

### STT / TTS catalog

Flat re-exports (short form):

```python
from getpatter import (
    Twilio, Telnyx,
    OpenAIRealtime, ElevenLabsConvAI,
    # STT / TTS classes live in namespaced modules — see below.
)
```

Namespaced imports (one module per provider):

```python
from getpatter.stt import deepgram, whisper, cartesia, soniox, speechmatics, assemblyai
from getpatter.tts import elevenlabs, openai as openai_tts, cartesia as cartesia_tts, rime, lmnt

stt = deepgram.STT()                    # reads DEEPGRAM_API_KEY
tts = elevenlabs.TTS(voice="rachel")    # reads ELEVENLABS_API_KEY
```

## Examples

### Inbound calls — default engine

```python
import asyncio
from getpatter import Patter, Twilio, OpenAIRealtime

async def main() -> None:
    phone = Patter(carrier=Twilio(), phone_number="+15550001234")
    agent = phone.agent(
        engine=OpenAIRealtime(),
        system_prompt="You are a helpful customer service agent.",
        first_message="Hello! How can I help?",
    )
    await phone.serve(
        agent,
        tunnel=True,
        on_call_start=lambda data: print(f"Call from {data['caller']}"),
        on_call_end=lambda data: print("Call ended"),
    )

asyncio.run(main())
```

### Custom voice — Deepgram STT + ElevenLabs TTS

```python
from getpatter import Patter, Twilio
from getpatter.stt import deepgram
from getpatter.tts import elevenlabs

phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(
    stt=deepgram.STT(),              # reads DEEPGRAM_API_KEY
    tts=elevenlabs.TTS(voice="rachel"),   # reads ELEVENLABS_API_KEY
    system_prompt="You are a helpful voice assistant.",
)
await phone.serve(agent, tunnel=True)
```

### Tool calling

```python
from getpatter import Patter, Twilio, OpenAIRealtime, Tool, tool

@tool
async def check_availability(date: str) -> dict:
    """Check appointment availability for a given ISO date."""
    return {"available": True}

phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(
    engine=OpenAIRealtime(),
    system_prompt="You are a booking assistant.",
    tools=[check_availability],
)
await phone.serve(agent, tunnel=True)
```

### Outbound calls

```python
from getpatter import Patter, Twilio, OpenAIRealtime

phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(
    engine=OpenAIRealtime(),
    system_prompt="You are making reminder calls.",
    first_message="Hi, this is a reminder from Acme Corp.",
)

await phone.serve(agent, tunnel=True)
await phone.call(to="+14155551234", agent=agent)
```

### Dynamic variables

```python
agent = phone.agent(
    engine=OpenAIRealtime(),
    system_prompt="You are helping {customer_name}, account #{account_id}.",
    first_message="Hi {customer_name}! How can I help you today?",
    variables={"customer_name": "Jane", "account_id": "A-789"},
)
```

## Contributing

Pull requests are welcome.

```bash
cd sdk-py && pip install -e ".[dev]" && pytest tests/ -v
```

Please open an issue before submitting large changes so we can discuss the approach first.

## License

MIT — see [LICENSE](../LICENSE).

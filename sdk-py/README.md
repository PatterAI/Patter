<p align="center">
  <h1 align="center">Patter Python SDK</h1>
  <p align="center">Connect AI agents to phone numbers with 4 lines of code</p>
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

```python
import asyncio
from patter import Patter

async def main():
    phone = Patter(
        twilio_sid="AC...", twilio_token="...",
        openai_key="sk-...",
        phone_number="+1...",
    )

    agent = phone.agent(
        system_prompt="You are a friendly customer service agent for Acme Corp.",
        voice="alloy",
        first_message="Hello! Thanks for calling. How can I help?",
    )

    await phone.serve(agent=agent, port=8000)

asyncio.run(main())
```

## Features

| Feature | Method | Example |
|---|---|---|
| Inbound calls | `phone.serve(agent)` | Answer calls as an AI |
| Outbound calls + AMD | `phone.call(to, machineDetection)` | Place calls with voicemail detection |
| Tool calling (webhooks) | `agent(tools=[...])` | Agent calls external APIs mid-conversation |
| Custom STT + TTS | `agent(provider="pipeline")` | Bring your own voice providers |
| Dynamic variables | `agent(variables={...})` | Personalize prompts per caller |
| Custom LLM (any model) | `serve(onMessage=handler)` | Claude, Mistral, LLaMA, etc. |
| Call recording | `serve(recording=True)` | Record all calls |
| Call transfer | `transfer_call` (auto-injected) | Transfer to a human |
| Voicemail drop | `call(voicemailMessage="...")` | Play message on voicemail |

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes (Realtime mode) | OpenAI API key with Realtime access |
| `TWILIO_ACCOUNT_SID` | Yes | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Yes | Twilio auth token |
| `TWILIO_PHONE_NUMBER` | Yes | Your Twilio phone number (E.164) |
| `DEEPGRAM_API_KEY` | Pipeline mode | Deepgram STT key |
| `ELEVENLABS_API_KEY` | Pipeline mode | ElevenLabs TTS key |
| `ANTHROPIC_API_KEY` | Custom LLM | For bringing your own model |
| `WEBHOOK_URL` | No | Public URL (auto-tunneled via Cloudflare if omitted) |

```bash
cp .env.example .env
# Edit .env with your API keys
```

> **Telnyx:** Telnyx is a fully supported telephony provider alternative to Twilio. Both carriers receive equal support for DTMF, transfer, recording, and metrics.

## Voice Modes

| Mode | Latency | Quality | Best For |
|---|---|---|---|
| **OpenAI Realtime** | Lowest | High | Fluid, low-latency conversations |
| **Deepgram + ElevenLabs** | Low | High | Independent control over STT and TTS |
| **ElevenLabs ConvAI** | Low | High | ElevenLabs-managed conversation flow |

## API Reference

### `Patter` Constructor

```python
Patter(
    twilio_sid: str,
    twilio_token: str,
    openai_key: str,
    phone_number: str,
    webhook_url: str = None,  # Optional; auto-tunneled via Cloudflare if omitted
)
```

| Parameter | Type | Description |
|---|---|---|
| `twilio_sid` | `str` | Twilio account SID |
| `twilio_token` | `str` | Twilio auth token |
| `openai_key` | `str` | OpenAI API key |
| `phone_number` | `str` | Your Twilio phone number (E.164 format) |
| `webhook_url` | `str` | Public URL for Twilio webhooks (optional) |

### `phone.agent()` Method

```python
agent = phone.agent(
    system_prompt: str,
    voice: str = "alloy",
    first_message: str = None,
    variables: dict = None,
    tools: list = None,
)
```

| Parameter | Type | Description |
|---|---|---|
| `system_prompt` | `str` | Prompt with optional `{variable}` placeholders |
| `voice` | `str` | TTS voice name (e.g., "alloy", "echo", "fable") |
| `first_message` | `str` | Opening message (supports `{variable}` placeholders) |
| `variables` | `dict` | Values substituted into prompts |
| `tools` | `list` | Tool definitions: `{name, description, parameters, webhook_url}` |

### `phone.serve()` Method

```python
await phone.serve(
    agent: Agent,
    port: int = 8000,
    dashboard: bool = False,
    recording: bool = False,
    onCallStart: callable = None,
    onCallEnd: callable = None,
    onTranscript: callable = None,
)
```

| Parameter | Type | Description |
|---|---|---|
| `agent` | `Agent` | Agent configuration to use for calls |
| `port` | `int` | Port to listen on (default: 8000) |
| `dashboard` | `bool` | Enable the built-in monitoring dashboard |
| `recording` | `bool` | Enable call recording via the telephony provider |
| `onCallStart` | `callable` | Called when a call connects; receives `data.caller`, `data.call_id` |
| `onCallEnd` | `callable` | Called when a call ends; receives `data.history`, `data.transcript`, `data.duration` |
| `onTranscript` | `callable` | Called on each transcript turn; receives `data.role`, `data.text`, `data.history` |

### `phone.call()` Method

```python
await phone.call(
    to: str,
    first_message: str = None,
    machine_detection: bool = False,
    voicemail_message: str = None,
)
```

| Parameter | Type | Description |
|---|---|---|
| `to` | `str` | Destination phone number (E.164 format) |
| `first_message` | `str` | Opening message for the outbound call |
| `machine_detection` | `bool` | Enable answering machine detection |
| `voicemail_message` | `str` | Message to play when voicemail is detected |

### Static Provider Helpers

```python
Patter.deepgram(api_key: str, language: str = "en") -> STT
Patter.elevenlabs(api_key: str, voice: str = "aria") -> TTS
Patter.openai_tts(api_key: str, voice: str = "alloy") -> TTS
Patter.whisper(api_key: str, language: str = "en") -> STT
```

## Examples

### Inbound Calls (AI answers the phone)

```python
import asyncio
from patter import Patter, IncomingMessage

async def agent(msg: IncomingMessage) -> str:
    if "hours" in msg.text.lower():
        return "We're open Monday through Friday, 9 to 5."
    return "How can I help you today?"

async def main():
    phone = Patter(
        twilio_sid="AC...", twilio_token="...",
        openai_key="sk-...",
        phone_number="+1...",
    )

    await phone.serve(
        agent=phone.agent(
            system_prompt="You are a helpful customer service agent.",
            first_message="Hello! How can I help?",
        ),
        port=8000,
        onCallStart=lambda data: print(f"Call from {data['caller']}"),
        onCallEnd=lambda data: print("Call ended"),
    )

asyncio.run(main())
```

### Outbound Calls (AI calls someone)

```python
import asyncio
from patter import Patter, IncomingMessage

async def agent(msg: IncomingMessage) -> str:
    return "Thanks for picking up. This is a reminder about your appointment tomorrow."

async def main():
    phone = Patter(
        twilio_sid="AC...", twilio_token="...",
        openai_key="sk-...",
        phone_number="+1...",
    )

    agent_config = phone.agent(
        system_prompt="You are making reminder calls.",
        first_message="Hi, this is an automated reminder from Acme Corp.",
    )

    await phone.serve(agent=agent_config, port=8000)
    await phone.call(to="+14155551234", first_message="Hi, just checking in.")

asyncio.run(main())
```

### Tool Calling (Agent calls external APIs)

```python
agent = phone.agent(
    system_prompt="You are a booking assistant. Check availability before confirming.",
    tools=[{
        "name": "check_availability",
        "description": "Check appointment availability for a given date",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date, e.g. 2025-06-15"},
            },
            "required": ["date"],
        },
        "webhook_url": "https://api.example.com/availability",
    }],
)
```

### Custom Voice (Deepgram STT + ElevenLabs TTS)

```python
phone = Patter(
    twilio_sid="AC...", twilio_token="...",
    openai_key="sk-...",
    phone_number="+1...",
)

agent = phone.agent(
    system_prompt="You are a helpful voice assistant.",
    voice="aria",
)

# Use custom STT and TTS in pipeline mode
await phone.serve(
    agent=agent,
    port=8000,
    stt=Patter.deepgram(api_key="dg_...", language="en"),
    tts=Patter.elevenlabs(api_key="el_...", voice="aria"),
)
```

### Call Recording

```python
await phone.serve(
    agent=agent,
    port=8000,
    recording=True,  # Records all inbound and outbound calls
)
```

### Dynamic Variables in Prompts

```python
agent = phone.agent(
    system_prompt="You are helping {customer_name}, account #{account_id}.",
    first_message="Hi {customer_name}! How can I help you today?",
    variables={
        "customer_name": "Jane",
        "account_id": "A-789",
    },
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

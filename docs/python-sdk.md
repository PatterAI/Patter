# Python SDK Reference

The `patter` Python package lets you connect any async function to a live phone number. Install it, point it at a handler, and incoming calls invoke your function with transcribed speech.

## Installation

```bash
pip install patter
```

Requires Python 3.11 or higher.

## Quick example

```python
import asyncio
from patter import Patter, IncomingMessage

async def handler(msg: IncomingMessage) -> str:
    return f"You said: {msg.text}"

async def main():
    phone = Patter(api_key="pt_xxx")
    await phone.connect(on_message=handler)
    await asyncio.Event().wait()

asyncio.run(main())
```

## Patter class

### Constructor

```python
Patter(
    api_key: str,
    backend_url: str = "wss://api.patter.dev",
    rest_url: str = "https://api.patter.dev",
)
```

| Parameter | Type | Description |
|---|---|---|
| `api_key` | `str` | Your API key. Must start with `pt_`. |
| `backend_url` | `str` | WebSocket URL of the Patter backend. Change this when self-hosting. |
| `rest_url` | `str` | HTTP base URL of the Patter backend. Change this when self-hosting. |

**Managed mode (default):**

```python
phone = Patter(api_key="pt_xxx")
```

**Self-hosted mode:**

```python
phone = Patter(
    api_key="pt_xxx",
    backend_url="ws://localhost:8000",
    rest_url="http://localhost:8000",
)
```

---

### connect()

```python
await phone.connect(
    on_message: Callable[[IncomingMessage], Awaitable[str]],
    on_call_start: Callable[[dict], Awaitable[None]] | None = None,
    on_call_end: Callable[[dict], Awaitable[None]] | None = None,
    *,
    # Self-hosted parameters (optional)
    provider: str | None = None,
    provider_key: str | None = None,
    provider_secret: str | None = None,
    number: str | None = None,
    country: str = "US",
    stt: STTConfig | None = None,
    tts: TTSConfig | None = None,
) -> None
```

Opens a WebSocket connection to the backend. The `on_message` handler is called for each caller utterance; its return value is spoken back to the caller.

`connect()` returns once the connection is established (it does not block). To keep the process alive while waiting for calls, use `await asyncio.Event().wait()`.

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `on_message` | `async (IncomingMessage) -> str` | Yes | Called with each transcribed utterance. Return the text to speak. |
| `on_call_start` | `async (dict) -> None` | No | Called when a call starts. Dict includes `caller`, `callee`, `call_id`, `mode`, `custom_params`. |
| `on_call_end` | `async (dict) -> None` | No | Called when a call ends. Dict includes `call_id`, `duration_seconds`. |
| `provider` | `str` | No | Telephony provider: `"twilio"` or `"telnyx"`. Required for self-hosted. |
| `provider_key` | `str` | No | Provider API key (Twilio Account SID or Telnyx API key). |
| `provider_secret` | `str` | No | Provider secret (Twilio auth token). Not needed for Telnyx. |
| `number` | `str` | No | Phone number in E.164 format (e.g. `"+14155550000"`). |
| `country` | `str` | No | ISO 3166-1 alpha-2 country code. Defaults to `"US"`. |
| `stt` | `STTConfig` | No | Speech-to-text config. Use the provider helpers to create one. |
| `tts` | `TTSConfig` | No | Text-to-speech config. Use the provider helpers to create one. |

**Minimal managed mode:**

```python
await phone.connect(on_message=handler)
```

**With lifecycle callbacks:**

```python
async def on_start(data: dict) -> None:
    print(f"Call started from {data['caller']}")
    customer_id = data.get("custom_params", {}).get("customer_id")

async def on_end(data: dict) -> None:
    print(f"Call ended after {data.get('duration_seconds')} seconds")

await phone.connect(
    on_message=handler,
    on_call_start=on_start,
    on_call_end=on_end,
)
```

**Self-hosted with custom providers:**

```python
await phone.connect(
    on_message=handler,
    provider="twilio",
    provider_key="ACxxxxxxxx",
    provider_secret="your_auth_token",
    number="+14155550000",
    country="US",
    stt=Patter.deepgram(api_key="dg_..."),
    tts=Patter.elevenlabs(api_key="el_...", voice="rachel"),
)
```

---

### call()

```python
await phone.call(
    to: str,
    on_message: Callable[[IncomingMessage], Awaitable[str]] | None = None,
    first_message: str = "",
    from_number: str = "",
    machine_detection: bool = False,
    voicemail_message: str = "",
) -> None
```

Places an outbound call. If you have not called `connect()` yet, pass `on_message` and `call()` will connect automatically.

| Parameter | Type | Description |
|---|---|---|
| `to` | `str` | Destination phone number in E.164 format. |
| `on_message` | `async (IncomingMessage) -> str` | Handler for the call. If omitted, must have called `connect()` first. |
| `first_message` | `str` | What the AI says when the callee answers. |
| `from_number` | `str` | Caller ID to use. If empty, uses the first registered number on the account. |
| `machine_detection` | `bool` | Enable Twilio AMD. If a machine is detected, `voicemail_message` is played and the call ends. Defaults to `False`. |
| `voicemail_message` | `str` | Message to play when AMD detects an answering machine. Only used when `machine_detection=True`. |

```python
# Connect first, then call
await phone.connect(on_message=handler)
await phone.call(
    to="+14155551234",
    first_message="Hi! This is an automated reminder.",
)

# Outbound call with machine detection
await phone.call(
    to="+14155551234",
    first_message="Hi, I'm calling about your appointment.",
    machine_detection=True,
    voicemail_message="Hi, please call us back at your earliest convenience.",
)
```

---

### disconnect()

```python
await phone.disconnect() -> None
```

Closes the WebSocket connection and the internal HTTP client. Call this for a clean shutdown.

```python
import signal, asyncio

phone = Patter(api_key="pt_xxx")

async def shutdown():
    await phone.disconnect()

loop = asyncio.get_event_loop()
loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(shutdown()))
```

---

## Provider helpers

Static methods on `Patter` that return `STTConfig` or `TTSConfig` objects. Use these when running self-hosted.

### Patter.deepgram()

```python
Patter.deepgram(api_key: str, language: str = "en") -> STTConfig
```

Deepgram Nova speech-to-text.

| Parameter | Default | Description |
|---|---|---|
| `api_key` | — | Deepgram API key |
| `language` | `"en"` | BCP-47 language tag (e.g. `"es"`, `"fr"`) |

```python
stt = Patter.deepgram(api_key="dg_...", language="es")
```

---

### Patter.whisper()

```python
Patter.whisper(api_key: str, language: str = "en") -> STTConfig
```

OpenAI Whisper speech-to-text.

| Parameter | Default | Description |
|---|---|---|
| `api_key` | — | OpenAI API key |
| `language` | `"en"` | BCP-47 language tag |

```python
stt = Patter.whisper(api_key="sk-...")
```

---

### Patter.elevenlabs()

```python
Patter.elevenlabs(api_key: str, voice: str = "rachel") -> TTSConfig
```

ElevenLabs text-to-speech.

| Parameter | Default | Description |
|---|---|---|
| `api_key` | — | ElevenLabs API key |
| `voice` | `"rachel"` | ElevenLabs voice name or ID |

```python
tts = Patter.elevenlabs(api_key="el_...", voice="aria")
```

---

### Patter.openai_tts()

```python
Patter.openai_tts(api_key: str, voice: str = "alloy") -> TTSConfig
```

OpenAI text-to-speech.

| Parameter | Default | Description |
|---|---|---|
| `api_key` | — | OpenAI API key |
| `voice` | `"alloy"` | OpenAI voice name: `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer` |

```python
tts = Patter.openai_tts(api_key="sk-...", voice="nova")
```

---

## Models

### IncomingMessage

```python
@dataclass(frozen=True)
class IncomingMessage:
    text: str      # Transcribed speech from the caller (or "[DTMF: N]" for keypad presses)
    call_id: str   # Unique identifier for the current call
    caller: str    # Caller's phone number in E.164 format
```

Received by your `on_message` handler for every utterance the caller makes, including DTMF keypad events.

```python
async def handler(msg: IncomingMessage) -> str:
    if msg.text.startswith("[DTMF:"):
        key = msg.text.split(":")[1].strip().rstrip("]")
        return f"You pressed {key}."
    print(f"Call {msg.call_id}: '{msg.text}' from {msg.caller}")
    return "Got it, thank you."
```

---

### STTConfig

```python
@dataclass(frozen=True)
class STTConfig:
    provider: str    # "deepgram" or "whisper"
    api_key: str     # Provider API key
    language: str    # BCP-47 language tag, default "en"
```

Created by `Patter.deepgram()` or `Patter.whisper()`. You rarely need to instantiate this directly.

---

### TTSConfig

```python
@dataclass(frozen=True)
class TTSConfig:
    provider: str    # "elevenlabs" or "openai"
    api_key: str     # Provider API key
    voice: str       # Voice name or ID
```

Created by `Patter.elevenlabs()` or `Patter.openai_tts()`. You rarely need to instantiate this directly.

---

## Exceptions

All exceptions inherit from `PatterError`.

### PatterError

```python
class PatterError(Exception): ...
```

Base class for all Patter exceptions. Catch this to handle any Patter error.

```python
from patter import PatterError

try:
    await phone.connect(on_message=handler)
except PatterError as e:
    print(f"Patter error: {e}")
```

---

### PatterConnectionError

```python
class PatterConnectionError(PatterError): ...
```

Raised when the WebSocket connection to the backend fails, or when you try to send a message on a closed connection.

Common causes:
- Invalid `backend_url`
- Network unreachable
- Calling `call()` before `connect()`

---

### AuthenticationError

```python
class AuthenticationError(PatterError): ...
```

Raised when the API key is rejected by the backend.

---

### ProvisionError

```python
class ProvisionError(PatterError): ...
```

Raised in self-hosted mode when registering a phone number fails (e.g. invalid credentials, provider error).

---

## Modes

### Managed mode

The backend stores your telephony provider credentials and voice configuration. You only need an API key.

```python
phone = Patter(api_key="pt_xxx")
await phone.connect(on_message=handler)
```

### Self-hosted mode

You run the backend yourself and pass provider credentials on each `connect()` call. The SDK registers your number via the REST API before opening the WebSocket.

```python
phone = Patter(
    api_key="pt_xxx",
    backend_url="ws://localhost:8000",
    rest_url="http://localhost:8000",
)

await phone.connect(
    on_message=handler,
    provider="telnyx",
    provider_key="KEY4...",
    number="+14155550000",
    stt=Patter.deepgram(api_key="dg_..."),
    tts=Patter.elevenlabs(api_key="el_..."),
)
```

If the number is already registered (HTTP 409), registration is skipped silently. This means it's safe to call `connect()` every time the process starts.

---

## Full working examples

### Inbound call handler with branching logic

```python
import asyncio
from patter import Patter, IncomingMessage

async def support_agent(msg: IncomingMessage) -> str:
    text = msg.text.lower()
    if "billing" in text:
        return "For billing questions, I can transfer you to our billing team. Would you like that?"
    if "hours" in text or "open" in text:
        return "We're open Monday through Friday, 9 AM to 6 PM Eastern time."
    if "cancel" in text:
        return "I'm sorry to hear you want to cancel. Let me connect you with a specialist."
    return "I'm here to help. Could you tell me more about your question?"

async def main():
    phone = Patter(api_key="pt_xxx")
    await phone.connect(
        on_message=support_agent,
        on_call_start=lambda d: print(f"Call from {d['caller']}"),
        on_call_end=lambda d: print("Call ended"),
    )
    print("Listening for calls...")
    await asyncio.Event().wait()

asyncio.run(main())
```

### Outbound appointment reminder

```python
import asyncio
from patter import Patter, IncomingMessage

APPOINTMENTS = {
    "+14155551234": "dentist appointment on Friday at 2 PM",
}

async def reminder_agent(msg: IncomingMessage) -> str:
    text = msg.text.lower()
    if "yes" in text or "confirm" in text:
        return "Great, your appointment is confirmed. See you then. Goodbye!"
    if "no" in text or "cancel" in text:
        return "Understood. I'll cancel your appointment. Have a good day."
    return "Sorry, I didn't catch that. Can you say yes to confirm or no to cancel?"

async def main():
    phone = Patter(api_key="pt_xxx")
    await phone.connect(on_message=reminder_agent)

    for number, appointment in APPOINTMENTS.items():
        await phone.call(
            to=number,
            first_message=f"Hi! This is a reminder about your {appointment}. "
                          "Press 1 or say yes to confirm, or no to cancel.",
        )

asyncio.run(main())
```

### Local mode — full self-contained server

```python
import asyncio
from patter import Patter

async def main():
    phone = Patter(
        mode="local",
        twilio_sid="AC...",
        twilio_token="...",
        openai_key="sk-...",
        phone_number="+1...",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        system_prompt="You are a friendly customer service agent.",
        voice="alloy",
        first_message="Hello! How can I help you today?",
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)

asyncio.run(main())
```

### Self-hosted with OpenAI for response generation

```python
import asyncio
from openai import AsyncOpenAI
from patter import Patter, IncomingMessage

openai = AsyncOpenAI(api_key="sk-...")

async def ai_handler(msg: IncomingMessage) -> str:
    response = await openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful phone assistant. Keep replies under 2 sentences."},
            {"role": "user", "content": msg.text},
        ],
    )
    return response.choices[0].message.content

async def main():
    phone = Patter(
        api_key="pt_xxx",
        backend_url="ws://localhost:8000",
        rest_url="http://localhost:8000",
    )
    await phone.connect(
        on_message=ai_handler,
        provider="telnyx",
        provider_key="KEY4...",
        number="+14155550000",
        stt=Patter.deepgram(api_key="dg_..."),
        tts=Patter.elevenlabs(api_key="el_...", voice="rachel"),
    )
    await asyncio.Event().wait()

asyncio.run(main())
```

---

## Local Mode

Local Mode runs the full Patter stack (telephony webhook server, STT, TTS) inside your Python process. No cloud backend or Patter API key is required — you bring your own telephony and OpenAI credentials.

### Local Mode Constructor

```python
Patter(
    mode: str = "cloud",               # "local" to enable local mode
    twilio_sid: str | None = None,     # Twilio Account SID
    twilio_token: str | None = None,   # Twilio Auth Token
    openai_key: str | None = None,     # OpenAI API key (for STT/TTS/Realtime)
    phone_number: str | None = None,   # Your Twilio/Telnyx number in E.164
    webhook_url: str | None = None,    # Public URL (e.g. ngrok) for Twilio webhooks
)
```

| Parameter | Type | Description |
|---|---|---|
| `mode` | `str` | Set to `"local"` to run without a cloud backend. |
| `twilio_sid` | `str` | Twilio Account SID (starts with `AC`). |
| `twilio_token` | `str` | Twilio Auth Token. |
| `openai_key` | `str` | OpenAI API key used for speech recognition and synthesis. |
| `phone_number` | `str` | Phone number to receive calls on, in E.164 format. |
| `webhook_url` | `str` | Publicly accessible base URL. Twilio will POST call events here. |

### phone.agent()

```python
phone.agent(
    system_prompt: str,
    voice: str = "alloy",
    first_message: str = "",
    tools: list[dict] | None = None,
    variables: dict[str, str] | None = None,
    stt: STTConfig | None = None,
    tts: TTSConfig | None = None,
) -> Agent
```

Creates an `Agent` configuration that is passed to `phone.serve()`.

| Parameter | Type | Description |
|---|---|---|
| `system_prompt` | `str` | System prompt for the AI. Supports `{variable}` placeholders replaced by `variables`. |
| `voice` | `str` | OpenAI voice name: `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`. |
| `first_message` | `str` | What the AI says immediately when the call connects. |
| `tools` | `list[dict]` | Tool definitions for function calling during the call. |
| `variables` | `dict[str, str]` | Key/value pairs substituted into `{placeholders}` in `system_prompt` before sending to OpenAI. |
| `stt` | `STTConfig` | Override the STT provider for this agent. |
| `tts` | `TTSConfig` | Override the TTS provider for this agent. |

**Dynamic variables example:**

```python
agent = phone.agent(
    system_prompt="Hello {name}, your order #{order_id} is ready for pickup.",
    variables={"name": "Mario", "order_id": "12345"},
    voice="alloy",
    first_message="Hi! I'm calling about your order.",
)
```

### Agent dataclass

```python
@dataclass(frozen=True)
class Agent:
    system_prompt: str
    voice: str = "alloy"
    first_message: str = ""
    tools: tuple[dict, ...] = ()
    variables: dict[str, str] = field(default_factory=dict)
    stt: STTConfig | None = None
    tts: TTSConfig | None = None
```

### phone.serve()

```python
await phone.serve(
    agent: Agent,
    port: int = 8000,
    host: str = "0.0.0.0",
    recording: bool = False,
) -> None
```

Starts a local FastAPI/uvicorn server that handles Twilio webhooks and manages the call lifecycle. Blocks indefinitely (use `Ctrl+C` or a signal handler to stop).

| Parameter | Type | Description |
|---|---|---|
| `agent` | `Agent` | The agent configuration created by `phone.agent()`. |
| `port` | `int` | Local port to bind. Defaults to `8000`. |
| `host` | `str` | Host to bind. Defaults to `0.0.0.0`. |
| `recording` | `bool` | Enable Twilio call recording. Defaults to `False`. Recording status callbacks are sent to `/webhooks/twilio/recording`. |

**With recording enabled:**

```python
await phone.serve(agent=agent, port=8000, recording=True)
```

### Pipeline mode example (local)

Use Deepgram + ElevenLabs instead of OpenAI for STT/TTS:

```python
from patter import Patter

phone = Patter(
    mode="local",
    twilio_sid="AC...",
    twilio_token="...",
    phone_number="+1...",
    webhook_url="xxx.ngrok-free.dev",
)

agent = phone.agent(
    system_prompt="You are a helpful support agent.",
    voice="rachel",  # ElevenLabs voice
    first_message="Hi! Thanks for calling. How can I help?",
)

await phone.serve(
    agent=agent,
    port=8000,
    stt=Patter.deepgram(api_key="dg_..."),
    tts=Patter.elevenlabs(api_key="el_...", voice="rachel"),
)
```

---

## System Tools

Patter automatically injects two system tools into every agent. You do not need to define them; they are always available.

### transfer_call

Transfers the active call to another phone number via the Twilio REST API. The agent invokes this tool autonomously when it decides a transfer is appropriate (e.g. "I'll transfer you to billing now").

```
Tool name: transfer_call
Parameters:
  to (string) — E.164 destination number for the transfer
```

No SDK code is required. The agent triggers the transfer through its natural conversation flow.

### end_call

Hangs up the active call via the Twilio REST API. The agent invokes this tool when the conversation is naturally complete (e.g. after saying "Goodbye, have a great day!").

```
Tool name: end_call
Parameters: none
```

No SDK code is required. The agent triggers the hangup autonomously.

---

## Callback Data Reference

### on_message / on_transcript data

The dict (or `IncomingMessage`) passed to your message handler includes:

| Field | Type | Description |
|---|---|---|
| `text` | `str` | Transcribed speech, or `"[DTMF: N]"` for keypad presses. |
| `call_id` | `str` | Unique call identifier. |
| `caller` | `str` | Caller's E.164 phone number. |
| `history` | `list[dict]` | Full conversation history: `[{"role": "user"/"assistant", "text": "...", "timestamp": 1234}, ...]`. |

**Using conversation history with an LLM:**

```python
async def on_message(data: dict) -> str:
    history = data["history"]
    messages = [{"role": h["role"], "content": h["text"]} for h in history]
    response = await openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a helpful assistant."}] + messages,
    )
    return response.choices[0].message.content
```

### on_call_start data

| Field | Type | Description |
|---|---|---|
| `call_id` | `str` | Unique call identifier. |
| `caller` | `str` | Caller's E.164 phone number. |
| `callee` | `str` | Called E.164 phone number. |
| `mode` | `str` | Voice pipeline mode: `"pipeline"`, `"realtime"`, or `"elevenlabs_convai"`. |
| `custom_params` | `dict` | TwiML `<Parameter>` values set on the inbound call flow. |

**Reading custom parameters:**

```python
async def on_call_start(data: dict) -> None:
    customer_id = data["custom_params"].get("customer_id")
    plan = data["custom_params"].get("plan", "free")
    print(f"Call from customer {customer_id} on plan {plan}")
```

### on_call_end data

| Field | Type | Description |
|---|---|---|
| `call_id` | `str` | Unique call identifier. |
| `duration_seconds` | `int` | Total call duration in seconds. |

---

## Automatic Behaviors

These features require no configuration and are always active in Local Mode.

### Webhook Retry

If a tool webhook call fails, Patter automatically retries up to 3 times with a 500 ms delay between attempts. After 3 failures the error is returned as JSON with `"fallback": true` so the agent can handle it gracefully.

### Mark-based Barge-in

After each audio chunk Patter sends a Twilio mark event. When the caller speaks mid-response, audio playback is cleared precisely at the last mark boundary. This produces more natural interruptions than flag-based approaches.

### DTMF Handling

Keypad presses (DTMF tones) are delivered to `on_message` with `text = "[DTMF: N]"` where N is the pressed digit or symbol. They are also forwarded to OpenAI as plain text (`"The user pressed key N"`) so the agent can react naturally without extra code.

### AI Disclosure

A brief AI disclosure message plays automatically at the start of every call. This is non-optional and cannot be disabled.

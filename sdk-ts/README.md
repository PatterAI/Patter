<p align="center">
  <h1 align="center">Patter</h1>
  <p align="center">Connect AI agents to phone numbers with 4 lines of code</p>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> •
  <a href="#features">Features</a> •
  <a href="#installation">Installation</a> •
  <a href="#documentation">Documentation</a> •
  <a href="#self-hosting">Self-Hosting</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/typescript-5.0%2B-3178c6?logo=typescript&logoColor=white" alt="TypeScript 5+" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License" />
</p>

---

Patter is the open-source SDK that gives your AI agent a phone number. Point it at any function that returns a string, and Patter handles the rest: telephony, speech-to-text, text-to-speech, and real-time audio streaming. You build the agent — we connect it to the phone.

## Quickstart

<details open>
<summary><strong>Python</strong></summary>

```python
import asyncio
from patter import Patter, IncomingMessage

async def on_message(msg: IncomingMessage) -> str:
    # Your agent logic here — return what the AI should say
    return f"You said: {msg.text}"

async def main():
    phone = Patter(api_key="pt_xxx")
    await phone.connect(on_message=on_message)  # starts listening for inbound calls

asyncio.run(main())
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
import { Patter } from "getpatter";

const phone = new Patter({ apiKey: "pt_xxx" });

await phone.connect({
  onMessage: async (msg) => {
    // Your agent logic here — return what the AI should say
    return `You said: ${msg.text}`;
  },
});
```

</details>

## Local Mode (No Cloud Required)

Run Patter entirely in your process — no Patter account, no cloud backend.

<details open>
<summary><strong>Python</strong></summary>

```python
import asyncio
from patter import Patter

async def main():
    phone = Patter(
        mode="local",
        twilio_sid="AC...", twilio_token="...",
        openai_key="sk-...",
        phone_number="+1...",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        system_prompt="You are a friendly customer service agent for Acme Corp.",
        voice="alloy",
        first_message="Hello! Thanks for calling. How can I help?",
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)

asyncio.run(main())
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
import { Patter } from "getpatter";

const phone = new Patter({
  mode: "local",
  twilioSid: "AC...", twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
  webhookUrl: "xxx.ngrok-free.dev",
});

const agent = phone.agent({
  systemPrompt: "You are a friendly customer service agent for Acme Corp.",
  voice: "alloy",
  firstMessage: "Hello! Thanks for calling. How can I help?",
});

console.log("Listening for calls...");
await phone.serve({ agent, port: 8000 });
```

</details>

## Local vs Cloud

| | Cloud Mode | Local Mode |
|---|---|---|
| **Setup** | Patter API key only | Twilio/Telnyx + OpenAI keys |
| **Infrastructure** | Managed by Patter | Runs in your process |
| **Backend** | `wss://api.getpatter.com` | Built-in (FastAPI / Express) |
| **Webhook** | Configured automatically | Requires public URL (e.g. ngrok) |
| **Voice modes** | All three | All three |
| **Best for** | Production, multi-tenant | Development, on-prem, full control |

## Features

### Voice
- Three voice modes: OpenAI Realtime, ElevenLabs ConvAI, Pipeline (any STT + TTS)
- Any STT: Deepgram, OpenAI Whisper
- Any TTS: ElevenLabs, OpenAI TTS
- Natural barge-in with mark-based audio tracking
- DTMF keypad input forwarded to agent as `[DTMF: 1]`

### Agent
- Bring your own agent (any LLM in pipeline mode)
- System prompt with dynamic `{variable}` substitution
- Conversation history tracked per call (`data.history` in all callbacks)
- Tool calling via webhooks with automatic 3x retry
- Built-in tools: `transfer_call`, `end_call` (auto-injected)

### Telephony
- Twilio and Telnyx carriers
- Inbound and outbound calls
- Call transfer to humans (`transfer_call` system tool)
- Call recording (`recording: true` in `serve()`)
- Answering machine detection (`machineDetection: true` for outbound)
- Voicemail drop (`voicemailMessage: "..."` plays on voicemail detection)
- Custom parameters passthrough via TwiML

### Developer Experience
- `pip install patter` / `npm install getpatter`
- 4 lines of code to connect an agent to a phone
- Local mode (embedded, no backend) + Cloud mode
- Python + TypeScript SDKs with full parity
- MCP server for Claude Desktop
- Open-source (MIT)

## Complete Example

```typescript
const phone = new Patter({
  mode: 'local',
  twilioSid: process.env.TWILIO_SID,
  twilioToken: process.env.TWILIO_TOKEN,
  openaiKey: process.env.OPENAI_KEY,
  phoneNumber: '+16592214527',
  webhookUrl: 'your-domain.ngrok-free.dev',
});

const agent = phone.agent({
  systemPrompt: `You are a customer service agent for Acme Corp.
The customer is {customer_name} with order #{order_id}.
Check inventory before answering stock questions.
Transfer to a human if the customer is upset.`,
  voice: 'alloy',
  language: 'en',
  firstMessage: 'Hi {customer_name}! How can I help with order #{order_id}?',
  variables: {
    customer_name: 'John',
    order_id: '12345',
  },
  tools: [{
    name: 'check_inventory',
    description: 'Check product stock',
    parameters: { type: 'object', properties: { product: { type: 'string' } } },
    webhookUrl: 'https://api.acme.com/inventory',
  }],
  // Built-in: transfer_call, end_call (auto-injected)
});

await phone.serve({
  agent,
  port: 8000,
  recording: true,
  onCallStart: async (data) => console.log(`Call from ${data.caller}`),
  onCallEnd: async (data) => console.log(`Transcript: ${data.transcript?.length} turns`),
  onTranscript: async (data) => console.log(`${data.role}: ${data.text}`),
});

// Outbound with machine detection
await phone.call({
  to: '+1234567890',
  machineDetection: true,
  voicemailMessage: 'Hi, please call us back at 555-0123.',
});
```

## How It Works

```
Your Code (on_message handler)
        │
        ▼
   Patter SDK  ──WebSocket──►  Patter Backend  ──────────────────────────────┐
                                      │                                       │
                              ┌───────┴────────┐                              │
                              ▼                ▼                              ▼
                          STT Engine       TTS Engine          Telephony Provider
                       (Deepgram /      (ElevenLabs /          (Twilio / Telnyx)
                        Whisper /        OpenAI TTS)                  │
                       OpenAI RT)              │                       │
                              │               └───────────────────────►│
                              └────────────────────────────────────────►│
                                                                        ▼
                                                                  Phone Call
```

The audio path: **Phone → Telephony → WebSocket → Backend → STT → your handler → TTS → Backend → WebSocket → Phone**

## Installation

```bash
# Python
pip install patter

# TypeScript / Node.js
npm install getpatter
```

## Documentation

### Inbound Calls (AI answers the phone)

<details open>
<summary><strong>Python</strong></summary>

```python
import asyncio
from patter import Patter, IncomingMessage

async def agent(msg: IncomingMessage) -> str:
    if "hours" in msg.text.lower():
        return "We're open Monday through Friday, 9 to 5."
    return "How can I help you today?"

async def main():
    phone = Patter(api_key="pt_xxx")
    await phone.connect(
        on_message=agent,
        on_call_start=lambda data: print(f"Call from {data['caller']}"),
        on_call_end=lambda data: print("Call ended"),
    )
    await asyncio.Event().wait()  # keep the process alive

asyncio.run(main())
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
import { Patter } from "getpatter";

const phone = new Patter({ apiKey: "pt_xxx" });

await phone.connect({
  onMessage: async (msg) => {
    if (msg.text.toLowerCase().includes("hours")) {
      return "We're open Monday through Friday, 9 to 5.";
    }
    return "How can I help you today?";
  },
  onCallStart: (data) => console.log(`Call from ${data.caller}`),
  onCallEnd: () => console.log("Call ended"),
});
```

</details>

---

### Outbound Calls (AI calls someone)

<details open>
<summary><strong>Python</strong></summary>

```python
import asyncio
from patter import Patter, IncomingMessage

async def agent(msg: IncomingMessage) -> str:
    return "Thanks for picking up. This is a reminder about your appointment tomorrow."

async def main():
    phone = Patter(api_key="pt_xxx")
    await phone.connect(on_message=agent)
    await phone.call(
        to="+14155551234",
        first_message="Hi, this is an automated reminder from Acme Corp.",
    )

asyncio.run(main())
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
import { Patter } from "getpatter";

const phone = new Patter({ apiKey: "pt_xxx" });

await phone.connect({
  onMessage: async () =>
    "Thanks for picking up. This is a reminder about your appointment tomorrow.",
});

await phone.call({
  to: "+14155551234",
  firstMessage: "Hi, this is an automated reminder from Acme Corp.",
});
```

</details>

---

### Outbound with Machine Detection + Voicemail Drop

<details open>
<summary><strong>Python</strong></summary>

```python
await phone.call(
    to="+14155551234",
    first_message="Hi, this is an automated reminder from Acme Corp.",
    machine_detection=True,
    voicemail_message="Hi, we tried to reach you. Please call us back at 555-0123.",
)
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
await phone.call({
  to: "+14155551234",
  firstMessage: "Hi, this is an automated reminder from Acme Corp.",
  machineDetection: true,
  voicemailMessage: "Hi, we tried to reach you. Please call us back at 555-0123.",
});
```

</details>

---

### Dynamic Variables in Prompts

Inject call-specific data into system prompts and first messages using `{variable}` placeholders.

<details open>
<summary><strong>Python</strong></summary>

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

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
const agent = phone.agent({
  systemPrompt: "You are helping {customer_name}, account #{account_id}.",
  firstMessage: "Hi {customer_name}! How can I help you today?",
  variables: {
    customer_name: "Jane",
    account_id: "A-789",
  },
});
```

</details>

---

### Tool Calling via Webhooks

Agents can call external APIs mid-conversation. Patter POSTs to your webhook URL and retries up to 3 times on failure.

<details open>
<summary><strong>Python</strong></summary>

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

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
const agent = phone.agent({
  systemPrompt: "You are a booking assistant. Check availability before confirming.",
  tools: [{
    name: "check_availability",
    description: "Check appointment availability for a given date",
    parameters: {
      type: "object",
      properties: {
        date: { type: "string", description: "ISO date, e.g. 2025-06-15" },
      },
      required: ["date"],
    },
    webhookUrl: "https://api.example.com/availability",
  }],
});
```

</details>

---

### Built-in Tools: Transfer & End Call

`transfer_call` and `end_call` are automatically injected into every agent — no configuration needed.

- The agent calls `transfer_call` when it decides to route to a human (e.g. "Let me transfer you now.")
- The agent calls `end_call` when the conversation is complete (e.g. after a confirmed booking.)

---

### Call Recording

<details open>
<summary><strong>Python</strong></summary>

```python
await phone.serve(agent=agent, port=8000, recording=True)
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
await phone.serve({ agent, port: 8000, recording: true });
```

</details>

---

### Conversation History

Every callback receives `data.history` — the full conversation so far as a list of `{role, text}` turns.

<details open>
<summary><strong>Python</strong></summary>

```python
await phone.serve(
    agent=agent,
    port=8000,
    on_transcript=lambda data: print(f"[{data['role']}] {data['text']}"),
    on_call_end=lambda data: print(f"Full history: {data['history']}"),
)
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
await phone.serve({
  agent,
  port: 8000,
  onTranscript: (data) => console.log(`[${data.role}] ${data.text}`),
  onCallEnd: (data) => console.log(`Full history:`, data.history),
});
```

</details>

---

### Custom Voice (choose your providers)

<details open>
<summary><strong>Python</strong></summary>

```python
phone = Patter(api_key="pt_xxx", backend_url="ws://localhost:8000")

await phone.connect(
    on_message=agent,
    provider="twilio",
    provider_key="ACxxxxxxxx",
    provider_secret="your_auth_token",
    number="+14155550000",
    stt=Patter.deepgram(api_key="dg_xxx", language="en"),
    tts=Patter.elevenlabs(api_key="el_xxx", voice="rachel"),
)
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```typescript
const phone = new Patter({ apiKey: "pt_xxx", backendUrl: "ws://localhost:8000" });

await phone.connect({
  onMessage: agent,
  provider: "twilio",
  providerKey: "ACxxxxxxxx",
  providerSecret: "your_auth_token",
  number: "+14155550000",
  stt: Patter.deepgram({ apiKey: "dg_xxx", language: "en" }),
  tts: Patter.elevenlabs({ apiKey: "el_xxx", voice: "rachel" }),
});
```

</details>

## Voice Modes

| Mode | Latency | Quality | Best For |
|---|---|---|---|
| **OpenAI Realtime** | Lowest | High | Fluid, low-latency conversations |
| **Deepgram + ElevenLabs** | Low | High | Independent control over STT and TTS |
| **ElevenLabs ConvAI** | Low | High | ElevenLabs-managed conversation flow |

The voice mode is configured on the backend. Your `on_message` handler works identically regardless of mode.

## MCP Server (Claude Desktop)

Patter ships an MCP server so you can control calls directly from Claude Desktop.

```json
{
  "mcpServers": {
    "patter": {
      "command": "patter-mcp",
      "env": { "PATTER_API_KEY": "pt_xxx" }
    }
  }
}
```

## Self-Hosting

Run the full stack yourself — no Patter Cloud account needed.

```bash
# 1. Clone the repo
git clone https://github.com/your-org/patter
cd patter

# 2. Copy env and fill in your keys
cp .env.example .env

# 3. Start the backend
cd backend
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

Then point your SDK at your local backend:

```python
phone = Patter(
    api_key="pt_xxx",
    backend_url="ws://localhost:8000",
    rest_url="http://localhost:8000",
)
```

**Required environment variables:**

| Variable | Description |
|---|---|
| `PATTER_DATABASE_URL` | PostgreSQL connection string |
| `PATTER_ENCRYPTION_KEY` | Key for encrypting stored provider credentials |
| `PATTER_SECRET_KEY` | JWT / HMAC signing secret |

See `backend/.env.example` for the full list.

## API Reference

### `Patter` (Python & TypeScript)

| Method | Description |
|---|---|
| `Patter(api_key, backend_url?, rest_url?)` | Create client. `backend_url` defaults to `wss://api.getpatter.com`. |
| `connect(on_message, ...)` | Connect and start receiving calls. Blocks until disconnected. |
| `call(to, first_message?, machine_detection?, voicemail_message?, ...)` | Place an outbound call. |
| `disconnect()` | Gracefully close the connection. |

**`serve()` options:**

| Option | Type | Description |
|---|---|---|
| `agent` | `Agent` | Agent configuration to use for calls |
| `port` | `int` | Port to listen on |
| `recording` | `bool` | Enable call recording via the telephony provider |
| `onCallStart` | `callable` | Called when a call connects; receives `data.caller`, `data.call_id` |
| `onCallEnd` | `callable` | Called when a call ends; receives `data.history`, `data.transcript`, `data.duration` |
| `onTranscript` | `callable` | Called on each transcript turn; receives `data.role`, `data.text`, `data.history` |

**`agent()` options:**

| Option | Type | Description |
|---|---|---|
| `system_prompt` | `str` | Prompt with optional `{variable}` placeholders |
| `variables` | `dict` | Values substituted into `system_prompt` and `first_message` |
| `voice` | `str` | TTS voice name |
| `language` | `str` | BCP-47 language code |
| `first_message` | `str` | Opening message (supports `{variable}` placeholders) |
| `tools` | `list` | Tool definitions with `name`, `description`, `parameters`, `webhook_url` |

**`call()` options:**

| Option | Type | Description |
|---|---|---|
| `to` | `str` | Destination phone number |
| `first_message` | `str` | Opening message for the outbound call |
| `machine_detection` | `bool` | Enable answering machine detection |
| `voicemail_message` | `str` | Message to play when voicemail is detected |

**Static provider helpers** (for self-hosted mode):

| Helper | Type | Description |
|---|---|---|
| `Patter.deepgram(api_key, language?)` | STT | Deepgram Nova |
| `Patter.whisper(api_key, language?)` | STT | OpenAI Whisper |
| `Patter.elevenlabs(api_key, voice?)` | TTS | ElevenLabs |
| `Patter.openai_tts(api_key, voice?)` | TTS | OpenAI TTS |

### `IncomingMessage`

| Field | Type | Description |
|---|---|---|
| `text` | `str` | Transcribed speech from the caller (includes `[DTMF: N]` for keypad presses) |
| `call_id` | `str` | Unique identifier for the current call |
| `history` | `list` | Conversation turns so far: `[{role, text}, ...]` |

## Contributing

Pull requests are welcome.

```bash
# Run tests
cd backend && pytest tests/ -v
cd sdk && pytest tests/ -v

# Install dev dependencies
cd backend && pip install -e ".[dev]"
cd sdk && pip install -e ".[dev]"
```

Please open an issue before submitting large changes so we can discuss the approach first.

## License

MIT — see [LICENSE](./LICENSE).

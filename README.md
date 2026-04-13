<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/PatterAI/Patter/main/docs/patter-logo-banner.svg" />
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/PatterAI/Patter/main/docs/patter-logo-banner.svg" />
    <img src="https://raw.githubusercontent.com/PatterAI/Patter/main/docs/patter-logo-banner.svg" alt="Patter" width="400" />
  </picture>
  <br />
  <em>Connect AI agents to phone numbers with 10 lines of code</em>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> •
  <a href="#features">Features</a> •
  <a href="#installation">Installation</a> •
  <a href="#documentation">Documentation</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/typescript-5.0%2B-3178c6?logo=typescript&logoColor=white" alt="TypeScript 5+" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License" />
</p>

---

Patter is an open-source platform that gives your AI agent a voice and a phone number. Point it at any function that returns a string, and Patter handles the rest: telephony, speech-to-text, text-to-speech, and real-time audio streaming.

## Quickstart

<details open>
<summary><strong>Python</strong></summary>

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
  twilioSid: "AC...", twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
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
- 10 lines of code to connect an agent to a phone
- Runs entirely in your process — no external backend needed
- Built-in tunnel via Cloudflare (no ngrok required)
- Python + TypeScript SDKs with full parity
- MCP server for Claude Desktop
- Open-source (MIT)

## Complete Example

```typescript
const phone = new Patter({
  twilioSid: process.env.TWILIO_SID,
  twilioToken: process.env.TWILIO_TOKEN,
  openaiKey: process.env.OPENAI_KEY,
  phoneNumber: '+16592214527',
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
                          Phone Call
                              │
                              ▼
                     Telephony Provider
                     (Twilio / Telnyx)
                              │
                              ▼
                   Patter Embedded Server
                  (FastAPI / Express in your process)
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
               STT Engine  LLM Loop  TTS Engine
              (Deepgram /  (OpenAI / (ElevenLabs /
               Whisper /   Claude /   OpenAI TTS)
              OpenAI RT)   any LLM)
```

The audio path: **Phone → Twilio/Telnyx → Patter (in your process) → STT → LLM → TTS → Phone**

## Installation

```bash
# Python
pip install patter

# TypeScript / Node.js
npm install getpatter
```

## Documentation

### Outbound Calls with Machine Detection + Voicemail Drop

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

## Voice Modes

| Mode | Latency | Quality | Best For |
|---|---|---|---|
| **OpenAI Realtime** | Lowest | High | Fluid, low-latency conversations |
| **Deepgram + ElevenLabs** | Low | High | Independent control over STT and TTS |
| **ElevenLabs ConvAI** | Low | High | ElevenLabs-managed conversation flow |

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

## API Reference

### `Patter` (Python & TypeScript)

| Method | Description |
|---|---|
| `Patter(twilio_sid, twilio_token, openai_key, phone_number, ...)` | Create client with provider credentials. |
| `agent(system_prompt, voice?, first_message?, tools?, ...)` | Create an agent configuration. |
| `serve(agent, port?, ...)` | Start the embedded server and listen for calls. |
| `call(to, first_message?, machine_detection?, voicemail_message?, ...)` | Place an outbound call. |

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

### `IncomingMessage`

| Field | Type | Description |
|---|---|---|
| `text` | `str` | Transcribed speech from the caller (includes `[DTMF: N]` for keypad presses) |
| `call_id` | `str` | Unique identifier for the current call |
| `history` | `list` | Conversation turns so far: `[{role, text}, ...]` |

## Contributing

Pull requests are welcome.

```bash
# Python SDK
cd sdk && pip install -e ".[dev]" && pytest tests/ -v

# TypeScript SDK
cd sdk-ts && npm install && npm test
```

Please open an issue before submitting large changes so we can discuss the approach first.

## License

MIT — see [LICENSE](./LICENSE).

# Getting Started with Patter

Patter connects AI agents to phone numbers with about 10 lines of code. You write a function that takes a transcribed message and returns a string — Patter handles the telephony, speech-to-text, and text-to-speech.

## Prerequisites

- Python 3.11+ or Node.js 18+
- A Patter API key (starts with `pt_`)

## Step 1 — Get an API key

Create an account and get your API key:

```bash
curl -X POST https://api.getpatter.com/api/accounts \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
```

Response:

```json
{
  "id": "uuid",
  "email": "you@example.com",
  "api_key": "pt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

Store this key — you'll pass it to the SDK.

## Step 2 — Install the SDK

**Python:**

```bash
pip install patter
```

**TypeScript / Node.js:**

```bash
npm install getpatter
```

## Step 3 — Write your handler

Your handler receives every transcribed utterance from the caller and returns what the AI should say back. That's all you write.

**Python:**

```python
import asyncio
from patter import Patter, IncomingMessage

async def on_message(msg: IncomingMessage) -> str:
    return f"You said: {msg.text}. How can I help?"

async def main():
    phone = Patter(api_key="pt_your_api_key")
    await phone.connect(on_message=on_message)
    await asyncio.Event().wait()  # keep alive

asyncio.run(main())
```

**TypeScript:**

```typescript
import { Patter } from "getpatter";

const phone = new Patter({ apiKey: "pt_your_api_key" });

await phone.connect({
  onMessage: async (msg) => `You said: ${msg.text}. How can I help?`,
});
```

Run it. Patter connects to the backend, registers your handler, and your phone number starts answering calls.

## Step 4 — Make your first call

**Receive an inbound call:** call your Patter number from any phone. Your handler runs for each utterance.

**Place an outbound call:**

```python
# Python
await phone.call(
    to="+14155551234",
    first_message="Hi! I'm calling from Patter.",
)
```

```typescript
// TypeScript
await phone.call({
  to: "+14155551234",
  firstMessage: "Hi! I'm calling from Patter.",
});
```

## Lifecycle callbacks

You can listen to call start and end events:

```python
# Python
await phone.connect(
    on_message=on_message,
    on_call_start=lambda data: print(f"Call from {data['caller']}"),
    on_call_end=lambda data: print("Call ended"),
)
```

```typescript
// TypeScript
await phone.connect({
  onMessage: on_message,
  onCallStart: async (data) => console.log(`Call from ${data.caller}`),
  onCallEnd: async () => console.log("Call ended"),
});
```

## Phone Features

Local Mode (self-hosted) exposes additional features you can opt into when creating and serving an agent.

### Call Recording

Pass `recording=True` (Python) or `recording: true` (TypeScript) to `serve()` to record the call via the Twilio Recordings API. A status callback is sent to `/webhooks/twilio/recording` when the recording is ready.

```python
# Python
await phone.serve(agent=agent, recording=True)
```

```typescript
// TypeScript
await phone.serve({ agent, recording: true });
```

### Machine Detection and Voicemail Drop

When placing outbound calls you can enable Twilio's Answering Machine Detection (AMD). If a machine is detected, Patter plays a voicemail message and hangs up instead of connecting the agent.

```python
# Python
await phone.call(
    to="+39...",
    machine_detection=True,
    voicemail_message="Hi, this is Patter. Please call us back.",
)
```

```typescript
// TypeScript
await phone.call({
  to: '+39...',
  machineDetection: true,
  voicemailMessage: 'Hi, this is Patter. Please call us back.',
});
```

### Automatic Call Transfer

Every agent automatically has a `transfer_call` system tool available. When the agent decides to transfer (e.g. it says "I'll transfer you now"), Patter redirects the call via the Twilio REST API. No code is required — it just works.

### Automatic Call Hangup

Every agent automatically has an `end_call` system tool available. When the agent says goodbye or the conversation naturally concludes, Patter hangs up the call via the Twilio REST API. No code is required.

### Conversation History

Every callback automatically receives a `history` field containing the full conversation so far. Pass it directly to your LLM to maintain context.

```python
async def on_message(data):
    history = data["history"]  # [{"role": "user", "text": "...", "timestamp": 1234}, ...]
    return await my_llm.chat(history)
```

### Dynamic Variables in Prompts

Personalise the agent's system prompt at call time using named placeholders. Variables are substituted before the prompt is sent to OpenAI.

```python
agent = phone.agent(
    system_prompt="Hello {name}, your order #{order_id} is ready.",
    variables={"name": "Mario", "order_id": "12345"},
)
```

```typescript
const agent = phone.agent({
  systemPrompt: "Hello {name}, your order #{order_id} is ready.",
  variables: { name: "Mario", orderId: "12345" },
});
```

### DTMF (Keypad Input)

Keypad presses are delivered automatically via the `on_transcript` callback (or `on_message` in the SDK). The text field contains a marker like `[DTMF: 1]`. Patter also forwards the event to OpenAI as plain text so the agent can react naturally.

```python
async def on_message(data):
    if data["text"].startswith("[DTMF:"):
        key = data["text"].split(":")[1].strip().rstrip("]")
        return f"You pressed {key}."
```

### Custom Parameters for Inbound Calls

TwiML `<Parameter>` values set on your inbound call flow arrive in the `on_call_start` callback under `custom_params`.

```python
async def on_call_start(data):
    customer_id = data["custom_params"].get("customer_id")
    print(f"Inbound call from customer {customer_id}")
```

---

## Managed vs. self-hosted

The examples above use **Managed mode** — your phone number and voice providers are configured by Patter Cloud. No extra keys needed.

In **Self-hosted mode** you bring your own telephony and voice provider credentials:

```python
# Python — self-hosted
phone = Patter(api_key="pt_xxx", backend_url="ws://localhost:8000")

await phone.connect(
    on_message=on_message,
    provider="telnyx",
    provider_key="KEY4...",
    number="+14155550000",
    stt=Patter.deepgram(api_key="dg_..."),
    tts=Patter.elevenlabs(api_key="el_..."),
)
```

See [self-hosting.md](./self-hosting.md) for the full setup guide.

## Local Mode (Self-Hosted)

Run Patter entirely in your process — no cloud backend needed.

### Prerequisites
- Twilio or Telnyx account
- OpenAI API key
- ngrok for webhooks: `ngrok http 8000`

### Python

```python
from patter import Patter

phone = Patter(
    mode="local",
    twilio_sid="AC...", twilio_token="...",
    openai_key="sk-...",
    phone_number="+1...",
    webhook_url="xxx.ngrok-free.dev",
)

agent = phone.agent(system_prompt="You are a helpful assistant...", voice="alloy")
await phone.serve(agent=agent, port=8000)
```

### TypeScript

```typescript
import { Patter } from 'patter';

const phone = new Patter({
  mode: 'local',
  twilioSid: 'AC...', twilioToken: '...',
  openaiKey: 'sk-...',
  phoneNumber: '+1...',
  webhookUrl: 'xxx.ngrok-free.dev',
});

const agent = phone.agent({ systemPrompt: '...', voice: 'alloy' });
await phone.serve({ agent, port: 8000 });
```

## Next steps

| Doc | Contents |
|---|---|
| [python-sdk.md](./python-sdk.md) | Full Python SDK reference |
| [typescript-sdk.md](./typescript-sdk.md) | Full TypeScript SDK reference |
| [voice-modes.md](./voice-modes.md) | Choosing and configuring voice modes |
| [api-reference.md](./api-reference.md) | REST API and WebSocket protocol |
| [self-hosting.md](./self-hosting.md) | Running your own backend |
| [architecture.md](./architecture.md) | How everything fits together |
| [examples/](./examples/) | Copy-pasteable working examples |

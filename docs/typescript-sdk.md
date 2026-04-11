# TypeScript SDK Reference

The `patter` npm package gives your TypeScript or JavaScript project a fully typed interface to Patter. Install it, pass a handler, and phone calls invoke your function with transcribed speech.

## Installation

```bash
npm install getpatter
# or
yarn add patter
# or
pnpm add patter
```

Requires Node.js 18 or higher.

## Quick example

```typescript
import { Patter } from "getpatter";

const phone = new Patter({ apiKey: "pt_xxx" });

await phone.connect({
  onMessage: async (msg) => `You said: ${msg.text}`,
});
```

## Patter class

### Constructor

```typescript
new Patter(options: PatterOptions)
```

```typescript
interface PatterOptions {
  apiKey: string;       // Your API key — must start with pt_
  backendUrl?: string;  // WebSocket URL — defaults to wss://api.getpatter.com
  restUrl?: string;     // HTTP base URL — defaults to https://api.getpatter.com
}
```

**Managed mode (default):**

```typescript
const phone = new Patter({ apiKey: "pt_xxx" });
```

**Self-hosted mode:**

```typescript
const phone = new Patter({
  apiKey: "pt_xxx",
  backendUrl: "ws://localhost:8000",
  restUrl: "http://localhost:8000",
});
```

---

### connect()

```typescript
await phone.connect(options: ConnectOptions): Promise<void>
```

```typescript
interface ConnectOptions {
  onMessage: MessageHandler;           // Required
  onCallStart?: CallEventHandler;      // Optional
  onCallEnd?: CallEventHandler;        // Optional
  // Self-hosted parameters (optional)
  provider?: string;
  providerKey?: string;
  providerSecret?: string;
  number?: string;
  country?: string;
  stt?: STTConfig;
  tts?: TTSConfig;
}

type MessageHandler = (msg: IncomingMessage) => Promise<string>;
type CallEventHandler = (data: Record<string, unknown>) => Promise<void>;
```

Opens a WebSocket connection to the backend and registers the handler. Resolves once the connection is open (non-blocking — the handler runs in the background on incoming events).

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `onMessage` | `async (IncomingMessage) => string` | Yes | Called with each transcribed utterance. Return the text to speak. |
| `onCallStart` | `async (data) => void` | No | Called when a call starts. Includes `caller`, `callee`, `call_id`, `mode`, `customParams`. |
| `onCallEnd` | `async (data) => void` | No | Called when a call ends. Includes `call_id`, `durationSeconds`. |
| `provider` | `string` | No | `"twilio"` or `"telnyx"`. Required for self-hosted. |
| `providerKey` | `string` | No | Provider API key (Twilio Account SID or Telnyx API key). |
| `providerSecret` | `string` | No | Provider secret (Twilio auth token). Not needed for Telnyx. |
| `number` | `string` | No | Phone number in E.164 format (e.g. `"+14155550000"`). |
| `country` | `string` | No | ISO 3166-1 alpha-2 country code. Defaults to `"US"`. |
| `stt` | `STTConfig` | No | Speech-to-text config. Use the static provider helpers. |
| `tts` | `TTSConfig` | No | Text-to-speech config. Use the static provider helpers. |

**Minimal managed mode:**

```typescript
await phone.connect({
  onMessage: async (msg) => `You said: ${msg.text}`,
});
```

**With lifecycle callbacks:**

```typescript
await phone.connect({
  onMessage: async (msg) => "How can I help?",
  onCallStart: async (data) => {
    const customerId = (data.customParams as Record<string, string>)?.customerId;
    console.log(`Call from ${data.caller}, customer: ${customerId}`);
  },
  onCallEnd: async (data) => console.log(`Call ended after ${data.durationSeconds}s`),
});
```

**Self-hosted with custom providers:**

```typescript
await phone.connect({
  onMessage: handler,
  provider: "twilio",
  providerKey: "ACxxxxxxxx",
  providerSecret: "your_auth_token",
  number: "+14155550000",
  country: "US",
  stt: Patter.deepgram({ apiKey: "dg_...", language: "en" }),
  tts: Patter.elevenlabs({ apiKey: "el_...", voice: "rachel" }),
});
```

---

### call()

```typescript
await phone.call(options: CallOptions): Promise<void>
```

```typescript
interface CallOptions {
  to: string;                      // Destination number in E.164 format
  onMessage?: MessageHandler;      // Handler — required if not yet connected
  firstMessage?: string;           // What the AI says when callee answers
  fromNumber?: string;             // Caller ID; defaults to first registered number
  machineDetection?: boolean;      // Enable Twilio AMD — defaults to false
  voicemailMessage?: string;       // Played when AMD detects a machine
}
```

Places an outbound call. If you have not called `connect()` yet, pass `onMessage` and `call()` will connect automatically.

| Parameter | Type | Description |
|---|---|---|
| `to` | `string` | Destination phone number in E.164 format. |
| `onMessage` | `MessageHandler` | Handler for the call. If omitted, must have called `connect()` first. |
| `firstMessage` | `string` | What the AI says when the callee answers. |
| `fromNumber` | `string` | Caller ID to use. Defaults to the first registered number. |
| `machineDetection` | `boolean` | Enable Twilio AMD. If a machine is detected, `voicemailMessage` is played and the call ends. |
| `voicemailMessage` | `string` | Message to play when AMD detects an answering machine. |

```typescript
// Connect first, then call
await phone.connect({ onMessage: handler });
await phone.call({
  to: "+14155551234",
  firstMessage: "Hi! This is a reminder about your appointment.",
});

// Outbound call with machine detection
await phone.call({
  to: "+39...",
  firstMessage: "Hi, I'm calling about your order.",
  machineDetection: true,
  voicemailMessage: "Hi, please call us back at your earliest convenience.",
});
```

---

### disconnect()

```typescript
await phone.disconnect(): Promise<void>
```

Closes the WebSocket connection. Call this for a clean shutdown.

```typescript
process.on("SIGINT", async () => {
  await phone.disconnect();
  process.exit(0);
});
```

---

## Static provider helpers

Static methods on `Patter` that return `STTConfig` or `TTSConfig` objects. Use these in self-hosted mode.

### Patter.deepgram()

```typescript
static deepgram(opts: { apiKey: string; language?: string }): STTConfig
```

Deepgram Nova speech-to-text.

| Parameter | Default | Description |
|---|---|---|
| `apiKey` | — | Deepgram API key |
| `language` | `"en"` | BCP-47 language tag (e.g. `"es"`, `"fr"`) |

```typescript
const stt = Patter.deepgram({ apiKey: "dg_...", language: "es" });
```

---

### Patter.whisper()

```typescript
static whisper(opts: { apiKey: string; language?: string }): STTConfig
```

OpenAI Whisper speech-to-text.

```typescript
const stt = Patter.whisper({ apiKey: "sk-..." });
```

---

### Patter.elevenlabs()

```typescript
static elevenlabs(opts: { apiKey: string; voice?: string }): TTSConfig
```

ElevenLabs text-to-speech.

| Parameter | Default | Description |
|---|---|---|
| `apiKey` | — | ElevenLabs API key |
| `voice` | `"rachel"` | ElevenLabs voice name or ID |

```typescript
const tts = Patter.elevenlabs({ apiKey: "el_...", voice: "aria" });
```

---

### Patter.openaiTts()

```typescript
static openaiTts(opts: { apiKey: string; voice?: string }): TTSConfig
```

OpenAI text-to-speech.

| Parameter | Default | Description |
|---|---|---|
| `apiKey` | — | OpenAI API key |
| `voice` | `"alloy"` | Voice: `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer` |

```typescript
const tts = Patter.openaiTts({ apiKey: "sk-...", voice: "nova" });
```

---

## Types

### IncomingMessage

```typescript
interface IncomingMessage {
  readonly text: string;     // Transcribed speech, or "[DTMF: N]" for keypad presses
  readonly callId: string;   // Unique identifier for the current call
  readonly caller: string;   // Caller's phone number in E.164 format
  readonly history: ConversationTurn[];  // Full conversation history so far
}

interface ConversationTurn {
  role: "user" | "assistant";
  text: string;
  timestamp: number;  // Unix timestamp (ms)
}
```

Note: the TypeScript SDK uses camelCase (`callId`) while the Python SDK uses snake_case (`call_id`).

```typescript
const handler: MessageHandler = async (msg) => {
  if (msg.text.startsWith("[DTMF:")) {
    const key = msg.text.replace("[DTMF: ", "").replace("]", "");
    return `You pressed ${key}.`;
  }
  console.log(`[${msg.callId}] ${msg.caller}: ${msg.text}`);
  // Pass history to your LLM for context
  const messages = msg.history.map((h) => ({ role: h.role, content: h.text }));
  return await myLlm.chat(messages);
};
```

---

### STTConfig

```typescript
interface STTConfig {
  readonly provider: string;   // "deepgram" or "whisper"
  readonly apiKey: string;     // Provider API key
  readonly language: string;   // BCP-47 language tag
  toDict(): Record<string, string>;
}
```

---

### TTSConfig

```typescript
interface TTSConfig {
  readonly provider: string;   // "elevenlabs" or "openai"
  readonly apiKey: string;     // Provider API key
  readonly voice: string;      // Voice name or ID
  toDict(): Record<string, string>;
}
```

---

### PatterOptions

```typescript
interface PatterOptions {
  apiKey: string;
  backendUrl?: string;
  restUrl?: string;
}
```

---

### PatterLocalOptions

```typescript
interface PatterLocalOptions {
  mode: "local";
  twilioSid: string;      // Twilio Account SID
  twilioToken: string;    // Twilio Auth Token
  openaiKey: string;      // OpenAI API key
  phoneNumber: string;    // Phone number in E.164 format
  webhookUrl: string;     // Public base URL (e.g. ngrok) for Twilio webhooks
}
```

---

### ConnectOptions

```typescript
interface ConnectOptions {
  onMessage: MessageHandler;
  onCallStart?: CallEventHandler;
  onCallEnd?: CallEventHandler;
  provider?: string;
  providerKey?: string;
  providerSecret?: string;
  number?: string;
  country?: string;
  stt?: STTConfig;
  tts?: TTSConfig;
}
```

---

### CallOptions

```typescript
interface CallOptions {
  to: string;
  onMessage?: MessageHandler;
  firstMessage?: string;
  fromNumber?: string;
  machineDetection?: boolean;
  voicemailMessage?: string;
}
```

---

### AgentOptions

```typescript
interface AgentOptions {
  systemPrompt: string;          // System prompt — supports {placeholder} variables
  voice?: string;                // OpenAI voice: alloy, echo, fable, onyx, nova, shimmer
  firstMessage?: string;         // Greeting spoken when a call connects
  tools?: ToolDefinition[];      // Tool/function definitions for tool calling
  variables?: Record<string, string>;  // Values substituted into {placeholders} in systemPrompt
  stt?: STTConfig;               // Override STT for this agent
  tts?: TTSConfig;               // Override TTS for this agent
}
```

---

### ServeOptions

```typescript
interface ServeOptions {
  agent: Agent;        // Agent config from phone.agent()
  port?: number;       // Local port — defaults to 8000
  host?: string;       // Host — defaults to "0.0.0.0"
  recording?: boolean; // Enable Twilio call recording — defaults to false
}
```

---

### Agent

```typescript
interface Agent {
  readonly systemPrompt: string;
  readonly voice: string;
  readonly firstMessage: string;
  readonly tools: readonly ToolDefinition[];
  readonly variables: Record<string, string>;
}
```

---

### MessageHandler / CallEventHandler

```typescript
type MessageHandler = (msg: IncomingMessage) => Promise<string>;
type CallEventHandler = (data: Record<string, unknown>) => Promise<void>;
```

---

## Errors

All errors extend `PatterError`.

### PatterError

```typescript
class PatterError extends Error {
  name: "PatterError";
}
```

Base class. Catch this to handle any Patter error.

```typescript
import { PatterError } from "getpatter";

try {
  await phone.connect({ onMessage: handler });
} catch (err) {
  if (err instanceof PatterError) {
    console.error("Patter error:", err.message);
  }
}
```

---

### PatterConnectionError

```typescript
class PatterConnectionError extends PatterError {
  name: "PatterConnectionError";
}
```

Raised when the WebSocket connection fails, or when you call `call()` before `connect()` without providing `onMessage`.

---

### AuthenticationError

```typescript
class AuthenticationError extends PatterError {
  name: "AuthenticationError";
}
```

Raised when the API key is rejected by the backend.

---

### ProvisionError

```typescript
class ProvisionError extends PatterError {
  name: "ProvisionError";
}
```

Raised in self-hosted mode when phone number registration fails.

---

## Modes

### Managed mode

```typescript
const phone = new Patter({ apiKey: "pt_xxx" });
await phone.connect({ onMessage: handler });
```

### Self-hosted mode

```typescript
const phone = new Patter({
  apiKey: "pt_xxx",
  backendUrl: "ws://localhost:8000",
  restUrl: "http://localhost:8000",
});

await phone.connect({
  onMessage: handler,
  provider: "telnyx",
  providerKey: "KEY4...",
  number: "+14155550000",
  stt: Patter.deepgram({ apiKey: "dg_..." }),
  tts: Patter.elevenlabs({ apiKey: "el_..." }),
});
```

If the number is already registered (HTTP 409), registration is skipped silently.

---

## Full working examples

### Inbound call handler

```typescript
import { Patter, IncomingMessage } from "getpatter";

const phone = new Patter({ apiKey: "pt_xxx" });

async function supportAgent(msg: IncomingMessage): Promise<string> {
  const text = msg.text.toLowerCase();
  if (text.includes("billing")) {
    return "For billing, I can transfer you to our team. Would you like that?";
  }
  if (text.includes("hours") || text.includes("open")) {
    return "We are open Monday through Friday, 9 AM to 6 PM Eastern.";
  }
  return "I am here to help. Could you tell me more about your question?";
}

await phone.connect({
  onMessage: supportAgent,
  onCallStart: async (data) => {
    const customerId = (data.customParams as Record<string, string>)?.customerId;
    console.log(`Call from ${data.caller}, customer ID: ${customerId}`);
  },
  onCallEnd: async () => console.log("Call ended"),
});

console.log("Listening for calls...");
await new Promise(() => {});
```

### Outbound appointment reminder

```typescript
import { Patter, IncomingMessage } from "getpatter";

const phone = new Patter({ apiKey: "pt_xxx" });

const appointments: Record<string, string> = {
  "+14155551234": "dentist appointment on Friday at 2 PM",
};

async function reminderAgent(msg: IncomingMessage): Promise<string> {
  const text = msg.text.toLowerCase();
  if (text.includes("yes") || text.includes("confirm")) {
    return "Great, your appointment is confirmed. See you then. Goodbye!";
  }
  if (text.includes("no") || text.includes("cancel")) {
    return "Understood. I will cancel your appointment. Have a good day.";
  }
  return "Sorry, I did not catch that. Say yes to confirm or no to cancel.";
}

await phone.connect({ onMessage: reminderAgent });

for (const [number, appointment] of Object.entries(appointments)) {
  await phone.call({
    to: number,
    firstMessage: `Hi! This is a reminder about your ${appointment}. Say yes to confirm or no to cancel.`,
    machineDetection: true,
    voicemailMessage: "Hi, we called to confirm your appointment. Please call us back.",
  });
}
```

### Self-hosted with OpenAI and conversation history

```typescript
import OpenAI from "openai";
import { Patter, IncomingMessage } from "getpatter";

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

const phone = new Patter({
  apiKey: "pt_xxx",
  backendUrl: "ws://localhost:8000",
  restUrl: "http://localhost:8000",
});

async function aiHandler(msg: IncomingMessage): Promise<string> {
  const messages = msg.history.map((h) => ({
    role: h.role as "user" | "assistant",
    content: h.text,
  }));
  const response = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [
      { role: "system", content: "You are a helpful phone assistant. Keep replies under 2 sentences." },
      ...messages,
    ],
  });
  return response.choices[0].message.content ?? "I did not understand that.";
}

await phone.connect({
  onMessage: aiHandler,
  provider: "telnyx",
  providerKey: process.env.TELNYX_API_KEY!,
  number: "+14155550000",
  stt: Patter.deepgram({ apiKey: process.env.DEEPGRAM_API_KEY! }),
  tts: Patter.elevenlabs({ apiKey: process.env.ELEVENLABS_API_KEY!, voice: "rachel" }),
});
```

---

## Local Mode

Local Mode runs the full Patter stack inside your Node.js process. No cloud backend or Patter API key is required — you bring your own telephony and OpenAI credentials.

### Local Mode Constructor

```typescript
new Patter({
  mode: "local",
  twilioSid: "AC...",
  twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
  webhookUrl: "xxx.ngrok-free.dev",
})
```

### phone.agent()

```typescript
phone.agent(options: AgentOptions): Agent
```

Creates an `Agent` object passed to `phone.serve()`.

| Parameter | Type | Description |
|---|---|---|
| `systemPrompt` | `string` | System prompt — supports `{placeholder}` variables replaced by `variables`. |
| `voice` | `string` | OpenAI voice name. Defaults to `"alloy"`. |
| `firstMessage` | `string` | Greeting spoken immediately when a call connects. |
| `tools` | `ToolDefinition[]` | Tool/function definitions for tool calling during calls. |
| `variables` | `Record<string, string>` | Key/value pairs substituted into `{placeholders}` in `systemPrompt`. |
| `stt` | `STTConfig` | Override the STT provider for this agent. |
| `tts` | `TTSConfig` | Override the TTS provider for this agent. |

**Dynamic variables example:**

```typescript
const agent = phone.agent({
  systemPrompt: "Hello {name}, your order #{orderId} is ready for pickup.",
  variables: { name: "Mario", orderId: "12345" },
  voice: "alloy",
  firstMessage: "Hi! I'm calling about your order.",
});
```

### phone.serve()

```typescript
await phone.serve(options: ServeOptions): Promise<void>
```

Starts an Express server that handles Twilio webhooks and manages the call lifecycle. Resolves only when the server is stopped.

| Parameter | Type | Description |
|---|---|---|
| `agent` | `Agent` | Agent config from `phone.agent()`. |
| `port` | `number` | Local port to bind. Defaults to `8000`. |
| `host` | `string` | Host to bind. Defaults to `"0.0.0.0"`. |
| `recording` | `boolean` | Enable Twilio call recording. Defaults to `false`. |

**With recording enabled:**

```typescript
await phone.serve({ agent, port: 8000, recording: true });
```

### Pipeline mode example (local)

Use Deepgram + ElevenLabs instead of OpenAI for STT/TTS:

```typescript
import { Patter } from "getpatter";

const phone = new Patter({
  mode: "local",
  twilioSid: "AC...",
  twilioToken: "...",
  phoneNumber: "+1...",
  webhookUrl: "xxx.ngrok-free.dev",
});

const agent = phone.agent({
  systemPrompt: "You are a helpful support agent.",
  voice: "rachel",
  firstMessage: "Hi! Thanks for calling. How can I help?",
});

await phone.serve({
  agent,
  port: 8000,
  stt: Patter.deepgram({ apiKey: "dg_..." }),
  tts: Patter.elevenlabs({ apiKey: "el_...", voice: "rachel" }),
});
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

## Automatic Behaviors

These features require no configuration and are always active in Local Mode.

### Webhook Retry

If a tool webhook call fails, Patter automatically retries up to 3 times with a 500 ms delay between attempts. After 3 failures the error is returned as JSON with `fallback: true` so the agent can handle it gracefully.

### Mark-based Barge-in

After each audio chunk Patter sends a Twilio mark event. When the caller speaks mid-response, audio playback is cleared precisely at the last mark boundary. This produces more natural interruptions than flag-based approaches.

### DTMF Handling

Keypad presses (DTMF tones) are delivered to `onMessage` with `text = "[DTMF: N]"` where N is the pressed digit or symbol. They are also forwarded to OpenAI as plain text (`"The user pressed key N"`) so the agent can react naturally.

```typescript
const handler: MessageHandler = async (msg) => {
  if (msg.text.startsWith("[DTMF:")) {
    const key = msg.text.replace(/\[DTMF:\s*/, "").replace("]", "");
    return `You pressed ${key}.`;
  }
  return "How can I help you?";
};
```

### AI Disclosure

A brief AI disclosure message plays automatically at the start of every call. This is non-optional and cannot be disabled.

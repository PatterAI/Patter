<p align="center">
  <h1 align="center">Patter TypeScript SDK</h1>
  <p align="center">Connect AI agents to phone numbers with 4 lines of code</p>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/getpatter"><img src="https://img.shields.io/npm/v/getpatter?logo=npm&logoColor=white&label=npm%20install%20getpatter" alt="npm" /></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/typescript-5.0%2B-3178c6?logo=typescript&logoColor=white" alt="TypeScript 5+" />
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
npm install getpatter
```

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

await phone.serve({ agent, port: 8000 });
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
| Call recording | `serve(recording=true)` | Record all calls |
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

```typescript
new Patter({
  twilioSid: string;
  twilioToken: string;
  openaiKey: string;
  phoneNumber: string;
  webhookUrl?: string;  // Optional; auto-tunneled via Cloudflare if omitted
})
```

| Parameter | Type | Description |
|---|---|---|
| `twilioSid` | `string` | Twilio account SID |
| `twilioToken` | `string` | Twilio auth token |
| `openaiKey` | `string` | OpenAI API key |
| `phoneNumber` | `string` | Your Twilio phone number (E.164 format) |
| `webhookUrl` | `string` | Public URL for Twilio webhooks (optional) |

### `phone.agent()` Method

```typescript
phone.agent({
  systemPrompt: string;
  voice?: string;
  firstMessage?: string;
  variables?: Record<string, string>;
  tools?: Array<{name, description, parameters, webhookUrl}>;
})
```

| Parameter | Type | Description |
|---|---|---|
| `systemPrompt` | `string` | Prompt with optional `{variable}` placeholders |
| `voice` | `string` | TTS voice name (e.g., "alloy", "echo", "fable") |
| `firstMessage` | `string` | Opening message (supports `{variable}` placeholders) |
| `variables` | `Record<string, string>` | Values substituted into prompts |
| `tools` | `Array` | Tool definitions: `{name, description, parameters, webhookUrl}` |

### `phone.serve()` Method

```typescript
await phone.serve({
  agent: Agent;
  port?: number;
  dashboard?: boolean;
  recording?: boolean;
  onCallStart?: (data: CallData) => Promise<void>;
  onCallEnd?: (data: CallData) => Promise<void>;
  onTranscript?: (data: TranscriptData) => Promise<void>;
})
```

| Parameter | Type | Description |
|---|---|---|
| `agent` | `Agent` | Agent configuration to use for calls |
| `port` | `number` | Port to listen on (default: 8000) |
| `dashboard` | `boolean` | Enable the built-in monitoring dashboard |
| `recording` | `boolean` | Enable call recording via the telephony provider |
| `onCallStart` | `(data) => Promise<void>` | Called when a call connects; receives `data.caller`, `data.callId` |
| `onCallEnd` | `(data) => Promise<void>` | Called when a call ends; receives `data.history`, `data.transcript`, `data.duration` |
| `onTranscript` | `(data) => Promise<void>` | Called on each transcript turn; receives `data.role`, `data.text`, `data.history` |

### `phone.call()` Method

```typescript
await phone.call({
  to: string;
  firstMessage?: string;
  machineDetection?: boolean;
  voicemailMessage?: string;
})
```

| Parameter | Type | Description |
|---|---|---|
| `to` | `string` | Destination phone number (E.164 format) |
| `firstMessage` | `string` | Opening message for the outbound call |
| `machineDetection` | `boolean` | Enable answering machine detection |
| `voicemailMessage` | `string` | Message to play when voicemail is detected |

### Static Provider Helpers

```typescript
Patter.deepgram(options: { apiKey: string; language?: string }) -> STT
Patter.elevenlabs(options: { apiKey: string; voice?: string }) -> TTS
Patter.openaiTts(options: { apiKey: string; voice?: string }) -> TTS
Patter.whisper(options: { apiKey: string; language?: string }) -> STT
```

## Examples

### Inbound Calls (AI answers the phone)

```typescript
import { Patter, IncomingMessage } from "getpatter";

const phone = new Patter({
  twilioSid: "AC...", twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
});

async function agent(msg: IncomingMessage): Promise<string> {
  if (msg.text.toLowerCase().includes("hours")) {
    return "We're open Monday through Friday, 9 to 5.";
  }
  return "How can I help you today?";
}

await phone.serve({
  agent: phone.agent({
    systemPrompt: "You are a helpful customer service agent.",
    firstMessage: "Hello! How can I help?",
  }),
  port: 8000,
  onCallStart: (data) => console.log(`Call from ${data.caller}`),
  onCallEnd: (data) => console.log("Call ended"),
});
```

### Outbound Calls (AI calls someone)

```typescript
import { Patter } from "getpatter";

const phone = new Patter({
  twilioSid: "AC...", twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
});

const agentConfig = phone.agent({
  systemPrompt: "You are making reminder calls.",
  firstMessage: "Hi, this is an automated reminder from Acme Corp.",
});

await phone.serve({ agent: agentConfig, port: 8000 });
await phone.call({
  to: "+14155551234",
  firstMessage: "Hi, just checking in.",
});
```

### Tool Calling (Agent calls external APIs)

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

### Custom Voice (Deepgram STT + ElevenLabs TTS)

```typescript
const phone = new Patter({
  twilioSid: "AC...", twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
});

const agent = phone.agent({
  systemPrompt: "You are a helpful voice assistant.",
  voice: "aria",
});

// Use custom STT and TTS in pipeline mode
await phone.serve({
  agent,
  port: 8000,
  stt: Patter.deepgram({ apiKey: "dg_...", language: "en" }),
  tts: Patter.elevenlabs({ apiKey: "el_...", voice: "aria" }),
});
```

### Call Recording

```typescript
await phone.serve({
  agent,
  port: 8000,
  recording: true,  // Records all inbound and outbound calls
});
```

### Dynamic Variables in Prompts

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

## Contributing

Pull requests are welcome.

```bash
cd sdk-ts && npm install && npm test
```

Please open an issue before submitting large changes so we can discuss the approach first.

## License

MIT — see [LICENSE](../LICENSE).

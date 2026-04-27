<p align="center">
  <h1 align="center">Patter TypeScript SDK</h1>
  <p align="center">Connect AI agents to phone numbers in four lines of code</p>
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

Set the env vars your carrier and engine need:

```bash
export TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export TWILIO_AUTH_TOKEN=your_auth_token
export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

Four lines of TypeScript:

```typescript
import { Patter, Twilio, OpenAIRealtime } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({ engine: new OpenAIRealtime(), systemPrompt: "You are a friendly receptionist for Acme Corp.", firstMessage: "Hello! How can I help?" });
await phone.serve({ agent, tunnel: true });
```

`tunnel: true` spawns a Cloudflare tunnel and points your Twilio number at it. In production, pass `webhookUrl: "api.prod.example.com"` to the constructor instead.

## Features

| Feature | Method | Example |
|---|---|---|
| Inbound calls | `phone.serve({ agent })` | Answer calls as an AI |
| Outbound calls + AMD | `phone.call({ to, machineDetection: true })` | Place calls with voicemail detection |
| Tool calling | `agent({ tools: [tool(...)] })` | Agent calls external APIs mid-conversation |
| Custom STT + TTS | `agent({ stt: new DeepgramSTT(), tts: new ElevenLabsTTS() })` | Bring your own voice providers |
| Dynamic variables | `agent({ variables: {...} })` | Personalize prompts per caller |
| Pluggable LLM | `agent({ llm: new AnthropicLLM() })` | 5 built-in providers: OpenAI, Anthropic, Groq, Cerebras, Google |
| Custom LLM (any model) | `serve({ onMessage })` | Route to anything — local inference, internal gateways, etc. |
| Call recording | `serve({ recording: true })` | Record all calls |
| Call transfer | `transfer_call` (auto-injected) | Transfer to a human |
| Voicemail drop | `call({ voicemailMessage: "..." })` | Play message on voicemail |

## Configuration

### Environment variables

Every provider reads its credentials from the environment by default. Pass `apiKey: "..."` to any constructor to override.

| Variable | Used by |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | `new Twilio()` carrier |
| `TELNYX_API_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_PUBLIC_KEY` (optional) | `new Telnyx()` carrier |
| `OPENAI_API_KEY` | `OpenAIRealtime`, `WhisperSTT`, `OpenAITTS` |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID` | `ElevenLabsConvAI`, `ElevenLabsTTS` |
| `DEEPGRAM_API_KEY` | `DeepgramSTT` |
| `CARTESIA_API_KEY` | `CartesiaSTT`, `CartesiaTTS` |
| `RIME_API_KEY` | `RimeTTS` |
| `LMNT_API_KEY` | `LMNTTTS` |
| `SONIOX_API_KEY` | `SonioxSTT` |
| `ASSEMBLYAI_API_KEY` | `AssemblyAISTT` |
| `ANTHROPIC_API_KEY` | `AnthropicLLM` |
| `GROQ_API_KEY` | `GroqLLM` |
| `CEREBRAS_API_KEY` | `CerebrasLLM` |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | `GoogleLLM` |

```bash
cp .env.example .env
# Edit .env with your API keys
```

> **Telnyx:** Telnyx is a fully supported telephony provider alternative to Twilio. Both carriers receive equal support for DTMF, transfer, and metrics. Recording is Twilio-only.

## Voice Modes

| Mode | Latency | Quality | Best For |
|---|---|---|---|
| **OpenAI Realtime** | Lowest | High | Fluid, low-latency conversations |
| **Pipeline** (STT + LLM + TTS) | Low | High | Independent control over STT and TTS |
| **ElevenLabs ConvAI** | Low | High | ElevenLabs-managed conversation flow |

## API Reference

### `Patter` constructor

```typescript
new Patter({
  carrier: Twilio | Telnyx;
  phoneNumber: string;
  webhookUrl?: string;                              // Public hostname. Mutually exclusive with tunnel.
  tunnel?: CloudflareTunnel | StaticTunnel | boolean;  // `true` is shorthand for new CloudflareTunnel().
})
```

| Parameter | Type | Description |
|---|---|---|
| `carrier` | `Twilio` / `Telnyx` | Carrier instance. Reads env vars by default. |
| `phoneNumber` | `string` | Your phone number in E.164 format. |
| `webhookUrl` | `string` | Public hostname your local server is reachable on. |
| `tunnel` | `CloudflareTunnel \| StaticTunnel \| boolean` | `new CloudflareTunnel()`, `new StaticTunnel({ hostname: ... })`, or `true` (shorthand for `new CloudflareTunnel()`). |

### `phone.agent()`

```typescript
phone.agent({
  systemPrompt: string;
  engine?: OpenAIRealtime | ElevenLabsConvAI;        // default: new OpenAIRealtime()
  stt?: STTProvider;                                 // e.g. new DeepgramSTT()
  tts?: TTSProvider;                                 // e.g. new ElevenLabsTTS()
  voice?: string;
  model?: string;
  language?: string;
  firstMessage?: string;
  tools?: Tool[];
  guardrails?: Guardrail[];
  variables?: Record<string, string>;
})
```

Pass `engine` for end-to-end mode, `stt` + `tts` for pipeline mode. Both arguments may take plain adapter instances (e.g. `new DeepgramSTT()`) that read their API key from the environment.

### `phone.serve()`

```typescript
await phone.serve({
  agent: Agent;
  port?: number;
  tunnel?: boolean;                 // shortcut for Patter({ tunnel: new CloudflareTunnel() })
  dashboard?: boolean;
  recording?: boolean;
  onCallStart?: (data) => Promise<void>;
  onCallEnd?: (data) => Promise<void>;
  onTranscript?: (data) => Promise<void>;
  onMessage?: (data) => Promise<string> | string;
  voicemailMessage?: string;
  dashboardToken?: string;
});
```

### `phone.call()`

```typescript
await phone.call({
  to: string;
  agent?: Agent;
  from?: string;
  firstMessage?: string;
  machineDetection?: boolean;
  voicemailMessage?: string;
  ringTimeout?: number;
});
```

### STT / TTS catalog

```typescript
import {
  // Carriers
  Twilio, Telnyx,
  // Engines
  OpenAIRealtime, ElevenLabsConvAI,
  // STT
  DeepgramSTT, WhisperSTT, CartesiaSTT, SonioxSTT, AssemblyAISTT,
  // TTS
  ElevenLabsTTS, OpenAITTS, CartesiaTTS, RimeTTS, LMNTTTS,
  // LLM
  OpenAILLM, AnthropicLLM, GroqLLM, CerebrasLLM, GoogleLLM,
  // Tunnels
  CloudflareTunnel, StaticTunnel,
  // Primitives
  Tool, Guardrail, tool, guardrail,
} from "getpatter";
```

Every class reads its API key from the environment by default, so `new DeepgramSTT()` / `new ElevenLabsTTS()` work out of the box when the corresponding env var is set.

## Examples

### Inbound calls — default engine

```typescript
import { Patter, Twilio, OpenAIRealtime } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  engine: new OpenAIRealtime(),
  systemPrompt: "You are a helpful customer service agent.",
  firstMessage: "Hello! How can I help?",
});

await phone.serve({
  agent,
  tunnel: true,
  onCallStart: (data) => console.log(`Call from ${data.caller}`),
  onCallEnd: () => console.log("Call ended"),
});
```

### Custom voice — Deepgram STT + ElevenLabs TTS

```typescript
import { Patter, Twilio, DeepgramSTT, ElevenLabsTTS } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  stt: new DeepgramSTT(),                         // reads DEEPGRAM_API_KEY
  tts: new ElevenLabsTTS({ voice: "sarah" }),     // reads ELEVENLABS_API_KEY
  systemPrompt: "You are a helpful voice assistant.",
});
await phone.serve({ agent, tunnel: true });
```

### Pipeline mode — pick STT, LLM, TTS independently

```typescript
import { Patter, Twilio, DeepgramSTT, AnthropicLLM, ElevenLabsTTS } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  stt: new DeepgramSTT(),                         // reads DEEPGRAM_API_KEY
  llm: new AnthropicLLM(),                        // reads ANTHROPIC_API_KEY
  tts: new ElevenLabsTTS({ voiceId: "sarah" }),   // reads ELEVENLABS_API_KEY
  systemPrompt: "You are a helpful voice assistant.",
});
await phone.serve({ agent, tunnel: true });
```

Available LLM providers: `OpenAILLM`, `AnthropicLLM`, `GroqLLM`, `CerebrasLLM`, `GoogleLLM`. Tool calling works across all five. For fully custom logic, drop `llm` and pass an `onMessage` callback to `serve()` instead.

### Tool calling

```typescript
import { Patter, Twilio, OpenAIRealtime, tool } from "getpatter";

const checkAvailability = tool({
  name: "check_availability",
  description: "Check appointment availability for a given ISO date.",
  parameters: {
    type: "object",
    properties: { date: { type: "string" } },
    required: ["date"],
  },
  handler: async ({ date }) => ({ available: true }),
});

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  engine: new OpenAIRealtime(),
  systemPrompt: "You are a booking assistant.",
  tools: [checkAvailability],
});
await phone.serve({ agent, tunnel: true });
```

### Outbound calls

```typescript
import { Patter, Twilio, OpenAIRealtime } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  engine: new OpenAIRealtime(),
  systemPrompt: "You are making reminder calls.",
  firstMessage: "Hi, this is a reminder from Acme Corp.",
});

await phone.serve({ agent, tunnel: true });
await phone.call({ to: "+14155551234", agent });
```

### Dynamic variables

```typescript
const agent = phone.agent({
  engine: new OpenAIRealtime(),
  systemPrompt: "You are helping {customer_name}, account #{account_id}.",
  firstMessage: "Hi {customer_name}! How can I help you today?",
  variables: { customer_name: "Jane", account_id: "A-789" },
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

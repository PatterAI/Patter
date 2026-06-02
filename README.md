<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/PatterAI/Patter/main/docs/github-banner.png" />
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/PatterAI/Patter/main/docs/github-banner.png" />
    <img src="https://raw.githubusercontent.com/PatterAI/Patter/main/docs/github-banner.png" alt="Patter SDK" width="100%" />
  </picture>
</p>

<h1 align="center">Patter SDK</h1>

<p align="center">
  <a href="https://pypi.org/project/getpatter/"><img src="https://img.shields.io/pypi/v/getpatter?logo=pypi&logoColor=white&label=pip%20install%20getpatter" alt="PyPI" /></a>
  <a href="https://www.npmjs.com/package/getpatter"><img src="https://img.shields.io/npm/v/getpatter?logo=npm&logoColor=white&label=npm%20install%20getpatter" alt="npm" /></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/typescript-5.0%2B-3178c6?logo=typescript&logoColor=white" alt="TypeScript 5+" />
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> •
  <a href="#skills-for-coding-agents">Skills</a> •
  <a href="#features">Features</a> •
  <a href="#templates">Templates</a> •
  <a href="#configuration">Configuration</a> •
  <a href="https://docs.getpatter.com">Docs</a>
</p>

---

Patter is the open-source SDK that gives your AI agent a phone number. Point it at any function that returns a string, and Patter handles the rest: telephony, speech-to-text, text-to-speech, and real-time audio streaming. You build the agent — we connect it to the phone.

## Quickstart

Set the env vars your carrier and engine need:

**Twilio**

```bash
export TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export TWILIO_AUTH_TOKEN=your_auth_token
export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

**Telnyx**

```bash
export TELNYX_API_KEY=KEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export TELNYX_CONNECTION_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

**Plivo**

```bash
export PLIVO_AUTH_ID=MAxxxxxxxxxxxxxxxxxx
export PLIVO_AUTH_TOKEN=your_auth_token
export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

<details open>
<summary><strong>Python</strong></summary>

```bash
pip install getpatter
```

```python
from getpatter import Patter, Twilio, OpenAIRealtime

phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(engine=OpenAIRealtime(), system_prompt="You are a friendly receptionist for Acme Corp.", first_message="Hello! How can I help?")
await phone.serve(agent, tunnel=True)
```

Or with **Telnyx**:

```python
from getpatter import Patter, Telnyx, OpenAIRealtime

phone = Patter(carrier=Telnyx(), phone_number="+15550001234")
agent = phone.agent(engine=OpenAIRealtime(), system_prompt="You are a friendly receptionist for Acme Corp.", first_message="Hello! How can I help?")
await phone.serve(agent, tunnel=True)
```

Or with **Plivo**:

```python
from getpatter import Patter, Plivo, OpenAIRealtime

phone = Patter(carrier=Plivo(), phone_number="+15550001234")
agent = phone.agent(engine=OpenAIRealtime(), system_prompt="You are a friendly receptionist for Acme Corp.", first_message="Hello! How can I help?")
await phone.serve(agent, tunnel=True)
```

</details>

<details>
<summary><strong>TypeScript</strong></summary>

```bash
npm install getpatter
```

```typescript
import { Patter, Twilio, OpenAIRealtime } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({ engine: new OpenAIRealtime(), systemPrompt: "You are a friendly receptionist for Acme Corp.", firstMessage: "Hello! How can I help?" });
await phone.serve({ agent, tunnel: true });
```

Or with **Telnyx**:

```typescript
import { Patter, Telnyx, OpenAIRealtime } from "getpatter";

const phone = new Patter({ carrier: new Telnyx(), phoneNumber: "+15550001234" });
const agent = phone.agent({ engine: new OpenAIRealtime(), systemPrompt: "You are a friendly receptionist for Acme Corp.", firstMessage: "Hello! How can I help?" });
await phone.serve({ agent, tunnel: true });
```

Or with **Plivo**:

```typescript
import { Patter, Plivo, OpenAIRealtime } from "getpatter";

const phone = new Patter({ carrier: new Plivo(), phoneNumber: "+15550001234" });
const agent = phone.agent({ engine: new OpenAIRealtime(), systemPrompt: "You are a friendly receptionist for Acme Corp.", firstMessage: "Hello! How can I help?" });
await phone.serve({ agent, tunnel: true });
```

</details>

`tunnel: true` spawns a Cloudflare quick tunnel and points your Twilio number at it — great for dev / acceptance. For production outbound calls (especially on Twilio), replace it with [ngrok](https://ngrok.com) or a static `webhook_url` to avoid WSS upgrade races on first call. See [Tunneling](/docs/dev-tools/tunneling) for details.

Every carrier and provider reads its credentials from environment variables by default; see each SDK's README for the full catalog.

## How Patter compares

Patter is purpose-built for production voice over real telephony. Out of the box you get **Twilio + Telnyx + Plivo parity** (DTMF, transfer, AMD, voicemail drop, recording), **both architectures from one API** — speech-to-speech (Realtime / ConvAI engines) and the sandwich pipeline (STT → LLM → TTS) — and **production-grade barge-in / VAD / IVR primitives** that work the same on every carrier. Observability is vendor-neutral OpenTelemetry tracing, plus a built-in dashboard and tunnel; no extra collector required. The 4-line quickstart above replaces ~50 lines of glue you'd otherwise write against a generic voice-agent toolkit, and the **Python and TypeScript SDKs are identical** — same surface, same hooks, same events — so cross-runtime teams ship the same agent twice without rewriting it.

## Features

| Feature | Method | Template |
|---|---|---|
| Inbound calls | `phone.serve(agent)` | [patter-inbound-agent](https://github.com/PatterAI/patter-inbound-agent) |
| Outbound calls + AMD | `phone.call(to, machineDetection)` | [patter-outbound-calls](https://github.com/PatterAI/patter-outbound-calls) |
| Tool calling (webhooks) | `agent(tools=[...])` | [patter-tool-calling](https://github.com/PatterAI/patter-tool-calling) |
| Custom STT + TTS | `agent(provider="pipeline")` | [patter-custom-voice](https://github.com/PatterAI/patter-custom-voice) |
| Dynamic variables | `agent(variables={...})` | [patter-dynamic-variables](https://github.com/PatterAI/patter-dynamic-variables) |
| Custom LLM (any model) | `serve(onMessage=handler)` | [patter-custom-llm](https://github.com/PatterAI/patter-custom-llm) |
| Dashboard + metrics | `serve(dashboard=True)` | [patter-dashboard](https://github.com/PatterAI/patter-dashboard) |
| Output guardrails | `agent(guardrails=[...])` | [docs](https://docs.getpatter.com) |
| Call recording | `serve(recording=True)` | [docs](https://docs.getpatter.com) |
| Call transfer | `transfer_call` (auto-injected) | [docs](https://docs.getpatter.com) |
| Voicemail drop | `call(voicemailMessage="...")` | [patter-outbound-calls](https://github.com/PatterAI/patter-outbound-calls) |
| Test mode (no phone) | `phone.test(agent)` | [docs](https://docs.getpatter.com) |
| Built-in tunnel | Cloudflare (auto) | [docs](https://docs.getpatter.com) |
| Phone-as-a-tool (LangChain / OpenAI Assistants / Hermes) | `PatterTool(phone, agent).execute(...)` | [docs](https://docs.getpatter.com) |

## How It Works

> **From code to phone call** — Your AI agent connects to real phone calls through the Patter SDK.

<table>
<tr>
<th align="center">AI Agents</th>
<th align="center"></th>
<th align="center">Patter SDK</th>
<th align="center"></th>
<th align="center">Phone Calls</th>
</tr>
<tr>
<td align="center">
  <strong>ChatGPT</strong><br><sub>OpenAI</sub><br><br>
  <strong>Claude</strong><br><sub>Anthropic</sub><br><br>
  <strong>Your AI Agent</strong><br><sub><code>on_message</code></sub>
</td>
<td align="center">→</td>
<td align="center">
  <strong>Patter SDK</strong><br>
  <em>Connect any AI to any phone</em><br><br>
  <code>STT</code> · <code>TTS</code> · <code>WebSocket</code>
</td>
<td align="center">→</td>
<td align="center">
  <strong>Twilio</strong><br><sub>Telephony</sub><br><br>
  <strong>Telnyx</strong><br><sub>Telephony</sub><br><br>
  <strong>Plivo</strong><br><sub>Telephony</sub>
</td>
</tr>
</table>

## Templates

Each template is a self-contained repo — clone, add your `.env`, and run. Both Python and TypeScript included.

| Template | Description | Repo |
|---|---|---|
| **Inbound Agent** | Answer calls as a restaurant booking assistant | [patter-inbound-agent](https://github.com/PatterAI/patter-inbound-agent) |
| **Outbound Calls** | Place calls with AMD and voicemail drop | [patter-outbound-calls](https://github.com/PatterAI/patter-outbound-calls) |
| **Tool Calling** | CRM lookup + ticket creation via webhook tools | [patter-tool-calling](https://github.com/PatterAI/patter-tool-calling) |
| **Custom Voice** | Pipeline mode: Deepgram STT + ElevenLabs TTS | [patter-custom-voice](https://github.com/PatterAI/patter-custom-voice) |
| **Dynamic Variables** | Personalize prompts per caller using CRM data | [patter-dynamic-variables](https://github.com/PatterAI/patter-dynamic-variables) |
| **Custom LLM** | Bring your own model (Claude, Mistral, LLaMA) | [patter-custom-llm](https://github.com/PatterAI/patter-custom-llm) |
| **Dashboard** | Real-time monitoring with cost + latency tracking | [patter-dashboard](https://github.com/PatterAI/patter-dashboard) |
| **Production Setup** | Everything enabled: tools, guardrails, recording, dashboard | [patter-production](https://github.com/PatterAI/patter-production) |

```bash
# Example: clone and run the inbound agent template
git clone https://github.com/PatterAI/patter-inbound-agent
cd patter-inbound-agent
cp .env.example .env    # fill in your keys
cd python && pip install -r requirements.txt && python main.py
```

## Skills for Coding Agents

> Using Claude Code, Claude Desktop, OpenClaw, Hermes, Cursor, Codex, or other AI coding agents?
>
> **[Install Patter skills for voice agents →](https://www.skills.sh/patterai/skills)**

```bash
# Install all five skills (recommended)
npx skills add patterai/skills

# Or install one
npx skills add patterai/skills --skill build-voice-agent
```

The bundle works in **~55 agent harnesses** that consume the
[Anthropic Agent Skills](https://agentskills.io) standard — Claude Code,
Claude Desktop, OpenClaw, Hermes Agent, Cursor, GitHub Copilot, Codex, Cline,
Crush, Goose, Amp, Antigravity, and more. Install once; every agent on your
machine learns the SDK.

| Skill | What it teaches the agent |
|---|---|
| [`setup-patter`](https://github.com/PatterAI/skills/tree/main/setup-patter) | Install Patter, walk the user through provider/carrier consoles, validate each API key, write `.env` |
| [`build-voice-agent`](https://github.com/PatterAI/skills/tree/main/build-voice-agent) | Build a voice agent — Realtime / ConvAI / Pipeline modes, with full Python and TypeScript examples |
| [`configure-telephony`](https://github.com/PatterAI/skills/tree/main/configure-telephony) | Twilio, Telnyx, or Plivo carrier setup — phone numbers, webhooks, tunnels, AMD, voicemail drop |
| [`add-tools-and-handoffs`](https://github.com/PatterAI/skills/tree/main/add-tools-and-handoffs) | Custom tools, `transfer_call`, `end_call`, output guardrails |
| [`inspect-calls-and-metrics`](https://github.com/PatterAI/skills/tree/main/inspect-calls-and-metrics) | Live dashboard, `CallMetrics`, cost tracking, CSV/JSON export |

Skills live in a dedicated repository: **[`PatterAI/skills`](https://github.com/PatterAI/skills)**.

Pin to an SDK version for reproducibility:

```bash
npx skills add patterai/skills#v0.6.2 --skill build-voice-agent
```

Pages on [skills.sh](https://www.skills.sh/patterai/skills) update automatically
via install telemetry — no submission required.

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes (Realtime mode) | OpenAI API key with Realtime access |
| `TWILIO_ACCOUNT_SID` | Yes | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Yes | Twilio auth token |
| `TWILIO_PHONE_NUMBER` | Yes | Your Twilio phone number (E.164) |
| `TELNYX_API_KEY` | Yes (Telnyx) | Telnyx API key |
| `TELNYX_CONNECTION_ID` | Yes (Telnyx) | Telnyx Call Control Application connection ID |
| `TELNYX_PHONE_NUMBER` | Yes (Telnyx) | Your Telnyx phone number (E.164) |
| `PLIVO_AUTH_ID` | Yes (Plivo) | Plivo Auth ID (also the V3 webhook signature account) |
| `PLIVO_AUTH_TOKEN` | Yes (Plivo) | Plivo Auth Token (Basic auth + V3 webhook signature key) |
| `PLIVO_PHONE_NUMBER` | Yes (Plivo) | Your Plivo phone number (E.164) |
| `DEEPGRAM_API_KEY` | Pipeline mode | Deepgram STT key |
| `ELEVENLABS_API_KEY` | Pipeline mode | ElevenLabs TTS key |
| `ANTHROPIC_API_KEY` | Custom LLM | For bringing your own model |
| `WEBHOOK_URL` | No | Public URL (auto-tunneled via Cloudflare if omitted) |

```bash
cp .env.example .env
# Edit .env with your API keys
```

> **Telnyx:** Telnyx is a fully supported telephony provider alternative to Twilio. Both carriers receive equal support for DTMF, transfer, and metrics. Recording parity is supported via Telnyx Call Control; consult the Telnyx portal for configuration details.

### Docker

```bash
docker compose up
```

See [`Dockerfile`](./Dockerfile) and [`docker-compose.yml`](./docker-compose.yml) for the default configuration.

## Voice Modes

| Mode | Quality | Best For |
|---|---|---|
| **OpenAI Realtime** (`engine=OpenAIRealtime()`) | High | Fluid, low-latency conversations |
| **ElevenLabs ConvAI** (`engine=ElevenLabsConvAI()`) | High | ElevenLabs-managed conversation flow |
| **Pipeline** (`stt=DeepgramSTT(), tts=ElevenLabsTTS()`) | High | Independent control over STT / LLM / TTS |

Pipeline mode composes STT + LLM + TTS sequentially and inherits the latency of each provider plus the endpointing window. For the fastest turn UX pick `engine=OpenAIRealtime()`, or pair the pipeline with low-latency providers such as Cerebras/Groq for the LLM and ElevenLabs Turbo v2.5 for TTS.

### Provider Notes

- **ElevenLabs free tier** — the library voice catalog is not reachable via API (`402 Payment Required`). Set `ELEVENLABS_VOICE_ID` to a voice you own (cloned or generated) before `phone.serve()`. The SDK resolves ~45 well-known names (`"rachel"`, `"adam"`, …) to their UUIDs automatically; custom voices must be referenced by ID.
- **Telnyx outbound** — calls will return `403 D38` until your connection has an "Outbound Profile" attached in the Telnyx portal. Inbound and Call Control answer flows work without it.
- **Google Gemini free tier** — `gemini-2.0-flash` has a hard `quota=0` on the free tier. Enable billing on the project before using Gemini as the LLM.
- **Whisper STT** — on mulaw 8 kHz inputs Whisper routinely hallucinates short fillers (`"you"`, `"."`, `"thank you"`). The pipeline drops these by default plus any duplicate / sub-500 ms back-to-back final, so you won't hear overlapping turns. For production prefer `OpenAITranscribeSTT` (`gpt-4o-transcribe`) — same `OPENAI_API_KEY`, ~10× faster, no hallucination floor.
- **Model IDs** — keep these updated per vendor release notes. Examples currently in use: `gpt-4o-mini-realtime-preview`, `claude-haiku-4-5`, `llama-3.3-70b-versatile` (Groq), `gpt-oss-120b` (Cerebras default — pass `model="llama3.1-8b"` for the smaller free-tier alternative).

## API Reference

### `Patter` (Python & TypeScript)

| Method | Description |
|---|---|
| `Patter(carrier=Twilio(), phone_number, ...)` | Create client bound to a carrier and phone number |
| `agent(engine=..., system_prompt, first_message?, tools?, ...)` | Create an agent configuration |
| `serve(agent, port?, tunnel?, dashboard?, ...)` | Start the embedded server and listen for calls |
| `call(to, agent?, machine_detection?, voicemail_message?, ring_timeout?, ...)` | Place an outbound call |

`call()` accepts a `ring_timeout` (seconds) that maps to Twilio's `Timeout` dial parameter, Telnyx's `timeout_secs`, and Plivo's `ring_timeout`. When the carrier reports `no-answer`, `busy`, or `canceled`, the outcome is forwarded to the dashboard via the per-carrier status callback (`/webhooks/twilio/status`, `/webhooks/plivo/status`, or Telnyx's `call.hangup` event) so it appears in the call log even if no media frames were ever exchanged.

**`serve()` options:**

| Option | Type | Description |
|---|---|---|
| `agent` | `Agent` | Agent configuration to use for calls |
| `port` | `int` | Port to listen on (default: 8000) |
| `dashboard` | `bool` | Enable the built-in monitoring dashboard |
| `recording` | `bool` | Enable call recording via the telephony provider |
| `onCallStart` | `callable` | Called when a call connects |
| `onCallEnd` | `callable` | Called when a call ends with transcript and metrics |
| `onTranscript` | `callable` | Called on each transcript turn |

**`agent()` options:**

| Option | Type | Description |
|---|---|---|
| `system_prompt` | `str` | Prompt with optional `{variable}` placeholders |
| `variables` | `dict` | Values substituted into prompts |
| `voice` | `str` | TTS voice name |
| `first_message` | `str` | Opening message (supports `{variable}` placeholders) |
| `tools` | `list` | Tool definitions with `name`, `description`, `parameters`, `webhook_url` |
| `guardrails` | `list` | Output guardrail rules |

## Contributing

Pull requests are welcome.

```bash
# Python SDK
cd libraries/python && pip install -e ".[dev]" && pytest tests/ -v

# TypeScript SDK
cd libraries/typescript && npm install && npm test
```

Please open an issue before submitting large changes so we can discuss the approach first.

## License

MIT — see [LICENSE](./LICENSE).

## Star History

<a href="https://www.star-history.com/?repos=PatterAI%2FPatter&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=PatterAI/Patter&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=PatterAI/Patter&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=PatterAI/Patter&type=date&legend=top-left" />
 </picture>
</a>

## Contributors

Thanks to all our amazing contributors!

<a href="https://github.com/PatterAI/Patter/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=PatterAI/Patter" />
</a>

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
  <a href="#about">About</a> •
  <a href="#how-it-works">How It Works</a> •
  <a href="#documentation">Documentation</a> •
  <a href="#quickstart">Quickstart</a> •
  <a href="#skills-for-coding-agents">Skills</a> •
  <a href="#templates">Templates</a>
</p>

---

## About

**Patter** is the open-source SDK that gives your AI agent a phone number. You build the agent; Patter handles everything between it and the phone network — the agent loop, the language model, speech-to-text, text-to-speech, real-time voice, audio processing, and the telephony carrier.

- **Build** with one API in [Python](https://pypi.org/project/getpatter/) or [TypeScript](https://www.npmjs.com/package/getpatter) — same surface, same hooks, same events, at full parity.
- **Choose** the provider for every layer — LLM, STT, TTS, realtime engine, carrier — and swap any of them with one line.
- **Run** locally with a built-in tunnel and dashboard, or simulate a whole call from your terminal — no phone required.

## How It Works

Patter is the **full voice stack** between your application and the phone network — not just glue between an LLM and a carrier. It runs the agent loop and owns every layer of the call, and **you pick the provider for each one**. Compose them in **Realtime**, **Pipeline**, or **Hybrid** mode.

> **27+ provider integrations across the voice stack · 3 voice modes · 2 SDKs (Python & TypeScript) at parity.**

| Layer | Choose from |
|---|---|
| **LLM** — text generation | OpenAI · Anthropic · Google Gemini · Groq · Cerebras |
| **STT** — speech-to-text | Deepgram · AssemblyAI · Cartesia · Soniox · Speechmatics · Whisper |
| **TTS** — text-to-speech | ElevenLabs · OpenAI · Cartesia · LMNT · Rime · Telnyx |
| **Realtime** — all-in-one voice | OpenAI Realtime · Gemini Live · Ultravox · ElevenLabs ConvAI |
| **Telephony** — phone carriers | Twilio · Telnyx · Plivo |
| **Audio** — VAD & suppression | Silero VAD · Krisp · DeepFilterNet |

On top of the stack: an automatic **LLM fallback chain** (provider failover mid-call), built-in **tools / call transfer / guardrails** that behave identically on every carrier, and a vendor-neutral **OpenTelemetry** trace of each call.

## Documentation

Visit the [docs](https://docs.getpatter.com), or jump straight to a quickstart: [TypeScript](#typescript) · [Python](#python).

## Skills for Coding Agents

> Using Claude Code, Claude Desktop, OpenClaw, Hermes, Cursor, Codex, or another AI coding agent?
>
> **[Install Patter skills for voice agents →](https://www.skills.sh/patterai/skills)**

```bash
npx skills add patterai/skills
```

The bundle works in ~55 agent harnesses that consume the [Anthropic Agent Skills](https://agentskills.io) standard. Install once; every agent on your machine learns the SDK. Skills live in their own repository: **[`PatterAI/skills`](https://github.com/PatterAI/skills)**.

## Quickstart

Provider and carrier credentials are read from environment variables (e.g. `TWILIO_ACCOUNT_SID`, `OPENAI_API_KEY`) — the [docs](https://docs.getpatter.com) list the full catalog. Swap `Twilio` for `Telnyx` or `Plivo` to change carrier.

### TypeScript

```bash
npm install getpatter
```

```typescript
import { Patter, Twilio, OpenAIRealtime } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  engine: new OpenAIRealtime(),
  systemPrompt: "You are a friendly receptionist for Acme Corp.",
  firstMessage: "Hello! How can I help?",
});
await phone.serve({ agent, tunnel: true });
```

### Python

```bash
pip install getpatter
```

```python
from getpatter import Patter, Twilio, OpenAIRealtime

phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(
    engine=OpenAIRealtime(),
    system_prompt="You are a friendly receptionist for Acme Corp.",
    first_message="Hello! How can I help?",
)
await phone.serve(agent, tunnel=True)
```

`tunnel: true` spawns a Cloudflare quick tunnel and points your number at it — ideal for local dev. For production, use a static `webhook_url` (or [ngrok](https://ngrok.com)); see [Tunneling](https://docs.getpatter.com).

## Templates

Each template is a self-contained repo — clone, add your `.env`, and run. Python and TypeScript both included.

| Template | Description | Repo |
|---|---|---|
| **Inbound Agent** | Answer calls as a restaurant booking assistant | [patter-inbound-agent](https://github.com/PatterAI/patter-inbound-agent) |
| **Outbound Calls** | Place calls with AMD and voicemail drop | [patter-outbound-calls](https://github.com/PatterAI/patter-outbound-calls) |
| **Tool Calling** | CRM lookup + ticket creation via webhook tools | [patter-tool-calling](https://github.com/PatterAI/patter-tool-calling) |
| **Custom Voice** | Pipeline mode: Deepgram STT + ElevenLabs TTS | [patter-custom-voice](https://github.com/PatterAI/patter-custom-voice) |
| **Dynamic Variables** | Personalize prompts per caller using CRM data | [patter-dynamic-variables](https://github.com/PatterAI/patter-dynamic-variables) |
| **Custom LLM** | Bring your own model | [patter-custom-llm](https://github.com/PatterAI/patter-custom-llm) |
| **Dashboard** | Real-time monitoring with cost + latency tracking | [patter-dashboard](https://github.com/PatterAI/patter-dashboard) |
| **Production Setup** | Everything enabled: tools, guardrails, recording, dashboard | [patter-production](https://github.com/PatterAI/patter-production) |

```bash
git clone https://github.com/PatterAI/patter-inbound-agent
cd patter-inbound-agent
cp .env.example .env    # fill in your keys
cd python && pip install -r requirements.txt && python main.py
```

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

## License

MIT — see [LICENSE](./LICENSE).

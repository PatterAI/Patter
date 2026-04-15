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
  <a href="#features">Features</a> •
  <a href="#templates">Templates</a> •
  <a href="#configuration">Configuration</a> •
  <a href="https://docs.getpatter.com">Docs</a>
</p>

---

Patter is an open-source platform that gives your AI agent a voice and a phone number. Point it at any function that returns a string, and Patter handles the rest: telephony, speech-to-text, text-to-speech, and real-time audio streaming.

## Quickstart

<details open>
<summary><strong>Python</strong></summary>

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

</details>

<details>
<summary><strong>TypeScript</strong></summary>

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

</details>

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
| MCP server | `patter-mcp` CLI | [docs](https://docs.getpatter.com) |

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
  <strong>Telnyx</strong><br><sub>Telephony</sub>
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

### Docker

```bash
docker compose up
```

See [`Dockerfile`](./Dockerfile) and [`docker-compose.yml`](./docker-compose.yml) for the default configuration.

## Voice Modes

| Mode | Latency | Quality | Best For |
|---|---|---|---|
| **OpenAI Realtime** | Lowest | High | Fluid, low-latency conversations |
| **Deepgram + ElevenLabs** | Low | High | Independent control over STT and TTS |
| **ElevenLabs ConvAI** | Low | High | ElevenLabs-managed conversation flow |

## API Reference

### `Patter` (Python & TypeScript)

| Method | Description |
|---|---|
| `Patter(twilio_sid, twilio_token, openai_key, phone_number, ...)` | Create client with provider credentials |
| `agent(system_prompt, voice?, first_message?, tools?, ...)` | Create an agent configuration |
| `serve(agent, port?, dashboard?, ...)` | Start the embedded server and listen for calls |
| `call(to, agent?, machine_detection?, voicemail_message?, ...)` | Place an outbound call |

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
cd sdk && pip install -e ".[dev]" && pytest tests/ -v

# TypeScript SDK
cd sdk-ts && npm install && npm test
```

Please open an issue before submitting large changes so we can discuss the approach first.

## License

MIT — see [LICENSE](./LICENSE).

# Patter Templates

This directory is a **stub-redirect index**. The files in `developer/`, `enterprise/`, and `startup/` are not runnable on their own — each is a short pointer to a standalone template repository on GitHub. Open any stub to see the redirect URL, or use the table below to jump straight to the repo. Every template ships with both Python and TypeScript implementations.

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

## Quick Start

```bash
git clone https://github.com/PatterAI/patter-inbound-agent
cd patter-inbound-agent
cp .env.example .env    # fill in your API keys

# Python
cd python && pip install -r requirements.txt && python main.py

# TypeScript
cd typescript && npm install && npx tsx main.ts
```

See the [main README](https://github.com/PatterAI/Patter) for full documentation.

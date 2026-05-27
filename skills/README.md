# Patter Agent Skills

A bundle of [Anthropic Agent Skills](https://agentskills.io) that teach an AI agent
how to use the [Patter](https://github.com/PatterAI/Patter) voice/telephony SDK —
covering setup, building voice agents (Realtime / ConvAI / Pipeline modes), Twilio
and Telnyx telephony, custom tools and handoffs, and call inspection.

Skills work in Claude Code, Claude Desktop, OpenClaw, Hermes, Cursor, Codex, and
~50 other agent harnesses that consume the `npx skills add` CLI.

## Install

```bash
# Install one skill
npx skills add patterai/patter --skill build-voice-agent

# Install all Patter skills
npx skills add patterai/patter

# Pin to a specific SDK version (recommended for production)
npx skills add patterai/patter#v0.6.2 --skill build-voice-agent
```

Skills land in `~/.agents/skills/<skill-name>/` (global) or `./.agents/skills/<skill-name>/`
(project-local), with symlinks into the per-agent skill directories.

## Skills in this bundle

| Skill | Purpose |
|---|---|
| [`setup-patter`](./setup-patter/) | Install Patter, configure provider API keys, and verify the environment for either Python or TypeScript. |
| [`build-voice-agent`](./build-voice-agent/) | Build a voice agent — pick between OpenAI Realtime, ElevenLabs ConvAI, or the STT→LLM→TTS Pipeline, with full code examples for Python and TypeScript. |
| [`configure-telephony`](./configure-telephony/) | Connect Twilio or Telnyx as the carrier — phone numbers, webhooks, tunnels, signature verification. |
| [`add-tools-and-handoffs`](./add-tools-and-handoffs/) | Add custom tools (`@tool` / `defineTool`), enable built-in `transfer_call` / `end_call`, and wire output guardrails. |
| [`inspect-calls-and-metrics`](./inspect-calls-and-metrics/) | Mount the live dashboard, read `CallMetrics`, export CSV/JSON, and track per-call cost. |

## Requirements

- **Python 3.11+** or **Node.js 20+**
- `getpatter` package on PyPI or npm — `pip install "getpatter>=0.6.2"` or `npm install "getpatter@>=0.6.2"`
- Provider credentials in env (OpenAI / ElevenLabs / Deepgram / Cerebras / Anthropic / Google as needed)
- Carrier credentials in env (Twilio `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`, or Telnyx `TELNYX_API_KEY`)

## Versioning

Skills version with the SDK. A skill at tag `v0.6.2` is guaranteed to match
the API of `getpatter==0.6.2`. Skills on `main` track the next unreleased
version — pin to a tag for reproducibility.

## License

MIT — same as the Patter SDK.

## Links

- SDK source: <https://github.com/PatterAI/Patter>
- Mintlify docs: <https://docs.getpatter.com>
- Skills spec: <https://agentskills.io/specification>

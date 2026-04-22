# Changelog

## 0.5.0 (2026-04-22)

Patter 0.5.0 ships an instance-based API. Every provider — carriers, engines, STT, TTS, tunnels — is a typed class that reads its credentials from environment variables by default. The result is a four-line quickstart:

```python
from patter import Patter, Twilio, OpenAIRealtime
phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(engine=OpenAIRealtime(), system_prompt="You are helpful.", first_message="Hello!")
await phone.serve(agent)
```

### Public API

- **Carriers**: `Twilio`, `Telnyx` — frozen dataclasses with env fallback (`TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`, `TELNYX_API_KEY` / `TELNYX_CONNECTION_ID` / `TELNYX_PUBLIC_KEY`).
- **Engines**: `OpenAIRealtime`, `ElevenLabsConvAI` — env fallback on `OPENAI_API_KEY` and `ELEVENLABS_API_KEY` / `ELEVENLABS_AGENT_ID`.
- **STT**: `DeepgramSTT`, `WhisperSTT`, `CartesiaSTT`, `SonioxSTT`, `SpeechmaticsSTT` (namespaced only), `AssemblyAISTT` — each reads its own `*_API_KEY` env var.
- **TTS**: `ElevenLabsTTS`, `OpenAITTS`, `CartesiaTTS`, `RimeTTS`, `LMNTTTS` — same env-fallback pattern.
- **Tunnels**: `CloudflareTunnel`, `StaticTunnel`, `Ngrok` — pass via `Patter(tunnel=...)` or use the `serve(tunnel=True)` dev shorthand.
- **Primitives**: `Tool` + `@tool` decorator, `Guardrail` + `guardrail(...)` factory.
- **Top-level flat re-exports** so everything is reachable with a single `from patter import ...` / `import { ... } from "getpatter"`.

### Fixed

- Pipeline dispatch now wires every STT and TTS provider end-to-end. Earlier builds had silent fallthrough paths that dropped Cartesia / Rime / LMNT / Soniox / Speechmatics / AssemblyAI configs before they reached the stream handler.
- Twilio webhook `voice_url` auto-configuration in the TypeScript SDK now matches Python behavior — `serve()` points your number at the running server automatically.
- Consistent env-var error messages across every provider: `"X requires an api_key. Pass api_key='...' or set <ENV_VAR> in the environment."`

### Documentation

- Quickstarts for [Python](./docs/python-sdk/quickstart.mdx) and [TypeScript](./docs/typescript-sdk/quickstart.mdx) rewritten around the four-line pattern with an env-var-first setup.

## 0.4.2 (2026-04-17)

### Changed
- Renamed `sdk/` directory to `sdk-py/` for clearer Python/TypeScript split; CI, pre-commit, pre-push hook, and docs updated accordingly
- Removed remaining Patter Cloud references from `sdk-py/README.md`, `sdk-ts/README.md`, and `docs/examples/custom-voice.*` — only local mode is documented (code still supports both modes)
- TypeScript provider docs parity: added `docs/typescript-sdk/providers/{lmnt,rime}.mdx` and registered them in `docs.json`
- High-signal test cleanup: dropped tautological and redundant tests (#59)
- CI workflow slimmed: removed unused soak job, shrunk test matrices (#60)
- Daily docs/feature-inventory drift check (#55) and daily merged-branch cleanup (#56)
- Extras coverage matrix (#58)

### Fixed
- Pre-commit `default_language_version` Python pin removed (#61)

### Security
- Pre-commit hardening and gitleaks integration (#57, #58)
- Real phone number redacted from tests and documentation (#57)

## 0.4.1 (2026-04-13)

### Changed
- Removed Patter Cloud references from SDK READMEs and custom-voice examples (#17)
- Updated PyPI publishing to use trusted publishers with OIDC authentication (#18)

## 0.4.0 (2026-04-13)

### Added
- Comprehensive test suite: 1,766 tests across unit, integration, E2E (Playwright), soak/stress, and security categories (#14)
- Built-in cloudflared tunnel for local mode — automatically expose local development server to internet (#16)
- Python SDK test coverage raised to 82%
- TypeScript SDK test coverage raised to 80.64%

### Fixed
- Dashboard JavaScript escaping bug (`fmt\$` → `fmt$`) that was breaking all client-side dashboard interactivity since v0.3.1
- `asyncio.get_event_loop()` compatibility issues on Python 3.14 in test files (#13)
- Express v5 type compatibility for `req.params` (#10)

### Changed
- SDK rebrand to getpatter.com with 30 comprehensive examples and dashboard redesign (#12, #11)
- Added Patter SDK title below banner in README (#32)
- Improved documentation and developer tooling section (#33)

## 0.3.0 (2026-04-10)

### Added
- Per-call cost tracking with actual cost queries from Twilio, Telnyx, and Deepgram APIs
- Per-turn latency profiling with avg and p95 aggregation
- Embedded web dashboard with real-time SSE updates at `/dashboard`
- B2B REST API (`/api/v1/calls`, `/api/v1/analytics/*`)
- CSV/JSON export for call data
- `LLMProvider` protocol for pluggable LLM providers (bring your own Anthropic, Gemini, etc.)
- `MetricsStoreProtocol` for custom metrics backends (Prometheus, Datadog, etc.)
- Webhook HMAC signing (`X-Patter-Signature` header) for B2B webhook verification
- `Patter.tool()` factory method in both Python and TypeScript SDKs
- `RemoteMessageHandler` for `on_message` as HTTP webhook or WebSocket URL
- Built-in LLM loop with OpenAI Chat Completions and automatic tool calling
- Test mode (terminal REPL) for agent development without telephony
- Output guardrails (blocked terms + custom check function)
- Dynamic variable substitution in system prompts
- Connection pooling for webhook HTTP clients
- Bounded conversation history with O(1) deque

### Changed
- Extracted shared handler utilities into `handlers/common.py` (Python) and `handler-utils.ts` (TypeScript)
- Dashboard uses in-memory store only (removed SQLite dependency from SDK)
- Improved type annotations across Python SDK models

### Fixed
- XSS protection in dashboard (HTML escaping on all user-controlled values)
- SSE deadlock in MetricsStore (publish outside lock + subscriber snapshot)
- ESM compatibility (`import crypto from 'node:crypto'` instead of `require`)
- Server binding `0.0.0.0` for webhook reachability (was `127.0.0.1`)
- Safe integer parsing on API query parameters with fallback defaults
- Route ordering (`/calls/active` before `/calls/{call_id}`)
- Token encoding in SSE URL with `URLSearchParams`

### Security
- SSRF protection on webhook URLs (private IP blocking)
- Insecure webhook warning for `http://` and `ws://` URLs
- Dashboard authentication warning when token is not set
- Twilio SID format validation to prevent path traversal
- E.164 phone number validation
- Prompt injection sanitization in variable values

## 0.2.0 (2026-04-03)

### Features
- Three voice modes: OpenAI Realtime, ElevenLabs ConvAI, Pipeline
- Twilio and Telnyx carrier support
- Embedded local mode — no backend needed
- Agent with system prompt, tools, and dynamic variables
- Built-in system tools: transfer_call, end_call
- Call recording via Twilio API
- Answering machine detection + voicemail drop
- DTMF keypad input forwarded to agent
- Conversation history tracking per call
- Mark-based barge-in for natural interruptions
- Webhook retry (3x) with fallback
- Custom TwiML parameters passthrough
- MCP server for Claude Desktop
- Cloud mode with REST API (agents, numbers, calls)
- Python + TypeScript SDKs with full parity

### Security
- XML escaping for TwiML injection prevention
- API keys as private attributes
- audioop guard for Python 3.13

## 0.1.0 (2026-03-31)

Initial release.

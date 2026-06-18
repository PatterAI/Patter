# Development Log

## Log

### [2026-06-18] — GeminiLive engine marker (TS) — make `agent({ engine: new GeminiLive() })` work

**Type:** feat
**Branch:** feat/gemini-3.1-live-demo

**What it does:**
Adds the high-level `GeminiLive` engine marker to the TypeScript SDK and wires it
through the client dispatch → adapter factory → stream handler so a user can pass
`engine: new GeminiLive({ ... })` to `Patter.agent()` exactly like `OpenAIRealtime`
/ `OpenAIRealtime2` / `ElevenLabsConvAI`. Before this, `GeminiLiveAdapter` (the
low-level runtime adapter) existed but was an orphan — nothing in the call path
constructed it, and passing it directly to `agent({ engine })` threw "Unknown
engine". The marker is the missing category-1 config object (`readonly kind =
"gemini_live"`); the existing adapter is the category-2 runtime object.

**Implementation details:**
- New marker `engines/gemini.ts` mirrors `engines/openai-2.ts`: immutable, carries
  `apiKey` (falls back to `GEMINI_API_KEY` / `GOOGLE_API_KEY`), `model`, `voice`,
  `language`, `temperature`. Exported from `index.ts`.
- `client.ts` dispatch: new `instanceof GeminiLive` branch sets
  `provider = 'gemini_live'`; added to both valid-provider whitelists.
- `server.ts` `buildAIAdapter`: new `provider === 'gemini_live'` branch constructs
  `GeminiLiveAdapter` from the marker's own credentials, forwarding the same
  engine-agnostic `tools` array (agent tools + transfer_call/end_call/handoff).
- `stream-handler.ts`: widened the `AIAdapter` union to include
  `GeminiLiveAdapter`; routed the `function_call` event to `handleFunctionCall`
  for Gemini (it shares the `{call_id,name,arguments}` shape and
  `sendFunctionResult` signature, so the tool round-trip works unchanged); sent
  `firstMessage` via `sendText` for Gemini (no dedicated `sendFirstMessage`).
- `types.ts`: `AgentOptions.engine` and `.provider` widened for `gemini_live`.

**Files changed:**

| File | Change |
|------|--------|
| `libraries/typescript/src/engines/gemini.ts` | NEW — `GeminiLive` marker + `GeminiLiveOptions` |
| `libraries/typescript/src/index.ts` | export `GeminiLive` / `GeminiLiveOptions` |
| `libraries/typescript/src/client.ts` | engine dispatch + provider whitelists |
| `libraries/typescript/src/server.ts` | `buildAIAdapter` gemini branch + adapter union |
| `libraries/typescript/src/stream-handler.ts` | adapter union + function_call + firstMessage gates |
| `libraries/typescript/src/types.ts` | `engine` / `provider` types widened |

**Breaking changes:** None — purely additive; all existing engines unchanged.

**Known gaps (NOT addressed here — see blockers in the impl report):**
- **Python parity is NOT shipped.** Unlike TS (one `StreamHandler` driving any
  adapter via `buildAIAdapter`), Python dispatches each provider mode to a
  dedicated `StreamHandler` subclass selected in three telephony files
  (twilio/telnyx/plivo). A `GeminiLive` Python marker would need a whole new
  `GeminiLiveStreamHandler` + 3 dispatcher edits — far more than a thin wrapper.
  This violates `sdk-parity` and must be a follow-up PR before merge.
- **Outbound/inbound audio codec is unverified.** `GeminiLiveAdapter` emits raw
  PCM 24 kHz and expects PCM 16 kHz in, but the stream handler's audio paths
  forward adapter bytes assuming the adapter self-transcodes to mulaw 8 kHz (as
  the OpenAI GA adapter does). Real-call audio will likely be wrong until the
  handler transcodes for the Gemini adapter (the Task 10 live-call gate).

### [2026-06-18] — GeminiLiveAdapter: audio field fix, apiVersion auto-detect, Gemini 3.1 Flash Live model

**Type:** fix + feat
**Branch:** feat/gemini-3.1-live-demo

**What it does:**
Fixes two bugs in `GeminiLiveAdapter` that prevented it from working with current google-genai SDKs:
(1) `sendAudio`/`send_audio` was using the deprecated `media:` field — now uses `audio:`.
(2) All models were forced to `v1alpha`; `gemini-3.1-flash-live-preview` works on `v1beta`.
Adds `apiVersion`/`api_version` option with auto-detection from model name, the new
`gemini-3.1-flash-live-preview` model constant, and its pricing row.

**Implementation details:**
- Auto-detect: model name containing 'native-audio' → v1alpha; all others → v1beta (SDK default)
- `connect()` now gates on a `_ready` promise that resolves when the receive loop starts,
  preventing callers from streaming audio before the session is established
- Pricing: same tier as 2.5-flash-live-native-audio ($0.30 input / $2.50 output per 1M tokens)

**Files changed:**

| File | Change |
|------|--------|
| `libraries/typescript/src/providers/gemini-live.ts` | audio field fix; apiVersion option; _ready gate; model constant |
| `libraries/typescript/src/pricing.ts` | gemini-3.1-flash-live-preview pricing row |
| `libraries/typescript/tests/gemini-live.mocked.test.ts` | 5 mocked tests |
| `libraries/python/getpatter/providers/gemini_live.py` | audio= fix; api_version option; enum value; module constant |
| `libraries/python/tests/test_gemini_live.py` | 5 mocked tests (parity) |

**Tests added:**
- `libraries/typescript/tests/gemini-live.mocked.test.ts` — 5 mocked tests
- `libraries/python/tests/test_gemini_live.py` — 5 mocked tests

**Breaking changes:** None — all new fields are optional with safe defaults.

**Docs to update:**
- [ ] `docs/typescript-sdk/providers/gemini-live.mdx` — add `apiVersion` option
- [ ] `docs/python-sdk/providers/gemini-live.mdx` — add `api_version` option

# Development Log

## Log

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

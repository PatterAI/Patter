# Development Log

## Log

### [2026-06-30] — Telnyx native-audio realtime keepalive (comfort-noise pump)

**Type:** fix
**Branch:** fix/telnyx-native-audio-bridge
**PR:** (unreleased)

**What it does:**
Native-audio realtime engines (GeminiLive / OpenAIRealtime2 / ConvAI) answered
over Telnyx but produced no caller audio and the call dropped ~1.6 s in
(`normal_clearing`, "1 turn"); Twilio worked. Root cause: the realtime path puts
ZERO bytes on the carrier between the carrier `start` event and the model's first
audio delta (cold `adapter.connect()` + model TTFT + resampler warmup, often
>1.5 s). Twilio tolerates that gap; Telnyx clears the idle bidirectional RTP leg.
The fix pumps paced μ-law-8k silence (the exact frame format already proven on
Telnyx, 160 bytes / 20 ms, `0xFF` digital zero) from stream-start until the first
real model frame, keeping the outbound leg primed. Self-cancels the instant real
model audio arrives. Pipeline mode never arms the pump (it already plays
firstMessage TTS within ~200 ms); Twilio/Plivo are unaffected (they accept the
same silence frame and have no such timeout).

**Implementation details:**
- TS: added `comfortNoiseTimer` state + `MULAW_SILENCE_FRAME`/`COMFORT_NOISE_INTERVAL_MS`
  constants and `startComfortNoise`/`stopComfortNoise` to `StreamHandler`. Armed at
  the end of `initRealtimeAdapter` (realtime-only, post-connect); cancelled as the
  first statement of `onAdapterAudio` and in `handleStop`/`handleWsClose`.
- Python: added `_comfort_noise_task` + `_MULAW_SILENCE_FRAME`/`_COMFORT_NOISE_INTERVAL_S`
  and `_start_comfort_noise`/`_stop_comfort_noise` to the base `StreamHandler`. Armed
  after the `_forward_events` task is spawned in both realtime subclasses'
  `start()` (`OpenAIRealtimeStreamHandler`, `ElevenLabsConvAIStreamHandler`);
  cancelled in each `_forward_events` first-audio guard and in each subclass
  `cleanup()` (the single carrier teardown funnel for stop + ws-close).
- No change to the transcode chain, `sendAudio` envelopes, streaming negotiation,
  or `bytesPerMs` bookkeeping — those were already correct.

**Files changed:**

| File | Change |
|------|--------|
| `libraries/typescript/src/stream-handler.ts` | Comfort-noise pump (constants, state, start/stop, arm/cancel/teardown) |
| `libraries/python/getpatter/stream_handler.py` | Python parity comfort-noise pump (base helpers + realtime subclass wiring) |

**Tests added:**
- `libraries/typescript/tests/telnyx-comfort-noise.mocked.test.ts` — 5 cases (silence-frame decode, pump-then-stop, pipeline never arms, stop/ws-close no leak)
- `libraries/python/tests/test_telnyx_comfort_noise.py` — 5 cases (silence-frame decode, pump-then-stop, idempotent start, stop no leak, cleanup stops pump)

**Breaking changes:** None

**Docs to update:**
- [ ] None (internal bug fix; no public surface change)

### [2026-06-18] — Gemini Live Python parity: audio codec transcode + `GeminiLive` marker

**Type:** feat + fix
**Branch:** feat/gemini-3.1-live-demo

**What it does:**
Brings the Python SDK to parity with the TS Gemini Live work. (1) `GeminiLiveAdapter`
now transcodes carrier audio in BOTH directions, mirroring `OpenAIRealtime2Adapter`:
`send_audio` decodes carrier mu-law 8 kHz → PCM16, applies a 2x inbound VAD gain, and
upsamples to `input_sample_rate` (16 kHz) before sending; `receive_events` resamples
Gemini's PCM output (24 kHz) → 16 kHz → 8 kHz and mu-law-encodes it, yielding one
160-byte / 20 ms `audio` frame at a time so Twilio's playout scheduler doesn't stall.
(2) Adds the `gemini-3.1-flash-live-preview` pricing row under `google` (without it
Python billed $0 for the model). (3) Adds the `GeminiLive` engine marker so callers can
pass `engine=GeminiLive(...)` to `Patter.agent()`, wired through `_unpack_engine`,
`ProviderMode`, `LocalConfig.google_key`, and the `__init__` exports.

**Implementation details:**
- Separate `StatefulResampler` instances per direction (inbound 8k→16k, outbound
  24k→16k + 16k→8k), created lazily per session and cleared in `close()`. Sharing one
  instance corrupts both directions because `audioop.ratecv` carries filter state.
- The 16k→8k outbound stage uses audioop's anti-alias FIR (avoids raspy speech); the
  inbound gain is a named `INBOUND_GAIN` constant, clamped to Int16.
- Carrier rate / frame size / gain are named constants (`CARRIER_SAMPLE_RATE`,
  `MULAW_FRAME_BYTES`, `INBOUND_GAIN`). When a configured rate already equals 8 kHz the
  resample is skipped and only the mu-law codec runs.
- Module docstring updated: the stale "native-audio is v1alpha-only / client must pass
  v1alpha" note now describes the model-name auto-detect (v1beta default).

**Files changed:**

| File | Change |
|------|--------|
| `libraries/python/getpatter/providers/gemini_live.py` | inbound/outbound transcode + constants + docstring |
| `libraries/python/getpatter/pricing.py` | `gemini-3.1-flash-live-preview` row under `google` |
| `libraries/python/getpatter/engines/gemini.py` | NEW — `GeminiLive` marker |
| `libraries/python/getpatter/engines/__init__.py` | export `gemini` |
| `libraries/python/getpatter/__init__.py` | flat alias + `__all__` for `GeminiLive` |
| `libraries/python/getpatter/client.py` | `_unpack_engine` gemini branch + key backfill |
| `libraries/python/getpatter/models.py` | `ProviderMode` += `gemini_live` |
| `libraries/python/getpatter/local_config.py` | `google_key` field |
| `libraries/python/tests/test_gemini_live.py` | 4 new mocked tests (real transcode + marker dispatch) |

**Tests added:**
- `test_send_audio_transcodes_carrier_mulaw8_to_pcm16k` — real mu-law decode + resample
- `test_receive_transcodes_pcm24_to_mulaw8_20ms_frames` — real resample + mu-law encode
- `test_gemini_live_engine_marker_and_dispatch` / `test_gemini_live_engine_requires_key`

**Breaking changes:** None — additive; `GeminiLiveAdapter.send_audio` now expects
carrier mu-law (it previously expected pre-resampled PCM), but no call path fed it yet.

**Known gap (NOT addressed — see blockers):** runtime wiring into the telephony
StreamHandler. Python uses a dedicated `StreamHandler` subclass per provider mode
(twilio/telnyx/plivo each dispatch to one), unlike TS's single handler. A
`gemini_live` runtime branch needs a new ~400-line `GeminiLiveStreamHandler` subclass,
not a thin branch — deferred to a follow-up.

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

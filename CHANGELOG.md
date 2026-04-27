# Changelog

## Unreleased

_(no entries yet — next version will land here)_

## 0.5.3 (2026-04-27)

Cost-accuracy, audio-pipeline, and observability hardening across both SDKs, plus opt-in per-call filesystem logging.

### Added — per-call filesystem logging
- **`CallLogger` (both SDKs)** — opt-in via `PATTER_LOG_DIR` env var. Writes per-call
  `metadata.json` (atomic) + `transcript.jsonl` + `events.jsonl` under a
  date-partitioned directory tree (`calls/YYYY/MM/DD/<call_id>/`). Schema is
  identical in Python and TypeScript so multi-runtime deployments share one tree.
- **Phone redaction** (`PATTER_LOG_REDACT_PHONE`): `mask` (default, last-4),
  `full`, or `hash_only` (sha256 prefix).
- **Retention sweep** (`PATTER_LOG_RETENTION_DAYS`, default 30) runs on ~2% of
  calls — no daemon required. Set to `0` to keep forever.
- `maskPhoneNumber` export added to TS `stream-handler` for parity with Python
  `mask_phone_number`.
- Docs: new `observability → call logging` page for both SDKs.

### Fixed — user callback return value dropped
- Python `EmbeddedServer._wrap_callbacks` silently threw away the value returned
  by `on_call_start`, defeating per-call config overrides. Wrapper now returns
  it so `apply_call_overrides` receives the user's dict.

### Fixed — cost accuracy (third audit wave, 9 agents)
- **Deepgram rate was batch not streaming** — `deepgram: $0.0043/min` was the batch/pre-recorded rate. Patter's Nova-3 streaming default actually bills at **$0.0077/min** (monolingual). Users were under-reporting cost by ~45%.
- **ElevenLabs rate was Creator-plan overage not Flash/Turbo API** — `$0.18/1k chars` is only correct on the Creator plan's overage tier. The `eleven_flash_v2_5` / `eleven_turbo_v2_5` direct-API rate is **$0.06/1k chars**. Users on the API were over-reporting cost by ~3×.
- **Six new provider pricing entries added** so their bills no longer silently display $0: `assemblyai` ($0.0025/min), `cartesia_stt` ($0.0025/min), `cartesia_tts` ($0.030/1k), `soniox` ($0.002/min), `speechmatics` ($0.0173/min), `rime` ($0.030/1k), `lmnt` ($0.050/1k), `openai_tts_hd` ($0.030/1k). Users still see $0 if they configure a provider we don't price yet — documented as a deferred item.

### Fixed — model defaults
- **Gemini Live default retired** — `gemini-2.0-flash-exp` was experimental preview, no longer in Google docs. Updated to `gemini-live-2.5-flash-preview`.
- **ElevenLabs model default modernised** — `eleven_turbo_v2_5` → `eleven_flash_v2_5`. Drop-in replacement per ElevenLabs docs: same price tier, ~3× lower latency.

### Fixed — metrics correctness
- **Dangling-turn guard at call end** — abrupt hangup mid-turn used to drop the partial latency/cost state silently. `endCall()` / `end_call()` now call `recordTurnInterrupted()` if a turn is still active, so the state flushes cleanly and percentile stats filter it out via `_completedTurns`.
- **Negative `tts_ms` in pipeline streaming** — `recordTtsFirstByte` can fire on the first sentence's first chunk before `recordLlmComplete` (which marks end-of-full-response). The subtraction produced negative ms that showed up as dashboard noise. Clamped to zero in both SDKs.

### Fixed — security
- **Python Twilio webhook could bypass signature verification** if the `twilio` package was missing. The ImportError fallback skipped validation and logged a warning; a deployer without `pip install 'getpatter[local]'` silently accepted any webhook body. Now fails closed with HTTP 503 and a hard error log.
- Python Twilio signature URL is now reconstructed from `config.webhook_url` + `request.url.path` when the full Starlette URL is available, avoiding proxy scheme/port drift. Falls back to string-replace for mock test harnesses.

### Verified (no change needed)
- No hallucinated model IDs anywhere in the codebase.
- Every ElevenLabs voice ID in the name-map still resolves to a live voice (ElevenLabs auto-routes legacy IDs). The `bella` alias now rebrands to the live "Sarah" voice — works but the label is outdated; kept for backwards compat.
- Anthropic `claude-haiku-4-5-20251001`, `claude-sonnet-4-6`, `claude-opus-4-7` all match official Anthropic snapshot IDs.
- Groq `llama-3.3-70b-versatile`, Deepgram `nova-3`, Cartesia `sonic-2`, LMNT `blizzard`, Rime `arcana`, Whisper `whisper-1`, OpenAI `tts-1` — all current in 2026.

### Deferred to 0.6.0 (tracked)
- **Per-model OpenAI Realtime pricing map**: default rates are calibrated for `gpt-4o-mini-realtime-preview`. Users on `gpt-realtime` (~3×) or `gpt-4o-realtime-preview` (~10×) still see under-reported cost. Startup warn (from 0.5.5) is the stopgap.
- **Native `ulaw_8000` negotiation per provider when target is Twilio** — ElevenLabs, LMNT, Cartesia, Rime all accept `ulaw_8000` output format natively. Today we fall through a resample-then-mulaw chain that introduces aliasing. Switching to native negotiation per the ElevenLabs Twilio cookbook is the canonical fix.
- **Replace 5-tap binomial FIR with Kaiser-windowed half-band (31-tap)** — industry stopband is 60-80 dB; our binomial is ~20 dB. `soxr` (LiveKit default) or `scipy.signal.resample_poly` if available.
- **LLM pipeline token tracking** — `anthropic`, `groq`, `cerebras`, `google`, `openai` LLM adapters report latency but never emit token usage. Pipeline-mode `CostBreakdown.llm` is always $0, regardless of actual spend. New `record_llm_usage()` + per-model pricing entries.
- **TS Telnyx outbound wrong codec** — TS `encodePipelineAudio` and `handleAdapterEvent` ship PCM16 16k to Telnyx that negotiated PCMU 8k. Telnyx customers see broken audio. Requires a `TelephonyBridge.encodeAudio` abstraction parity with Python's `TelnyxAudioSender`.
- **TS OpenAI Realtime missing `audioFormat` parameter** — Python has it. Blocks TS Telnyx+Realtime.
- **Runtime WebSocket error/close listeners** across all TS voice providers — today a mid-call WS drop is silent. Needs a shared `_retry.ts` helper.
- **ElevenLabs ConvAI barge-in** — adapter never emits `interruption` event; stream handler has a handler for it that's dead code.
- **Gemini Live never emits `transcript_input`** — `stt_ms` always 0 and `user_text` empty on every Gemini turn.
- **Whisper is unsafe in pipeline mode** — emits `isFinal=true` every ~1s regardless of speech; triggers LLM mid-utterance. Needs VAD gating.
- **Cerebras default `llama3.1-8b` deprecates May 27, 2026** — need migration to `gpt-oss-120b`.

### Fixed — cost accounting (first + second audit waves, 3 + 11 agents)
- **Python `calculate_realtime_cost` would crash on `input_token_details: null`** — `dict.get("...", {})` returns `None` when the key exists with a `None` value, and the chained `.get()` raised `AttributeError`. Switched to `or {}` fallback. TS was already safe via `??`.
- **`cached_tokens_details` ignored** → cached portion was billed at full rate (up to ~33× overcharge on cached audio). Now subtracted from the total and re-billed at the cached rate.
- **Twilio rounds partial minutes up** to the next whole minute ([twilio help 223132307](https://help.twilio.com/articles/223132307)). Our `(seconds/60) * rate` under-reported cost for every call ending on a non-minute boundary. `calculateTelephonyCost` / `calculate_telephony_cost` now apply `ceil(seconds/60)` for Twilio and keep per-second math for Telnyx (which bills per-second).
- **Dashboard had no way to show "saved from cache"** — the `cached_tokens_details` discount was consumed inside `calculateRealtimeCost` and then thrown away. Added `CostBreakdown.llm_cached_savings` (propagated via new `calculateRealtimeCachedSavings` helper + `_totalRealtimeCachedSavings` accumulator) so UI can render `LLM $0.08 (saved $0.02 from prompt caching)`.
- **`mergePricing` (TS) silently defaulted `unit: 'minute'`** for any new provider entry without an explicit unit, masking misconfiguration. Aligned to Python behaviour (fail-closed: cost = 0 when `unit` is missing).
- **`PRICING_VERSION` / `PRICING_LAST_UPDATED` now exported from the TS pricing module** for parity with Python — lets cross-SDK observability dedupe by pricing table version.

### Fixed — latency instrumentation
- **Python `waiting_first_audio = False` default** meant the `firstMessage` turn's `tts_ms` / `total_ms` were never captured in Realtime mode (OpenAI + ElevenLabs ConvAI). Flipped to `True`. Same TS behaviour already — parity restored.
- **Python `response_done` with empty `current_agent_text` left the turn dangling** (TS called `recordTurnInterrupted`, Python didn't). Both now close the active turn as interrupted so the next `speech_stopped` starts a clean turn.

### Fixed — audio pipeline (Python)
- **Python `TwilioAudioSender.send_audio` had no byte-alignment carry** — streaming TTS providers (ElevenLabs, Cartesia, LMNT, Rime, TelnyxTTS) yield chunks of arbitrary byte length including odd counts. Passing an odd buffer to `audioop.ratecv` raises `audioop.error: not a whole number of frames`, crashing the TTS mid-sentence. Now maintains a `_pcm16_carry` byte across calls. Parity with TS `StreamHandler.ttsByteCarry` fix in 0.5.4.
- **TS `ttsByteCarry` could persist across turns on mid-chunk exceptions** (security M1: defensive). Wrapped the three TTS loops in `try/finally` so the carry is always dropped.

### Security
- **`agent.model` was interpolated into warn logs without sanitisation** — dev-supplied string with ANSI escapes could inject colour codes into log aggregators. Now passes through `sanitizeLogValue`.

### Added — observability (LiveKit/Pipecat-style)
- `CallMetrics.latency_p50` and `.latency_p99` alongside `latency_p95` and `latency_avg`. Lets dashboards show the full distribution (typical UX / SLA / cold-start outlier).
- `CostBreakdown.llm_cached_savings` as described above.
- Percentile formula upgraded from `floor(n*p)` (returned max for n<21) to Hyndman-Fan type 7 linear interpolation (same as `numpy.percentile` default). Meaningful on 2-3 sample sets.
- `_completedTurns` helper excludes `[interrupted]` turns and zero-latency turns from every percentile + average computation, so barge-in / cancelled replacements stop dragging the reported numbers toward zero.

### Changed — default rates (2026)
| Provider | Old | New | Why |
|---|---|---|---|
| Twilio | $0.013/min | **$0.0085/min** | Old rate matched neither inbound ($0.0085) nor outbound ($0.0140). Default is now US inbound local (99% of receive-call use cases). |
| OpenAI Realtime audio in | $100/M | **$10/M** | Recalibrated for `gpt-4o-mini-realtime-preview` (Patter default model). |
| OpenAI Realtime audio out | $400/M | **$20/M** | Same (old value was ~20× wrong on default model). |
| OpenAI Realtime text in / out | $5 / $20 per M | **$0.60 / $2.40 per M** | Same. |
| OpenAI Realtime cached audio / text in | — (billed as full) | **$0.30/M / $0.06/M** | New fields. |

Users running non-default Realtime models (`gpt-realtime`, `gpt-4o-realtime-preview`) get a startup warning with instructions to override. See pricing.ts / pricing.py comments for the multipliers.

### Tests
- Added cached-tokens happy path + over-total clamp + null-input-details regression tests in both TS and Python pricing suites.
- Final: TS 1046/1046 · Py 1275/1275.

### Fixed — cost accounting
- **Prompt caching was billed at full rate** — OpenAI Realtime sends `input_token_details.cached_tokens_details.{audio,text}_tokens` as a breakdown of already-counted totals; cached portions are billed at ~3% (audio cached $0.30/M vs full $10/M) and ~10% (text cached $0.06/M vs $0.60/M) of full rates. We were multiplying the full total by the full rate. On long calls with warm KV cache this overcharged display by up to ~30%. `calculateRealtimeCost` / `calculate_realtime_cost` now subtract cached from the full count and apply the reduced rate. `cached_audio_input_per_token` and `cached_text_input_per_token` added to `DEFAULT_PRICING.openai_realtime`.
- **Twilio default was $0.013/min** which matches neither US inbound local ($0.0085) nor US outbound local ($0.0140). Default is now **$0.0085/min** (US inbound local — the 99% case for voice agents receiving calls). Users on toll-free or outbound should override via `Patter({ pricing: { twilio: {...} } })`.
- **Non-default Realtime models under-reported** — if you set `agent.model = "gpt-realtime"` or `"gpt-4o-realtime-preview"`, the dashboard still applied mini-tier rates (3-10× cheaper than actual). Startup now warns if `agent.model` is a realtime model other than `gpt-4o-mini-realtime-preview`, with instructions to override pricing.

### Fixed — latency measurement
- **Python missed the `audio → startTurn` fallback that TS had** (parity bug). When OpenAI emits `response.audio.delta` before `input_audio_buffer.speech_stopped` due to async event reordering, Python would produce a turn with `_turn_start=None` and all-zero latency, silently polluting p95 toward zero. Now matches TS — if audio arrives without an active turn, `start_turn()` fires defensively.
- **Interrupted turns (barge-in, cancelled replacements) inflated p95/avg** — every `[interrupted]` turn entered the percentile buckets with `latency=0` or partial latency, dragging the reported numbers toward zero regardless of real performance. `_completed_turns` helper now filters them out of both p50/p95/p99 and average computations in both SDKs.
- **`total_ms → llm_ms` fallback broke comparability** between pipeline and realtime modes. Removed. In Realtime mode `stt_ms/llm_ms/tts_ms` stay 0 (OpenAI bundles the pipeline internally) and only `total_ms` is meaningful — dashboards should prefer `total_ms` for Realtime and the component buckets for Pipeline.
- **`recordSttComplete` was called in Python realtime but not TS** — produced different latency bucket splits between the two SDKs on identical calls. Added in TS `transcript_input` handler for parity.
- **p95/p99 returned the sample maximum for any n < 21** — the previous `floor(n * 0.95)` formula was numerically meaningless on short calls. Replaced with linear interpolation between order statistics (Hyndman-Fan type 7, same as `numpy.percentile` default). Both SDKs.
- **`firstMessage` latency wasn't measured in Python** (TS measured it for pipeline + realtime). Python now emits a turn-level metric for the first greeting in both modes.

### Added — observability (LiveKit/Pipecat-style)
- `CallMetrics` now exposes `latency_p50` and `latency_p99` alongside `latency_p95` and `latency_avg`. Useful to detect cold-start outliers (p99) and typical UX latency (p50). Dashboards can render all four side by side.
- Both SDKs use the same percentile formula and same filtering (excludes interrupted turns).

### Fixed — initial audio + pricing pass
- **OpenAI Realtime cost display was 5-20× inflated** — `DEFAULT_PRICING.openai_realtime` was calibrated for `gpt-4o-realtime-preview` at mid-2024 rates ($100/M audio input, $400/M audio output, the latter already wrong vs OpenAI's then-published $200/M). Patter's default model is `gpt-4o-mini-realtime-preview`, which is billed at 1/10 the non-mini rate. The combined error made the dashboard report numbers roughly 5-20× higher than what OpenAI actually charged. Recalibrated to 2026 mini rates ($10/M audio in, $20/M audio out, $0.60/M text in, $2.40/M text out). Users on a different Realtime model should override via `Patter({ pricing: { openai_realtime: { ... } } })`.
- **Turn latency p95 artificially low in Realtime mode** — latency was measured from the `transcript_input` event (OpenAI's notification that ASR finished) to the first audio delta, but OpenAI generates the response in parallel with ASR so the two events arrive within tens of milliseconds of each other server-side. Real end-to-end latency is much higher. Now measuring from `input_audio_buffer.speech_stopped` (server VAD detected user finished talking) to first audio output — a truer proxy for user-perceived latency. Fallback to `transcript_input` kept for configs without server VAD.

### Fixed
- **TTS audio corruption on Twilio calls (pipeline mode)** — two independent bugs in the TypeScript audio pipeline both contributed to the symptom "voice buried under loud continuous noise" reported by users on pipeline-mode calls:
  1. **Byte misalignment across HTTP chunks.** Streaming TTS providers (ElevenLabs, OpenAI, Cartesia, ...) yield chunks of arbitrary byte length, including odd counts. `resample16kTo8k` silently dropped the trailing odd byte via `Math.floor(len / 2)`. That byte should have been the HIGH byte of the next int16 sample, paired with the first byte of the following chunk as the LOW byte — without the carry, every sample from the second chunk onwards was byte-swapped, turning modest amplitudes into huge magnitudes that the listener perceives as continuous hiss. Fixed by maintaining a `ttsByteCarry` buffer across chunks in `StreamHandler.encodePipelineAudio` so the resampler always sees even-length int16-aligned input. Affects every pipeline TTS provider, not just ElevenLabs.
  2. **Missing anti-aliasing filter on 2:1 downsampling.** `resample16kTo8k` was a naive `y[i] = x[2i]` decimation with no low-pass filter. All input energy between 4 kHz and 8 kHz (a large chunk of TTS voice: fricatives, sibilants, harmonics) folded back into the 0-4 kHz output band as hiss. Fixed by applying a 5-tap binomial low-pass FIR (`[1, 4, 6, 4, 1] / 16`) before decimation. Matches the Python SDK which uses `audioop.ratecv` (itself anti-aliased).
  The Python SDK was unaffected by both bugs — `audioop.ratecv` both anti-aliases and raises on misaligned input, forcing upstream code to keep alignment. Pure TypeScript parity violation.
- **Audio aliasing on 24 kHz → 16 kHz resampling** — same bug class in `resample24kTo16k`, used when OpenAI TTS (24 kHz native) runs in pipeline mode. Replaced the "take 2 of every 3 samples" logic with linear interpolation so content between 8 and 12 kHz doesn't alias into the 0-8 kHz band.
- **Anthropic default model** — updated from `claude-3-5-sonnet-20241022` (deprecated by Anthropic, now returns `404 not_found_error`) to `claude-haiku-4-5-20251001`. Haiku 4.5 is faster, cheaper, and more suitable as a default for voice agents where every conversation turn costs a LLM call. Pass `model="claude-sonnet-4-6"` or similar to override.

### Changed (dependencies)
- `npm install getpatter` is now ~90 MB instead of ~357 MB (-75%). Heavy optional runtimes are no longer installed by default:
  - `onnxruntime-node` (~210 MB) moved to `peerDependencies` with `optional: true`. Required only if you use `SileroVAD` or `DeepFilterNetFilter`. Install with `npm install onnxruntime-node` when needed — the SDK throws a clear error at construction otherwise.
  - `@google/genai` moved to `peerDependencies` with `optional: true`. Required only if you use `GeminiLive` as an engine. Install with `npm install @google/genai` when needed.
- `cloudflared` moved from `optionalDependencies` to `dependencies` in the TypeScript SDK — the built-in tunnel (`Patter({ tunnel: true })`) is now guaranteed to Just Work out of the box (the npm `cloudflared` package auto-downloads the binary).
- Python: the `cloudflared` binary is still required on PATH (via `brew install cloudflared` / `apt install cloudflared`) — there is no Python wrapper package available. The error at `tunnel=True` time already lists install options.
- Python `getpatter[tunnel]` extra is now an empty alias kept for backwards compatibility.

### Unchanged
- All other optional extras (`getpatter[silero]`, `getpatter[anthropic]`, `getpatter[google]`, etc.) stay as extras.

## 0.5.2 (2026-04-23)

### Fixed
- **ElevenLabs default voice** — changed from Rachel (`21m00Tcm4TlvDq8ikWAM`) to Sarah (`EXAVITQu4vr4xnSDxMaL`). Rachel is a library voice that free-tier ElevenLabs accounts cannot use, so `new ElevenLabsTTS()` / `ElevenLabsTTS()` without an explicit `voice_id` used to fail on the first synthesis with `402 paid_plan_required`. Sarah is a premade voice available to all accounts.
- `alloy` alias now resolves to Sarah for the same reason.
- Startup banner now renders at the top of the terminal output (before tunnel/webhook setup logs), with a visually distinct Dashboard section.
- Reduced log noise during calls: removed per-frame `WS event:`, `Telnyx event:`, `Upgrade request:`, `WebSocket connected:` lines. Only `Call started` / `Call ended` remain.

### Unchanged
- The `rachel` alias still resolves to `21m00Tcm4TlvDq8ikWAM` — pass `voice="rachel"` explicitly to keep using it (requires a paid ElevenLabs plan).

## 0.5.1 (2026-04-22)

### Added
- **First-class `llm=` selector on `phone.agent()`** — pick any of 5 LLM providers the same way you pick STT/TTS.
  - `OpenAILLM`, `AnthropicLLM`, `GroqLLM`, `CerebrasLLM`, `GoogleLLM` — all instance-based with env-var fallback.
  - Namespaced imports: `from getpatter.llm import openai, anthropic, groq, cerebras, google` (Python) / `import * as anthropic from "getpatter/llm/anthropic"` (TypeScript). (Note: TypeScript subpath imports were not exposed in the published `exports` map; use flat barrel imports from `"getpatter"` instead.)
  - Flat imports: `from getpatter import AnthropicLLM, GroqLLM, ...` / `import { AnthropicLLM, GroqLLM, ... } from "getpatter"`.
- Tool calling works across all 5 providers — each adapter normalizes to Patter's unified `{type: "text" | "tool_call" | "done"}` chunk protocol.
- `GoogleLLM` reads `GEMINI_API_KEY` preferred, falls back to `GOOGLE_API_KEY`.

### Unchanged (no break from 0.5.0)
- `on_message` / `onMessage` callback still works for custom LLM logic. Mutually exclusive with `llm=` (conflict raises at `serve()` time).
- When no `llm=` and no `on_message` but `OPENAI_API_KEY` is set, the default OpenAI LLM loop keeps running.

## 0.5.0 (2026-04-22)

Patter 0.5.0 ships an instance-based API. Every provider — carriers, engines, STT, TTS, tunnels — is a typed class that reads its credentials from environment variables by default. The result is a four-line quickstart:

```python
# (post-rename: package is now `getpatter` since 0.5.0)
from getpatter import Patter, Twilio, OpenAIRealtime
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
- **Top-level flat re-exports** so everything is reachable with a single `from getpatter import ...` / `import { ... } from "getpatter"`.

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

# Changelog

## Unreleased

## 0.6.0 (2026-05-08)

### Fixed

- **`getpatter.tts.elevenlabs.TTS` facade now forwards `language_code`, `voice_settings`, and `chunk_size`** (Python + TypeScript parity, fully backward-compatible). The HTTP-streaming ElevenLabs facade had a narrower `__init__` signature than the underlying `providers.elevenlabs_tts.ElevenLabsTTS` provider — accepting only `api_key/voice_id/model_id/output_format` — so users who built a TTS via the public facade silently lost language-aware synthesis and could not pass `voice_settings`. Multilingual scenarios (`feat_italian_language_live` in the agent-to-agent acceptance suite) crashed downstream with `TypeError: TTS.__init__() got an unexpected keyword argument 'language_code'` once the runner started passing the kwarg. Files: `libraries/python/getpatter/tts/elevenlabs.py`, `libraries/typescript/src/tts/elevenlabs.ts`. New regressions: `libraries/python/tests/unit/test_tts_facade_language.py` (7 tests covering language_code forwarding, voice_settings forwarding, defaults preservation, env-key resolution, missing-key error) and `libraries/typescript/tests/tts-facade-language.test.ts` (7 parity tests). The TS facade also tightened its `ElevenLabsTTSOptions` interface fields with `readonly` to match the project-wide immutability rule.

- **Cerebras + Groq LLM pricing — silent under-billing fix** (Python + TypeScript parity, fully backward-compatible). `calculate_llm_cost` / `calculateLlmCost` returns `$0.00` when the requested `(provider, model)` is not in the rate table. The Cerebras default model `gpt-oss-120b` (set as the SDK default in 0.5.4) and `llama3.1-8b` were both missing from `LLM_PRICING["cerebras"]` / `llmPricing.cerebras`, so every Patter user running the Cerebras default billed `llm_cost = 0` on the dashboard — silent under-billing across the entire Cerebras user base. Same class of bug on Groq: only `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` were priced; the other 5 enum entries (`llama-3.3-70b-specdec`, `llama3-70b-8192`, `llama3-8b-8192`, `mixtral-8x7b-32768`, `gemma2-9b-it`) all silently billed $0. Added per-1M-token rates for every `CerebrasModel` / `GroqModel` enum value (best-effort placeholders sourced from cerebras.net and groq.com/pricing as of 2026-05-08 — `# rate as of 2026-05-08; verify against ...` comment in the source). Files: `libraries/python/getpatter/pricing.py`, `libraries/typescript/src/pricing.ts`. New regressions in `libraries/python/tests/test_pricing.py::TestLLMCostBilling` (5 tests covering the Cerebras default, the deprecating llama3.1-8b, both full-enum coverage assertions, and the specdec-vs-versatile rate distinction) and `libraries/typescript/tests/pricing.test.ts` (`LLM cost billing — Cerebras + Groq silent under-billing regression` describe block, parity-equivalent 5 tests). Tests use real `calculateLlmCost` / `calculate_llm_cost` math — no mocks (per `.claude/rules/authentic-tests.md`).

- **AssemblyAI STT (Python): coalesce small Twilio frames before sending to WebSocket** (achieves parity with TypeScript, fully backward-compatible). Twilio's media stream emits 20 ms / 160-byte mulaw frames, which is below AssemblyAI v3's 50 ms minimum frame size — the server emits error 3007 and closes the WebSocket, surfacing as `RuntimeError: Not connected. Call connect() first.` after ~6 s of silent audio drop (scenario `stt_matrix_twilio_assemblyai`). The TypeScript adapter (`libraries/typescript/src/providers/assemblyai-stt.ts`) already buffered to a 60 ms target; the Python `AssemblyAISTT.send_audio` (`libraries/python/getpatter/providers/assemblyai_stt.py`) forwarded each Twilio frame untouched. Fix: added an internal `_audio_buffer` bytearray + lazy-computed `_audio_buffer_target_bytes` (default 60 ms — one Twilio frame above the 50 ms floor with jitter headroom; 480 bytes for mulaw 8 kHz, 1920 bytes for PCM s16le 16 kHz) so 3 Twilio frames are batched into one ws send. Trailing tail is drained by a new `flush_audio()` method called automatically from `close()` so the final <60 ms slice is not dropped. `send_audio` now silently returns when the WS is not yet open (mirrors the TS adapter and Deepgram baseline) — Twilio starts streaming audio during the 200–500 ms WS handshake, so the prior `RuntimeError` killed every AssemblyAI call. New unit tests `libraries/python/tests/unit/test_assemblyai_stt_buffering.py` (5 tests: 10 × 20 ms coalescing, 60 ms target sanity for mulaw 8 kHz and PCM s16le 16 kHz, `flush_audio` trailing-tail drain, pre-connect silent drop, empty-chunk noop) and `libraries/typescript/tests/unit/assemblyai-stt-buffering.test.ts` (5 tests: 10 × 20 ms → 3 ws sends, sub-target hold-back, pre-connect no-throw, exact-target single send for both encodings).

- **Pipeline mode now fires `on_transcript` for assistant turns and tool calls** (Python + TypeScript parity, fully backward-compatible). Two related observability gaps surfaced via the agent-to-agent acceptance suite:
  1. The pipeline LLM-loop / `on_message` paths (`stream_handler.py` ~lines 2459–2547 and `stream-handler.ts` ~lines 1525 / 1751 / 1809) appended the assistant turn to `conversation_history` + `transcript_entries` but never invoked the user-supplied `on_transcript` callback. Realtime mode already does this via `_flush_assistant_turn` / `flushAssistantTurn`. Pipeline-mode hosts therefore observed user turns but no assistant replies.
  2. The pipeline `LLMLoop` (`services/llm_loop.py` `_execute_tool` and `llm-loop.ts` `executeTool`) ran tools but never surfaced the call/result back to the `StreamHandler`. Realtime mode emits two `role=tool` transcript events per tool invocation (one for the call `name(argsJson)`, one for the result `name(...) → result`) via `_emit_tool_event`. Pipeline mode emitted neither.

  Fix: added an optional `on_tool_call: Callable[[name, args, result], Awaitable[None]] | None` ctor arg + `set_on_tool_call(...)` setter on `LLMLoop` (Py) and `setOnToolCall(...)` on `LLMLoop` (TS). `PipelineStreamHandler` now wires it to a new `_record_tool_call` (Py) / `recordToolCall` (TS) helper that emits the same two-event shape as realtime. Three pipeline assistant-turn sites (`_process_streaming_response`-then-llmloop / streaming-on_message / `_process_regular_response` on Py; `runPipelineLlm` / `runRegularLlm` / `handleWebSocketResponse` on TS) now route through a new `_emit_assistant_transcript` (Py) / `emitAssistantTranscript` (TS) helper that pushes history AND fires `on_transcript`. Observer exceptions are caught + logged so a misbehaving callback cannot abort a live call. Defaults unchanged (callback is opt-in via the existing `on_transcript` field on `Patter`). New mocked tests `test_on_tool_call_observer_fires_after_successful_tool_execution`, `test_on_tool_call_observer_exceptions_do_not_abort_loop`, `test_pipeline_stream_handler_emits_three_transcript_events_for_tool_turn` (Py) and `[mocked] onToolCall observer (pipeline parity with realtime emitToolEvent)` describe block in `tests/llm-loop.test.ts` (TS) — including an end-to-end assertion that one user turn → one tool call yields exactly three `on_transcript` events (tool call + tool result + assistant) in order. Files: `libraries/python/getpatter/services/llm_loop.py`, `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/llm-loop.ts`, `libraries/typescript/src/stream-handler.ts`.

- **OpenAI Realtime engine wrapper now forwards `reasoning_effort` and `input_audio_transcription_model`** (Python + TypeScript parity, fully backward-compatible). The two recently-shipped Realtime knobs were exposed on `OpenAIRealtimeAdapter` itself but the high-level `engines.openai.Realtime` (Py) / `Realtime` from `engines/openai` (TS) silently dropped them — users had to bypass the engine wrapper and instantiate `OpenAIRealtimeAdapter` directly to get `reasoning.effort` (gpt-realtime-2 reasoning tier) or a non-default transcription model (`gpt-realtime-whisper`, `gpt-4o-transcribe`). Wrapper now accepts both as optional kwargs / readonly fields, threads them through `Patter._unpack_engine` → `Agent.openai_realtime_reasoning_effort` / `Agent.openai_realtime_input_audio_transcription_model` (Py) and through `buildAIAdapter` directly off `agent.engine` (TS) into the adapter constructor. Defaults stay `None` / `undefined` so existing `OpenAIRealtime(model=..., voice=...)` callers compile and run unchanged. Files: `libraries/python/getpatter/engines/openai.py`, `libraries/python/getpatter/models.py`, `libraries/python/getpatter/client.py`, `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/engines/openai.ts`, `libraries/typescript/src/server.ts`. New unit tests: `test_realtime_engine_forwards_reasoning_and_transcription_to_adapter` (Py, mocks `OpenAIRealtimeAdapter` and asserts kwargs reach it) and two `OpenAIRealtime engine wrapper → OpenAIRealtimeAdapter forwarding` cases in TS (forwards-when-set + omits-when-unset).

- **OpenAI Realtime: post-barge-in re-greeting and mid-sentence fragments** (Python + TypeScript parity). After a barge-in, the agent could re-greet (`"Hello! How can I assist you today?"`) or resume mid-sentence (`"experiencing? This will help me assist you faster."`), losing the conversational thread. Root cause: `cancel_response` (Py `libraries/python/getpatter/providers/openai_realtime.py`, TS `libraries/typescript/src/providers/openai-realtime.ts`) sent `conversation.item.truncate { audio_end_ms }` using the **byte-derived counter of audio bytes received from the server**, not what the caller actually heard. OpenAI streams audio at 5-10x real-time, so when the consumer dropped its playout buffer on barge-in (`audio_sender.send_clear`), OpenAI was told the user heard the *full* generated text — leaving phantom assistant transcript on the conversation that the model replayed/resumed on the next turn. Fix bounds `audio_end_ms` by wall-clock elapsed time since the first `response.audio.delta` of the in-flight item (`time.monotonic()` Py / `Date.now()` TS), capped at the byte-derived total. Per-response state (`item_id`, `audio_ms`, `first_audio_at`) is also now reset by `cancel_response` itself so post-cancel late frames don't leak into the next turn. New `@pytest.mark.mocked` regression `test_cancel_response_caps_audio_end_ms_to_wallclock` in `libraries/python/tests/unit/test_providers_unit.py` and `[mocked]` regression in `libraries/typescript/tests/openai-realtime.test.ts` simulating 2 s generated / 30 ms played and asserting `audio_end_ms <= 200 ms`.

### Changed

- **`CircuitBreakerOptions.cooldown_s` → `cooldown_ms`** (Python + TypeScript parity, backward-compatible). The Python-side circuit-breaker tunable was named `cooldown_s` (seconds, default `30.0`) while TypeScript exposed `cooldownMs` (milliseconds, default `30_000`). Same number, different magnitude — a user copy-pasting `cooldown_s=30000` between SDKs would have set the cooldown to 8.3 hours instead of 30 seconds, leaving a per-tool circuit OPEN for the rest of every call. Aligned the Python field to milliseconds (`cooldown_ms`, default `30_000`) so it matches TS and the broader Patter time-field convention (`silence_duration_ms`, `prefix_padding_ms`, `after_ms`, ...). The legacy `cooldown_s=` kwarg is still accepted with a one-shot `DeprecationWarning` and is converted to `cooldown_ms` internally; an explicit `cooldown_ms` always wins. Scheduled for removal in v0.7.0. New `time_until_half_open_ms()` method added on `CircuitBreakerRegistry` for direct TS-parity callers; the existing `time_until_half_open()` keeps returning seconds so downstream code (notably `ToolExecutor` populating the `retry_after_ms` field on the rejection JSON) is unchanged at the wire level. Files touched: `libraries/python/getpatter/tools/circuit_breaker.py`, `libraries/python/getpatter/tools/tool_executor.py`, `libraries/python/tests/unit/test_circuit_breaker.py`. 4 new regression tests cover (a) the deprecation warning fires with the expected message, (b) the seconds-shim produces identical breaker behaviour to the ms field, (c) explicit `cooldown_ms` wins when both kwargs are passed, (d) defaults match TypeScript byte-for-byte (`30_000` ms, threshold `5`).

- **Pricing tables made model-aware across STT, TTS, and Realtime** (Python + TypeScript parity, fully backward-compatible). Previously every call billed at the **provider-level** rate regardless of which model the agent actually used: a Patter user on Deepgram Nova-3 multilingual was billed at the Nova-3 monolingual price, and a user on `gpt-realtime-2` was billed at the `gpt-realtime-mini` rate (4x undercharge on audio output). LLM cost was already model-aware via `LLM_PRICING`; this change extends the same pattern to the rest of the cost surface.
  - **Schema**: each `DEFAULT_PRICING` entry now carries provider-level defaults *plus* an optional `models` map keyed by model identifier with per-model overrides. New helper `_resolve_provider_rates(config, model)` (Py) / `resolveProviderRates(config, model)` (TS) merges model-specific rates on top of provider defaults — with longest-prefix fallback so versioned IDs like `gpt-realtime-2-2026-05-08` resolve against `gpt-realtime-2`, mirroring the existing LLM cost-calc logic.
  - **Cost-calc signatures**: `calculate_stt_cost`, `calculate_tts_cost`, `calculate_realtime_cost`, `calculate_realtime_cached_savings` (Py) and the TS equivalents now accept an optional trailing `model` arg. Default `None` preserves legacy behaviour (provider-level rate). All five functions are backward-compatible — existing callers compile and run unchanged.
  - **Auto-threading**: `CallMetricsAccumulator` now stores `stt_model` / `tts_model` / `realtime_model` (Py) and `sttModel` / `ttsModel` / `realtimeModel` (TS), populated by the stream-handler factory from the agent's adapter `model` attribute (`agent.stt.model`, `agent.tts.model`, `agent.model` for realtime). On every `record_realtime_usage(usage)` call the realtime model is also pulled from the `response.done` payload itself (per-turn), overriding the call-level default — supports mid-call model switches.
  - **`mergePricing` now overlays nested `models` shallowly**: a user override of `{"deepgram": {"models": {"nova-2": {"price": 0.01}}}}` keeps every other Deepgram model rate intact instead of replacing the entire `models` dict. Same shape for both SDKs.
  - **Built-in model rates added** for the major providers (no SDK source change required to bill them):
    - **Deepgram**: `nova-3` ($0.0077/min, default), `nova-3-multilingual` ($0.0092), `nova-2` ($0.0058), `nova` ($0.0043), `whisper-large` / `whisper-medium` ($0.0048).
    - **OpenAI Whisper / Transcribe**: `whisper-1` ($0.006/min), `gpt-4o-transcribe` ($0.006), `gpt-4o-mini-transcribe` ($0.003), `gpt-realtime-whisper` ($0.017). New provider key `openai_transcribe` exposed for the standalone REST endpoint.
    - **ElevenLabs (REST + WebSocket)**: `eleven_flash_v2_5` ($0.06/1k chars, default), `eleven_turbo_v2_5` ($0.05), `eleven_multilingual_v2` / `eleven_monolingual_v1` ($0.18), `eleven_v3` ($0.30).
    - **OpenAI TTS**: `tts-1` ($0.015/1k, default), `tts-1-hd` ($0.030), `gpt-4o-mini-tts` ($0.012).
    - **Cartesia TTS**: `sonic-1` / `sonic-2` / `sonic-english` / `sonic-multilingual` ($0.030).
    - **Rime**: `mistv2` / `mist` ($0.030), `arcana` ($0.040).
    - **LMNT**: `aurora` / `blizzard` ($0.050).
    - **Inworld**: `inworld-tts-2` ($0.020, default), `inworld-tts-1.5-max` / `inworld-tts-1.5` ($0.025).
    - **OpenAI Realtime**: provider defaults match the Patter default `gpt-realtime-mini` ($10/$20 per M audio in/out, $0.60/$2.40 per M text in/out, $0.30/$0.06 per M cached audio/text). Per-model overrides under `openai_realtime.models`:
      - `gpt-realtime` (GA, Aug 2025): $32/$64 audio, $4/$16 text, $0.40/M cached.
      - `gpt-realtime-2` (most-capable): $32/$64 audio, $4/$24 text, $0.40/M cached.
      - `gpt-realtime-mini` / `gpt-4o-mini-realtime-preview`: $10/$20 audio, $0.60/$2.40 text, $0.30/$0.06 cached.
      - `gpt-4o-realtime-preview`: $100/$200 audio, $5/$20 text (~10x mini).
  - **Removed standalone `openai_realtime_2` entry** introduced earlier in this Unreleased cycle — the per-model rate now lives under `openai_realtime.models["gpt-realtime-2"]` and is picked up automatically. The previous workaround (`Patter(pricing={"openai_realtime": DEFAULT_PRICING["openai_realtime_2"]})`) is no longer needed; just construct the agent with `model=OpenAIRealtimeModel.GPT_REALTIME_2` and the dashboard bills correctly.
  - **PRICING_VERSION bumped 2026.2 → 2026.3, PRICING_LAST_UPDATED 2026-05-08**.
  - **Tests**: 16 new Python tests + 16 new TypeScript tests covering the new helper, model-aware lookup, longest-prefix fallback, override merge semantics (single-model overlay leaves siblings intact), and end-to-end auto-resolution from `record_realtime_usage(usage)`. Full suite Py 1656 / TS 1321 green.

### Added

- **TypeScript SDK: `manageWebhook` opt-out for `serve()`** (TS only — closes a parity gap with Python which never auto-configured carriers). New `manageWebhook?: boolean` option on `ServeOptions`, default `true` (preserves existing behaviour byte-for-byte). When set to `false`, `serve()` skips the call to `autoConfigureCarrier` so the carrier's webhook URL (Twilio `voice_url`) is left untouched. Required when the webhook is managed externally (Terraform, an edge gateway, a voice-router function in front of the agent) — otherwise every container boot silently overwrites the externally-managed value, bypassing whichever gating layer fronts the SDK. `tunnel: true` overrides the opt-out — the tunnel hostname is dynamic and only known at runtime, so the carrier MUST be reconfigured. Files: `libraries/typescript/src/types.ts` (new `manageWebhook?: boolean` field with full doc comment on `ServeOptions`), `libraries/typescript/src/client.ts` (gated `autoConfigureCarrier` on `opts.manageWebhook !== false || wantsCloudflared`). 3 new unit tests under `serve() > manageWebhook opt-out` in `libraries/typescript/tests/unit/client.test.ts` swap `globalThis.fetch` to capture Twilio API calls and assert: default → API hit, `manageWebhook: true` → API hit, `manageWebhook: false` → API NOT hit. Doc updated in `docs/typescript-sdk/local-mode.mdx`.

- **TypeScript SDK now ships `SpeechmaticsSTT`**, fixing a long-standing Python-only gap. The TS adapter speaks Speechmatics's RT v2 WebSocket protocol directly via `ws` (no upstream Node SDK exists) while exposing the same options as the Python adapter — `language`, `turnDetectionMode`, `sampleRate`, `enableDiarization`, `maxDelay`, `endOfUtteranceSilenceTrigger` / `endOfUtteranceMaxDelay`, `includePartials`, `additionalVocab`, `operatingPoint`, `domain`, `outputLocale`. Pro-tier pricing $0.004/min already in `pricing.ts`. Multilingual + accented speech support. New files `libraries/typescript/src/providers/speechmatics-stt.ts` (low-level adapter) and `libraries/typescript/src/stt/speechmatics.ts` (pipeline-mode wrapper with `providerKey = "speechmatics"` and `SPEECHMATICS_API_KEY` env fallback). Re-exported as `SpeechmaticsSTT` from the package root, mirroring `from getpatter import SpeechmaticsSTT` on Python. The legacy `speechmatics()` config helper in `libraries/typescript/src/providers.ts` no longer throws — it now returns a real `STTConfig` like its Python sibling. 21-test mocked suite (`libraries/typescript/tests/speechmatics-stt.mocked.test.ts`) covers connect handshake, StartRecognition payload shape, partial vs final transcript translation, error-frame propagation, EndOfStream close path with `last_seq_no`, and pipeline-wrapper env-var resolution.

- **Python parity for `ConversationStateSnapshot`, `UserState`, `AgentState`, `EouTrigger`** (Python catches up to TypeScript — additive, no breaking changes). The four speech-edge types that the TypeScript SDK already exposed at the package root (`libraries/typescript/src/index.ts`) now exist as importable Python symbols too: `from getpatter import UserState, AgentState, EouTrigger, ConversationStateSnapshot`. `UserState` / `AgentState` / `EouTrigger` land as `StrEnum` (consistent with `OpenAIRealtimeAudioFormat` and the rest of the provider enums) so callers get autocomplete and `==` comparisons against bare strings keep working; `ConversationStateSnapshot` is a `@dataclass(frozen=True)` per the immutability rule, mirroring the TS `readonly interface`. `SpeechEvents.conversation_state_snapshot` returns the typed snapshot for new code, while the legacy `conversation_state` property still returns `dict[str, str]` for backward compatibility. 5 new unit tests in `libraries/python/tests/test_speech_events.py` confirm value-set equality with the TypeScript unions, frozen-dataclass immutability, and that all four names import from the package root. Closes the parity gap flagged in `.claude/rules/sdk-parity.md`. Files touched: `libraries/python/getpatter/_speech_events.py`, `libraries/python/getpatter/__init__.py`, `libraries/python/tests/test_speech_events.py`.

- **OpenAI Realtime: support for `gpt-realtime-2` and `gpt-realtime-whisper`** (Python + TypeScript parity, opt-in, fully backward-compatible). New model identifiers in the public enums:
  - `OpenAIRealtimeModel.GPT_REALTIME_2` (`"gpt-realtime-2"`) — OpenAI's most-capable realtime voice model: speech-to-speech with stronger instruction following, configurable reasoning effort, and a 128K context window. Accepts the same v1 `session.update` wire shape as `gpt-realtime`/`-mini` so it slots into the existing adapter without protocol changes; pass `model=OpenAIRealtimeModel.GPT_REALTIME_2` to `Patter.agent(...)`. Billing is auto-resolved via the model-aware pricing refactor (see Changed) — no manual override required.
  - `OpenAITranscriptionModel.GPT_REALTIME_WHISPER` (`"gpt-realtime-whisper"`) — streaming-optimised Whisper variant for low-latency transcript deltas inside a Realtime session. Use as `input_audio_transcription_model` (Py) / `inputAudioTranscriptionModel` (TS) when you want faster partials than `whisper-1` at lower cost than `gpt-4o-transcribe`.
  - New `reasoning_effort` (Py) / `reasoningEffort` (TS) constructor option on `OpenAIRealtimeAdapter`: `"minimal" | "low" | "medium" | "high"`. When set, Patter injects `session.reasoning = { effort: ... }` into the `session.update` payload. OpenAI recommends `"low"` for production voice flows — higher tiers add measurable per-turn latency. Default is unset (server default applies); has no effect on models that ignore the field.

  `gpt-realtime-translate` is intentionally **not** wired into the Realtime adapter: it lives on a different endpoint (`/v1/realtime/translations`), does not support tool calling / `response.create` / agent system prompts, and would invalidate the `Agent`-shaped contract Patter exposes. If we add real-time translation it will land as a separate feature (e.g. `Patter.translate_bridge(...)`), not as a variant of the conversational Realtime mode.

  Defaults unchanged (`gpt-realtime-mini`, `whisper-1`). 7 unit tests Py + 4 unit tests TS covering enum exposure, constructor option storage, `reasoning.effort` wire-format injection (and its absence when unset). Files touched: `libraries/python/getpatter/providers/openai_realtime.py`, `libraries/typescript/src/providers/openai-realtime.ts`, plus the corresponding test files.

- **Speech-edge events for turn-taking instrumentation** (Python + TypeScript parity, additive — no breaking changes). Patter now exposes seven optional async callbacks on every `Patter` instance plus a read-only `conversation_state` (Py) / `conversationState` (TS) snapshot, mirroring the public APIs of LiveKit Agents (`user_state_changed`, `agent_state_changed`, `user_turn_completed`, `user_interruption_detected`), Pipecat (`VADUserStartedSpeakingFrame`, `BotStartedSpeakingFrame`, `LLMFullResponseStartFrame`, `OutputAudioRawFrame`, `InterruptionFrame`) and OpenAI Realtime (`input_audio_buffer.speech_started/_stopped/_committed`). The seven events: `on_user_speech_started` (raw VAD positive edge), `on_user_speech_ended` (raw VAD trailing edge — *not* end-of-utterance), `on_user_speech_eos` (committed EOU — VAD edge + trailing silence + optional semantic turn-detector agreement; the canonical "user finished" signal that anchors `eos_to_first_token_ms`), `on_agent_speech_started` (first wire-time chunk of the agent turn — what the user actually hears, distinct from TTS warmup), `on_agent_speech_ended` (last wire chunk; payload includes `interrupted: bool` for barge-in), `on_llm_token` (TTFT marker, fires once per turn on the first LLM token), `on_audio_out` (first TTS audio chunk per turn — TTS warmup, distinct from wire-time). Each event also records an OpenTelemetry span event on the current call span (`patter.event.user_speech_started`, …, `patter.event.llm_first_token` carrying `gen_ai.request.model` + `gen_ai.provider.name` per the OTel GenAI semconv) when `PATTER_OTEL_ENABLED=1` and the `opentelemetry` peer dep is installed; otherwise the OTel branch is a zero-cost no-op. The dispatcher is callback-safe — observer exceptions are caught and logged, never propagated to the live call. State machine tracks per-side `conversation_state` (`user`: `listening`/`speaking`/`thinking`/`away`, `agent`: `initializing`/`idle`/`listening`/`thinking`/`speaking`) and a monotonically-increasing `turn_idx` that increments on every committed EOU. Wired into the realtime stream handler so `user_speech_started/_ended/_eos` and `agent_speech_started/_ended` fire automatically on the OpenAI Realtime + Twilio/Telnyx path; `on_llm_token` and `on_audio_out` are exposed on the dispatcher for adapter / pipeline-mode integrations to call. New files: `libraries/python/getpatter/_speech_events.py`, `libraries/typescript/src/_speech-events.ts`. Public exports: `SpeechEvents`, `SpeechEventCallback`, `ConversationStateSnapshot`, `UserState`, `AgentState`, `EouTrigger`. 16 unit tests Py + 15 unit tests TS covering every event payload, idempotency (LLM/audio fire-once-per-turn), state transitions, OTel attach contract, callback-exception isolation, chained-callback wrapping, and Patter-level proxy mirroring. Motivated by the `patter-agent-runner` acceptance suite which ships 15 turn-taking assertion verbs (barge-in latency, silence-gap, cross-talk, eos-to-first-token, MOS, WER) that previously auto-skipped because the SDK did not surface per-side speech edges.

- **Inworld TTS provider (`inworld-tts-2` + TTS-1.5 family)** (Python + TypeScript parity). New TTS adapter calling Inworld's HTTP NDJSON streaming endpoint `POST https://api.inworld.ai/tts/v1/voice:stream`. Default model is `inworld-tts-2` (sub-200 ms time-to-first-audio, 100+ languages with mid-utterance switching, natural-language voice steering); pass `model: "inworld-tts-1.5-max"` to fall back to the prior generation. Default audio output is `PCM` (PCM_S16LE) at 16 kHz so the result feeds straight into the Patter pipeline without transcoding. Public API: `import { InworldTTS } from "getpatter"` (TS) / `from getpatter import InworldTTS` (Py); pipeline-mode namespace `getpatter/tts/inworld` (TS) / `getpatter.tts.inworld` (Py) with env-var auto-resolve via `INWORLD_API_KEY`. Optional fields: `language` (BCP-47), `temperature` (TTS-1.5 only), `speakingRate` (0.5–1.5), `deliveryMode` (`EXPRESSIVE` / `BALANCED` / `STABLE` — TTS-2 only), `bitrate`. The Inworld dashboard issues a Base64 token already in the form expected by the `Authorization: Basic <token>` header — paste it as `INWORLD_API_KEY` directly. Pricing entry added to `pricing.ts` / `pricing.py` as `inworld` (placeholder $0.020 / 1k chars — verify against current platform tier). Optional dependency: `getpatter[inworld]` adds `aiohttp>=3.10`. New files: `libraries/typescript/src/providers/inworld-tts.ts`, `libraries/typescript/src/tts/inworld.ts`, `libraries/python/getpatter/providers/inworld_tts.py`, `libraries/python/getpatter/tts/inworld.py`. 7 unit tests per SDK covering payload shape, NDJSON parsing, base64 audio decoding, optional-field omission, env-var fallback, and non-200 error surfacing.

- **Tool streaming results via async generator handlers** (Python + TypeScript parity, Realtime-only). Tool handlers can now be `async function*` (TS) / `async def ... yield` (Py) generators that emit live progress updates while a long-running tool runs. Each `yield { progress: "..." }` (TS) / `yield {"progress": "..."}` (Py) is sent to the agent via `OpenAIRealtimeAdapter.sendText` so the caller hears status inline ("Searching the database...", "Found 12 matches..."). The generator's `return` value (TS) or final `{"result": "..."}` yield (Py — async generators don't surface return values cleanly) becomes the function-call result the model sees. Plain `async` handlers continue to work unchanged. Pipeline mode silently discards progress for now (no clean injection point — follow-up). 5 unit tests TS, 4 unit tests Py.

- **Reassurance auto-message during long tool calls** (Python + TypeScript parity, Realtime-only). New `Tool({..., reassurance: "Let me check..." })` (or `{ message, afterMs }`) bridges the silence on slow tool handlers/webhooks. After `afterMs` (default 1500 ms) the SDK enqueues the message via `OpenAIRealtimeAdapter.sendText` so the agent says it inline; if the tool returns earlier, the timer is cancelled and nothing is spoken. Improves the conversational UX when a handler runs >1 s. Pipeline mode silently skips for now (no clean injection point mid-turn yet — follow-up).

- **MCP (Model Context Protocol) client integration** (Python + TypeScript parity, MVP). New `agent({ mcpServers: [...] })` (TS) / `agent(..., mcp_servers=[...])` (Py) plugs a Patter agent into MCP servers (Google Workspace, PayPal, Postgres, GitHub, ...) without writing wrapper handlers. Each server is queried at call start via `tools/list`; discovered tools are wrapped with synthetic handlers that dispatch to `tools/call` and merged into `agent.tools` before the model sees the tool list. Two config forms: a URL string (shorthand for streamable-HTTP transport) or an explicit object with optional auth `headers` and a `name` for telemetry. Tool-name collisions with user-defined tools raise at startup. Connection lifecycle is per-call (one handshake at start, closed on call end). Optional dependency: `@modelcontextprotocol/sdk` (TS) / `mcp` (Py extra) — users who don't configure `mcpServers` never pay the install cost. Limitations of the MVP: streamable-HTTP only (no stdio/SSE fallback yet), no process-wide caching of `tools/list` (~50-200 ms × N servers per call). New file `tools/mcp-client.ts` (TS) / `tools/mcp_client.py` (Py).

- **Tool retry policy + per-tool circuit breaker** (Python + TypeScript parity). `DefaultToolExecutor` (TS `libraries/typescript/src/llm-loop.ts`) and `ToolExecutor` (Py `libraries/python/getpatter/tools/tool_executor.py`) now apply exponential-backoff retries (default 3 total attempts, base 500 ms × 2^attempt, jittered, capped at 5 s) to BOTH the local handler path and the webhook path — previously a single handler exception was a hard fault that silently killed the turn. New `CircuitBreakerRegistry` tracks per-tool consecutive failures; trips OPEN after 5 consecutive failures and stays OPEN for 30 s before allowing one HALF_OPEN probe. While OPEN, the executor returns `{error, fallback: true, circuit_state: "open", retry_after_ms}` immediately so the model can recover ("I couldn't reach the booking system, can I take your number?") instead of burning LLM tokens on retries that will keep failing. New unit tests: 10 per SDK for the breaker state machine, 14 per SDK for schema validation. Realtime mode also routes through the same executor (replaced inline ad-hoc dispatch in `stream-handler.ts:handleFunctionCall`) so its tool calls get the same robustness.

- **Tool JSON-schema validation at `agent()` build time** (Python + TypeScript parity). Patter now structurally validates every tool's `parameters` schema when you call `phone.agent({ tools: [...] })`: the root must be `type: "object"`, `properties` must be a dict, every `required` entry must exist in `properties`, etc. Typos that would have failed silently mid-call (e.g. `required: "name"` instead of `required: ["name"]`) raise `ToolSchemaError` immediately with a clear message naming the offending tool. New file `tools/schema-validation.ts` (TS) / `tools/schema_validation.py` (Py). 14 unit tests per SDK.
- **OpenAI strict mode opt-in for tools** (`Tool({ ..., strict: true })` TS / `Tool(..., strict=True)` Py). When set, Patter validates the schema satisfies OpenAI's strict-mode requirements (recursive `additionalProperties: false`, every property in `required`, no truly optional fields — use nullable types like `["string", "null"]` instead) at build time AND propagates `strict: true` in the OpenAI Realtime `session.update` wire format. The model is then constrained to emit arguments that exactly match the schema — no missing required fields, no extra properties, no type coercion. Default `false` for backward compatibility.

- **`Patter(persist=...)` option** for the dashboard's call history (Python + TypeScript parity). Previously the on-disk persistence layer (per-call `metadata.json` / `transcript.jsonl` / `events.jsonl` under `~/Library/Application Support/patter` or equivalent) was opt-in only via the `PATTER_LOG_DIR` env var, which was easy to miss when integrating Patter from a code-only setup. New explicit option in `LocalOptions` (TS) / `Patter()` constructor (Python):
  - omitted / `false` (default): no disk writes, in-memory ring buffer only — backward-compatible with prior behaviour.
  - `true`: write under the platform default location.
  - string: write under the supplied path.

  The env var still works as a deployment-time override and as the fallback when `persist` is undefined. When `persist` is set explicitly the env is ignored. `MetricsStore.hydrate(logRoot)` rebuilds the dashboard on startup so the call history survives process restarts without an external database. Retention defaults to 30 days (`PATTER_LOG_RETENTION_DAYS=0` to keep forever); phone numbers masked by default (`PATTER_LOG_REDACT_PHONE`).

### Changed

- **Dashboard rewritten as a Vite + React SPA** (Python + TypeScript parity, single source of truth). The dashboard UI previously lived as a multi-hundred-line vanilla template literal inside `libraries/typescript/src/dashboard/ui.ts` and `libraries/python/getpatter/dashboard/ui.py`; visual updates required hand-editing a string with no type safety, no componentisation, and no testability. The new dashboard lives at the repo root in a dedicated sub-project `dashboard-app/` (Vite + React 18 + TypeScript), bundled by `vite-plugin-singlefile` into one self-contained `dist/index.html` (~190 KB, JS + CSS inlined). `npm run sync` from `dashboard-app/` copies that file into both SDKs as `ui.html`, which the SDK then loads at runtime — Python via `importlib.resources`, TypeScript via `fs.readFileSync` next to the bundled module (tsup `shims: true` provides `__dirname` in ESM). End-user experience is unchanged: `phone.serve()` still serves the dashboard at `http://127.0.0.1:8000/` with zero CDN dependency, no extra build step, and identical routes. Internally the dashboard now consumes the existing `/api/dashboard/*` endpoints via React hooks (`useDashboardData`, `useTranscript`) and SSE for live updates, with a redesigned layout matching the new Patter design system (peach accent, dot-grid surfaces, JetBrains Mono for numerics, live-call right rail with per-call latency waterfall and cost breakdown). New files: `dashboard-app/` (~600 lines of TSX + ~500 lines of CSS, dev-only — never published). Updated files: `libraries/typescript/src/dashboard/ui.ts`, `libraries/typescript/src/dashboard/ui.html` (synced asset), `libraries/typescript/tsup.config.ts` (`shims: true`), `libraries/typescript/package.json` (asset copy in `build`, `files` array), `libraries/python/getpatter/dashboard/ui.py`, `libraries/python/getpatter/dashboard/ui.html` (synced asset), `libraries/python/pyproject.toml` (`package-data` includes `ui.html`).

### Fixed

- **Wire `on_llm_token` and `on_audio_out` callbacks for Realtime + ConvAI + Pipeline modes** (Python + TypeScript parity). The `SpeechEvents` dispatcher methods `fire_llm_first_token` (Py) / `fireLlmFirstToken` (TS) and `fire_audio_out` / `fireAudioOut` shipped in 0de4111 alongside the other five turn-taking edges, but no provider call site invoked them — the per-turn TTFT and TTS-warmup markers stayed silent for every backend. The `requires_sdk_callbacks` gates in the agent-to-agent acceptance suite that depend on these events (eos→first-token latency, audio-warmup vs wire-time skew) saw zero events and auto-skipped. Wired through new base-handler helpers `_emit_llm_first_token` / `_emit_audio_out` (Py) and `emitLlmFirstToken` / `emitAudioOut` (TS) called at: (a) Realtime `transcript_output` delta + `audio` delta — single `engine="openai_realtime"` tag, (b) ConvAI `transcript_output` + `audio` delta — `engine="elevenlabs_convai"` tag, (c) Pipeline LLM streaming first-token in `_process_streaming_response` (Py) / `runLLMLoop` (TS) — provider classified via `_infer_llm_provider` against the `agent.llm` class, (d) Pipeline TTS first-byte in `_synthesize_sentence` + `firstMessage` synth + WS-pipeline path — TTS provider classified via `_infer_tts_provider` (Py) or static `providerKey` (TS). Idempotent inside a turn — the dispatcher already guards on `_first_token_for_turn` / `_first_audio_for_turn` so per-delta calls are cheap. Files touched: `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/stream-handler.ts`, plus 2 new unit tests per SDK in `tests/test_speech_events.py` / `tests/speech-events.test.ts` covering Realtime + Pipeline wiring, idempotency-per-turn, and provider-tag classification.

- **Realtime mode: `Agent.first_message` was injected as user input, role-confusing the AI** (Python + TypeScript parity). The docstring states `first_message` is "what the AI says when the callee answers", but the implementation routed it through `OpenAIRealtimeAdapter.send_text` / `sendText`, which submits a `conversation.item.create` with `role: "user"`. The model then *replied* to its own greeting instead of *speaking* it. Symptoms ranged from harmless ("Hi! I'd be happy to help…" — model kept role) to breaking (a receptionist agent saying "Hi! I'd like to schedule a haircut for Friday afternoon" — model swapped role to customer because it interpreted the greeting as a customer cue). New `OpenAIRealtimeAdapter.send_first_message` / `sendFirstMessage` method that injects `role: "assistant"` so the AI continues from "having already said" the text. `StreamHandler` calls it via duck-typed lookup; older adapter builds without the method silently fall back to `send_text`. Surfaced by `inbound_twilio_realtime` in the agent-to-agent acceptance suite.

- **Cloudflared quick-tunnel: SDK notified port 8000 unconditionally**. Embedded usages where Patter co-tenants port 8000 with another HTTP server (notably the agent-to-agent test runner where the driver SDK and the dashboard ingest target share `127.0.0.1:8000`) saw `404 Not Found` access-log spam from `notify_dashboard` / `notifyDashboard` fire-and-forget POSTs. Send-side already swallows errors silently; the noise comes from the receiver's access log. New `PATTER_DASHBOARD_NOTIFY=0|false|no|off` env-var opt-out skips the POST entirely; default behaviour unchanged.

- **Realtime mode: handler-only tools were silently ignored** (TypeScript only — Python's `ToolExecutor` already dispatched both paths). `StreamHandler.handleFunctionCall` (TS `libraries/typescript/src/stream-handler.ts`) only dispatched tools that had `webhookUrl`; tools with an in-process `handler` callback fell through without sending `function_call_output` back to OpenAI Realtime, so the model hung waiting for a result and the conversation stalled. Now both `handler` and `webhookUrl` paths are supported, mirroring the pipeline-mode `LLMLoop.DefaultToolExecutor` and the existing Python behaviour. The `agent.tools` validator already requires exactly one of the two so the API surface is unchanged.
- **Realtime mode: assistant turns were not surfaced to `onTranscript`** (Python + TypeScript parity). `onAdapterResponseDone` (TS) / `_forward_events` `response_done` branch (Py) pushed the assistant text into `history` but never invoked the user-supplied `onTranscript({ role: 'assistant', ... })` callback, so demos and host applications that relied on the callback for live logging only saw `[user]` lines and never `[assistant]`. Added the callback fire alongside the history push.
- **Realtime mode: dashboard transcript shown out of order** (Python + TypeScript parity). OpenAI Realtime emits `input_audio_transcription.completed` (user transcript) AFTER `response.done` (assistant text) because Whisper transcription runs in parallel with — and slower than — the model response. The naïve push order was `[firstMessage, assistant₁, user₁, assistant₂, user₂, …]` which renders as "all assistant on top, all user below" in the dashboard. Added a per-call buffer (`pendingAssistantTurn` / `_pending_assistant_turn` + 3 s fallback timer) that holds the assistant turn until the matching user transcript arrives, so the rendered order is the natural `[user₁, assistant₁, user₂, assistant₂, …]`. Falls through gracefully if the user transcript never arrives (silence misclassified, transcription disabled, or call ends mid-turn).
- **Realtime mode: tool invocations were invisible in the transcript timeline** (Python + TypeScript parity). Tool calls (built-in `transfer_call`/`end_call` and user-defined) executed correctly when wired but were never recorded in `history` nor surfaced via `onTranscript`. The dashboard transcript and any host-application logging would skip them, making post-mortem analysis (and live observability) much harder. Added `emitToolEvent` (TS) / `_emit_tool_event` (Py) which pushes a `role: 'tool'` history entry with the rendered `name(args) → result` and fires `onTranscript({ role: 'tool', tool_name, tool_args, tool_result, ... })`. Both the invocation and the result are emitted so the timeline shows full call/return semantics. Dashboard CSS adds dedicated styling for `role=tool` (orange-bordered monospace card) and `role=system` (centered italic notice).
- **Cloudflared quick-tunnel WSS upgrade race**. Bumped `waitForTunnelPubliclyReachable` grace window from 2.5 s to 5 s (TS `libraries/typescript/src/client.ts`, Py `libraries/python/getpatter/client.py:_wait_for_tunnel_publicly_reachable`). Twilio's media-stream WSS upgrade goes through a different cloudflared edge route than HTTP and is ~1-3 s slower to propagate; the prior 2.5 s grace covered HTTP only and ~5 % of first calls dropped silently at pickup with no media (HTTP webhooks fired, AMD fired, but the inbound media WS never opened). 5 s covers both paths and drops the failure rate to <1 %. Adds 2.5 s to startup on first tunnel creation. Affects only dev/acceptance with cloudflared quick-tunnels (production deployments use direct webhook URLs).

## 0.6.0 (2026-05-03)

Repository cleanup + bug-fix + parity wave.

### Added — Acoustic echo cancellation (opt-in)

- New `getpatter.audio.aec.NlmsEchoCanceller` (Py) / `src/audio/aec.ts → NlmsEchoCanceller` (TS) — time-domain NLMS adaptive filter with Geigel double-talk detection. Subtracts the agent's TTS bleed from the inbound mic stream before VAD/STT see it, fixing the speakerphone barge-in fragility where VAD stays in "speaking" state because of the bleed and only fires during natural TTS pauses.
- New `Agent.echo_cancellation: bool` (Py) / `AgentOptions.echoCancellation?: boolean` (TS). **Default false** — handset / headset deployments don't have bleed. Set to `true` for speakerphone / tunnel-loop deployments.
- Algorithm: 512-tap NLMS at 16 kHz (≈32 ms history — appropriate for cellular / VoIP echo paths after the carrier's own AGC has trimmed the tail) with **adaptive step size**: 0.5 during a 0.5 s warm-up window, decaying to the textbook 0.1 for steady-state tracking. Geigel rho 0.6 freezes adaptation during double-talk so the larger warm-up step does not pull caller voice into the echo model.
- Convergence: ≥10 dB ERLE within the **first 250 ms** of TTS playback on broadband audio (regression-tested). Earlier 2048-tap + constant-step prototype showed 8–12 s convergence on real cellular calls — the user's first turn was lost. NOT a drop-in replacement for production-grade AEC3 — wrap a binding to `webrtc-audio-processing-2` externally if you need that quality.
- Wired into `PipelineStreamHandler.start()` (Py) / `StreamHandler.initPipeline` (TS). Far-end tap fires on every TTS chunk before the carrier transcode; near-end runs in `on_audio_received` / inbound `media` path before VAD.
- 9 unit tests per SDK covering: warm-up convergence (first 250 ms ≥10 dB ERLE), steady-state convergence (1 s ≥10 dB ERLE), double-talk preservation (≥50 % near-speech power), construction validation (taps / step / warmup_step / warmup_seconds / leakage / sample rate), pass-through before priming, reset, empty buffers.

### Improved — SileroVAD usability

- **Auto-VAD in pipeline mode**. When the user does not pass `agent.vad`, the pipeline stream handler now auto-loads `SileroVAD` with telephony-tuned defaults (1.0 s `min_silence_duration`, 16 kHz sample rate). Falls back silently to the legacy STT-endpoint heuristic when `onnxruntime-node` (TS) / `getpatter[silero]` (Py) is not installed. No opt-out flag — auto-VAD is a strict upgrade over the heuristic when available.
- **`SileroVAD.forPhoneCall(opts?)` / `SileroVAD.for_phone_call(**overrides)`** factory. Convenience wrapper around `load(...)` that pre-applies the telephony preset; pass overrides for noisy-environment tuning (e.g. `minSilenceDuration: 1.5` for tunnel + speakerphone echo).
- **Defaults aligned with upstream Silero** (`snakers4/silero-vad`): `min_speech_duration` 0.05 → 0.25 s, `min_silence_duration` 0.55 → 0.10 s, `prefix_padding` 0.5 → 0.03 s. Existing callers can restore the old telephony-tuned values explicitly. The new `for_phone_call` factory handles the common phone-call override.
- **Robust ONNX model resolution (TS)**. `silero-vad.ts` now probes 4 anchor candidates (`__dirname`, `import.meta.url`, `createRequire(import.meta.url).resolve("getpatter/package.json")`, `createRequire(cwd).resolve(...)`) crossed with 3 path shapes (`<dir>/resources/`, `<dir>/../resources/`, `<dir>/dist/resources/`). Eliminates the workaround `createRequire(import.meta.url).resolve("getpatter")` that callers had to add manually under bundlers that break `import.meta.url`. Most changes are internal hardening; the breaking changes are the package-tree reorganisation and the `Agent.provider` field type tightening.

### Breaking — package-tree reorganisation

- **SDK roots moved** from `sdk-py/` and `sdk-ts/` to `libraries/python/` and `libraries/typescript/` (mcp-use-style layout). Top-level imports for end users are unchanged (`pip install getpatter`, `npm install getpatter`, `import { Patter } from "getpatter"`). Repo-relative paths in CI scripts, contributing docs, and integrators that pull from the source tree must update.
- **Per-SDK tests live in `libraries/<lang>/tests/`** instead of inside the package directory. Cross-SDK integration tests at the repo root were removed; integration coverage now lives next to each SDK.
- **In-repo `examples/` directory removed.** Examples are maintained in separate downstream repos and pulled into the docs as needed.

### Breaking — internal SDK layout reorganised

Internal layout reorganised for both SDKs; **PUBLIC API surface unchanged** (every `from getpatter import ...` / `import { ... } from "getpatter"` keeps resolving). Affects only callers that import internal modules directly (e.g. `from getpatter.handlers.twilio_handler import ...` — that path no longer exists).

- **Python (`libraries/python/getpatter/`)**
  - `handlers/twilio_handler.py` → `telephony/twilio.py`
  - `handlers/telnyx_handler.py` → `telephony/telnyx.py`
  - `handlers/common.py` → `telephony/common.py`
  - `handlers/stream_handler.py` → `stream_handler.py` (top-level — it's the per-call orchestrator, not a telephony adapter; `handlers/` folder removed)
  - `services/transcoding.py`, `services/pcm_mixer.py`, `services/background_audio.py` → `audio/*.py`
  - `services/tool_decorator.py`, `services/tool_executor.py` → `tools/*.py`
  - All other `services/*.py` (llm_loop, metrics, sentence_chunker, text_transforms, ivr, fallback_provider, pipeline_hooks, chat_context, call_log, remote_message) stay where they are.

- **TypeScript (`libraries/typescript/src/`)**
  - `carriers/twilio.ts` → `telephony/twilio.ts`
  - `carriers/telnyx.ts` → `telephony/telnyx.ts`
  - top-level `transcoding.ts` → `audio/transcoding.ts`
  - `services/background-audio.ts` → `audio/background-audio.ts`
  - top-level `tool-decorator.ts` → `tools/tool-decorator.ts`

The `tts/` and `stt/` namespaces are unchanged — they expose `getpatter.{tts,stt}.<provider>.{TTS,STT}` with env-var auto-resolve and are real public API.

Migration: if your code did `from getpatter.handlers.twilio_handler import ...` change it to `from getpatter.telephony.twilio import ...`. Public exports from the package root (e.g. `from getpatter import Patter`) are unaffected.

### Breaking — `Agent.provider` is a closed enum

- `Agent.provider` (Python) is now typed `ProviderMode = Literal["openai_realtime", "elevenlabs_convai", "pipeline"]`; previously it was a free-form `str`. TypeScript `AgentOptions.provider` mirrors with the same union.
- Callers passing arbitrary strings (e.g. typo `"openai-realtime"`) get a static type error in TS and a run-time `PatterConfigError` in Py.

### Added — phone preamble (opt-in by default for phone mode)

- `Agent` gets an automatic preamble prepended to `system_prompt` for phone-call deployments: instructs the model to respond concisely, drop markdown/bullets/emojis, and spell out numbers/currencies/dates in spoken form. Set `disable_phone_preamble=True` (Py) / `disablePhonePreamble: true` (TS) to opt out.

### Added — per-SDK `.env.example`

- `libraries/python/.env.example` and `libraries/typescript/.env.example` ship a curated, role-grouped list of provider env vars (telephony, LLM, STT, TTS). Replaces the obsolete cloud-mode env file.

### Added — `PricingUnit` enum

- `pricing.py` / `pricing.ts` introduce a `PricingUnit` enum (`MINUTE`, `THOUSAND_CHARS`, `TOKEN`) replacing free-form unit strings on every `ProviderPricing` row. The TS server backward-compat shim still accepts plain strings on the wire.

### Added — provider enum constants

- Every provider that historically accepted hard-coded model / voice / format strings now exposes a typed enum (`StrEnum` / `IntEnum` in Py, const-object + value union in TS). Covered providers: assemblyai_stt, cartesia_stt, cartesia_tts, cerebras_llm, deepgram_stt, elevenlabs_tts, elevenlabs_ws_tts, gemini_live, google_llm, groq_llm, lmnt_tts, openai_realtime, openai_tts, rime_tts, silero_vad, silero_onnx, soniox_stt, speechmatics_stt, telnyx_stt, telnyx_tts, ultravox_realtime, whisper_stt, anthropic_llm, krisp_*. The string form is still accepted on each constructor (back-compat).

### Fixed — barge-in cancels in-flight LLM stream (HIGH IMPACT)

- The stream handler used to leak partial LLM completions during barge-in: even after the user interrupted, the LLM stream kept pulling tokens to completion (and racking up tokens/cost). Now Python uses an `asyncio.Event` checked between tokens; TypeScript uses an `AbortController` forwarded to the LLM stream `fetch`. Cancellation is observed inside one token cycle.

### Fixed — webhook + outbound URL hardening

- Python `server.py` adds a `validate_webhook_url()` SSRF guard on every outbound webhook (refuses `127.0.0.1` / `169.254.169.254` / private CIDR by default), matching the TypeScript validator that already existed.
- Python WS endpoint enforces `MAX_WS_PER_IP=10` connection cap (parity with TS).
- Python voicemail POST now has an explicit 10 s timeout (was unbounded).

### Fixed — observability & metrics

- Python `event_bus.py` now logs listener exceptions instead of swallowing them.
- Python `metrics.py` accepts a legitimate `0.0 ms` `agent_response_ms` value (was treated as "missing" via truthy check).
- TypeScript `metrics.ts` now emits `llm_ttfb_ms` / `stt_ttfb_ms` / `tts_ttfb_ms` events on the EventBus (Py parity).
- TypeScript `server.ts` `queryTelephonyCost` catch logs the failure instead of returning silently.
- TypeScript `llm-loop.ts` HTTP errors now `throw new PatterConnectionError(...)` (was: silent return masking provider 5xx).
- LLM loop cache token attribution: emit key is now `cache_read_tokens` (was `cache_read_input_tokens`, which didn't match what OpenAI / Google emit — Anthropic-style key was being read but never populated).

### Performance

- `text_transforms.py` precompiles all 14 markdown regex patterns + 2 emoji helpers as module-level constants (was: recompiled per call). Hot-path text transforms drop CPU by ~30% on long responses.
- `ElevenLabsWebSocketTTS` auto-flips `output_format` to `ulaw_8000` when paired with a Twilio carrier — eliminates the SDK-side mulaw transcode hop. New `set_telephony_carrier()` / `setTelephonyCarrier()` hook is duck-typed by the stream handler at call setup; explicit `output_format` always wins.
- `OpenAITTS` gains an opt-in `target_sample_rate=8000` (Py) / `targetSampleRate: 8000` (TS) constructor arg that collapses the 24k→16k + 16k→8k chain into a single 3:1 decimation with a tighter LPF (Nyquist ≈ 4 kHz). New `create_resampler_24k_to_8k()` / `createResampler24kTo8k()` factory exported from `transcoding`. Default stays at 16 kHz for backward compatibility.

### Improved — sentence chunker (Bugs #48 + #49)

- **Per-language honorifics**: new `HONORIFICS_{EN,IT,ES,DE,FR,PT}` constants merge into a union regex that prevents premature splits on `Mr.`, `Sig.`, `Sr.`, `Hr.`, `M.`, `Sra.`, `Dr.`, `Dott.`, `Prof.`, `Mme.`, etc. Aggregation is union-of-all regardless of caller `language` — mixed-language deployments are common, safer default.
- **Single-word flush**: `DEFAULT_MIN_WORDS_FOR_SHORT_FLUSH` lowered `2 → 1`; single-word replies like `"Yes."`, `"Done!"`, `"Sì."`, `"はい。"` now flush on the terminator. New gate #6 in `maybeShortFlush` blocks flushing when the trailing word is a known honorific (so `"Mr."` doesn't escape as a sentence). Pass `minWordsForShortFlush=2` to opt back into the previous behaviour.
- 22 Python + 21 TypeScript new honorific cases; 12 + 12 single-word flush cases.

### Added — `ErrorCode` enum on the exception taxonomy

- New `ErrorCode` enum (10 stable values: `CONFIG`, `CONNECTION`, `AUTH`, `TIMEOUT`, `RATE_LIMIT`, `WEBHOOK_VERIFICATION`, `INPUT_VALIDATION`, `PROVIDER_ERROR`, `PROVISION`, `INTERNAL`) attached to every Patter exception via a default `.code` class attribute. Catchers can branch on `exc.code is ErrorCode.AUTH` instead of class-name matching.
- Python: `StrEnum` on `exceptions.py`, optional `code=` kwarg on `PatterError.__init__` for per-instance override.
- TypeScript: const-object + value-union, optional `{ code }` constructor option on every subclass. Class→code mapping matches Python byte-for-byte (asserted in test parity).
- Backward-compatible: every existing `Foo("msg")` / `new Foo("msg")` call site keeps working.

### Documentation

- **Docstring / JSDoc sweep across both SDKs.** Every public module, class, function, method, interface, and exported type now has a description. Pre-existing docstrings were left untouched; the pass added ~75 Python docstrings and ~290 TypeScript JSDoc blocks across 100+ files. No behaviour changes.

### Cleanup

- All competitor license headers (LiveKit, Pipecat, Apache, etc.) removed from source files. New rule `.claude/rules/no-competitor-references.md` codifies the policy.
- Root `LICENSE` updated to `Copyright (c) 2026 Patter Contributors`.
- `Dockerfile` + `docker-compose.yml` simplified; non-public-repo scripts removed.
- `playwright.config.ts` + `@playwright/test` devDep dropped (E2E lives in downstream test repo).

## 0.5.5 (2026-04-28)

Latency-pass 1: TTFA optimisations grounded in the ElevenLabs latency posts and a head-to-head review of competing production voice-AI stacks. All changes are additive or opt-in — existing call sites keep their current behaviour unchanged.

### Improved — sentence chunker

- **Italian abbreviations** added to the prefix list (Sig, Sgr, Dott, Prof, Avv, Ing, Geom, Rag, Arch, On, Egr, Spett, Gent, Ill) and the suffix list (ecc, cit, cap, sez, art, pag, fig, tab, cfr, vol, ed). Sentences like _"Ho incontrato il Sig. Rossi alla riunione di stamattina."_ are no longer split on the abbreviation period.
- **English abbreviations** expanded with the Pipecat NLTK Punkt set: `Gen.`, `Sen.`, `Rep.`, `Lt.`, `Cpt.`, `Capt.`, `Col.`, `Cmdr.`, `Adm.`, `vs.`, `etc.`, `No.`, `Vol.`, `pp.`, `cf.`, `ca.`, `op.`, plus address forms `Mt.`, `Hwy.`, `Rt.`, `Pl.`, `Ave.`, `Blvd.`, `Sq.`. Phrases like _"Compare A vs. B"_ and _"Met Gen. Smith and Sen. Davis"_ no longer split mid-abbreviation.
- **Suffix + starter pattern preserves the period** (e.g. _"Patter Inc. He left."_ now keeps `Inc.` in the emitted sentence — previously dropped to `Inc`).
- **All-caps name flush fixed** (Pipecat issue #1692). Previously the gate-5 acronym guard blocked *any* uppercase-preceded period, so _"I was speaking with RAMESH."_ would sit in the buffer forever. Now only purely-uppercase ASCII words ≤3 chars (U, US, USA, NATO patterns) are treated as acronyms.
- **Multilingual terminator support**. The terminator set now includes ASCII semicolon `;`, Unicode ellipsis `…`, full-width semicolon `；`, full-width period `．`, half-width Japanese period `｡`, plus the Pipecat-derived non-Latin set: Hindi/Devanagari `। ॥`, Arabic `؟ ؛ ۔ ؏`, Armenian `։`, Ethiopic `። ፧`, Khmer `។ ៕`, Burmese `။`, Tibetan `༎ ༏`. Hindi text like _"यह हिन्दी का एक वाक्य है।"_ now flushes correctly.
- **Cross-SDK parity fixture** at `tests/parity/scenarios/sentence_chunker.json` — 61 cases covering EN/IT/CJK/Hindi/Arabic punctuation, decimals, abbreviations, currency, dates, ellipsis, JSON, lists, all-caps names. Standalone runner at `tests/parity/sentence_chunker_parity.py` verifies Python and TypeScript emit identical sentence streams (53 / 61 PASS, 8 documented quirks/regressions).

### Added — opt-in aggressive first-clause flush

- New `aggressiveFirstFlush` (TS) / `aggressive_first_flush` (Python) option on `Agent`/`AgentOptions`. **Default: false.** When enabled, the chunker emits the first clause of each response on a soft punctuation boundary (`,`, em-dash, en-dash) once ≥40 chars accumulate — saves 200–500 ms TTFA on the first sentence of each turn.
- **Eight guards** prevent regressions on the safe-but-aggressive path: minimum length, decimal-comma (`3,14`), currency (`$1,000`, `€1.000,50`), thousands-separator, balanced parens/brackets/braces/quotes (protects JSON), ellipsis (`...`, `…`), comma-before-quote, sub-token ambiguity (requires post-terminator char).
- **Italian language hard-disables** the feature regardless of caller preference (decimal-comma + dot-thousands inversion would split mid-number). Pass `language: "it"` and the flag is ignored.
- ElevenLabs `optimize_streaming_latency` is **deprecated** by ElevenLabs and is **not** added — the help-centre note flagged during research.

### Added — `after_llm` 3-tier hook API

- New shape: `afterLlm: { onChunk, onSentence, onResponse }` (TS) / `after_llm = AfterLLMHook(...)` (Python). All three methods optional, all sync-or-async.
  - **`onChunk` (tier 1)** — per-token sync transform (~0 ms budget). Use for regex replace, markdown strip, profanity char-swap. Does NOT block streaming.
  - **`onSentence` (tier 2)** — per-sentence rewrite (~50–300 ms). Runs between the chunker and TTS. Returning `null` keeps original; returning `""` drops the sentence silently. Use for PII redaction, persona overlay, refusal swap.
  - **`onResponse` (tier 3)** — per-full-response rewrite. Buffers tokens, blocks streaming TTS. Use only when sentence-level rewrite is insufficient.
- **Backward-compatible adapter**: the legacy `(text, ctx) => string` callable still works and is mapped to `onResponse`. A one-shot `PatterDeprecationWarning` (subclass of both `DeprecationWarning` and `UserWarning` so it surfaces by default in Python) is emitted on first construction. Scheduled removal: 0.7.0.
- The LLM loop now buffers only when tier-3 `onResponse` is configured; tier-1 `onChunk` is applied inline before yielding (zero latency cost), tier-2 `onSentence` runs in the stream handler between the chunker and TTS.

### Added — `ElevenLabsWebSocketTTS`

- New opt-in TTS class targeting the ElevenLabs `stream-input` WebSocket endpoint (`wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input`). Saves the per-utterance HTTP request setup time (~50 ms) and avoids the HTTP cold-start TLS handshake on bursty calls.
- **Drop-in API** matching `ElevenLabsTTS`: same `synthesize()` / `synthesizeStream()` signature, same `for_twilio()` / `for_telnyx()` factories, same default model (`eleven_flash_v2_5`).
- `auto_mode=true` by default — ElevenLabs handles internal chunk scheduling. Pass `auto_mode=False` + `chunk_length_schedule=[120, 160, 250, 290]` to take manual control.
- `inactivity_timeout=60 s` (default) — extends the WS keep-alive past the 20 s default to cover tool-call latency.
- `eleven_v3` is **rejected** at construction with a clear error (the WS endpoint does not support v3 — use the HTTP class).
- The HTTP `ElevenLabsTTS` class is **untouched** — both options coexist and the user picks per-call.

### Test coverage

- 31 new unit tests (20 Python + 11 TypeScript) for `ElevenLabsWebSocketTTS`.
- 22 new unit tests (11 Python + 11 TypeScript) for `aggressiveFirstFlush`.
- 20 new unit tests (11 Python + 9 TypeScript) for the `after_llm` 3-tier API.
- Parity fixture: 53 / 61 PASS, 8 XFAIL (3 known-fix regressions resolved in Phase 1; 5 by-design quirks documented).

### Internal

- `resolveVoiceId` is now a public export of `providers/elevenlabs-tts.ts` so the WS variant can share voice-name resolution.
- `tests/parity/sentence_chunker_parity.py` accepts `--side python | typescript | both` and `--strict` (treats XFAIL as hard fail).

## 0.5.4 (2026-04-27)

Fast-follow to align the Cerebras default with what 0.5.3 already promised in docs and changelog.

### Changed — Cerebras default model
- **Default model bumped to `gpt-oss-120b`** (production tier, ~3000 tok/sec on WSE-3, no deprecation date) in both Python and TypeScript SDKs. 0.5.3 had temporarily kept the default at `llama3.1-8b` while `gpt-oss-120b` rolled out across the Cerebras catalogue; that's no longer needed. Pass `model="llama3.1-8b"` (or any free-tier ID) to opt back into the smaller model.
- 404 `model_not_found` recovery hint and the `"common: …"` candidate list updated to surface `gpt-oss-120b` first. The `TODO(deprecation 2026-05-27)` note for `llama3.1-8b` retirement is preserved in the source.

## 0.5.3 (2026-04-27)

Cost-accuracy, audio-pipeline, and observability hardening across both SDKs, plus opt-in per-call filesystem logging.

### Improved — Cerebras
- **Default model bumped to `gpt-oss-120b`** (production tier, ~3000 tok/sec on WSE-3, no deprecation date) — replaces `llama-3.3-70b`, which is no longer in Cerebras's production catalogue. Docstrings updated against the verified model list at `inference-docs.cerebras.ai/models/overview`.
- **TS retry + backoff on 5xx / 429** — single retry with exponential backoff, honouring `x-ratelimit-reset-tokens-minute` / `x-ratelimit-reset-requests-minute` advisory headers. Terminal failures now throw a typed `PatterError` (was: silent `getLogger().error()` + empty stream).
- **`response_format` parameter** — Python and TS Cerebras providers accept the OpenAI-style structured-outputs dict (e.g. `{ type: "json_schema", json_schema: { ... } }`).
- **Forward additional sampling kwargs** to Cerebras and Groq (both SDKs): `parallel_tool_calls`, `tool_choice`, `seed`, `top_p`, `frequency_penalty`, `presence_penalty`, `stop`.
- **`max_tokens` → `max_completion_tokens` on the wire** for Cerebras and Groq; user-facing API still accepts `max_tokens` / `maxTokens`.
- **`User-Agent: getpatter/<version>` header** added to Cerebras and Groq HTTP requests for upstream attribution.

### Added — pipeline hooks
- **`before_llm` / `after_llm` hooks** (`PipelineHooks` in both SDKs) — receive the messages list pre-LLM and the assistant text post-stream. `before_llm` enables prompt injection / RAG augmentation; `after_llm` enables output validation, redaction, and post-processing.
- **New event types on the `EventBus`** (additive — existing callbacks unchanged): `transcript_partial`, `transcript_final`, `llm_chunk`, `tts_chunk`, `tool_call_started`. Subscribe via `on(...)` for fine-grained pipeline observability.

### Added — providers
- **OpenAITranscribeSTT** — first-class STT class for OpenAI's gpt-4o-transcribe and gpt-4o-mini-transcribe models (~10x faster than Whisper-1).
- **ElevenLabs `eleven_v3`** — typed model literal added; v3 is now selectable via `model_id="eleven_v3"`.
- **Cerebras: gzip compression now enabled by default in the TypeScript SDK** (Python already had it on). Reduces TTFT on prompts >2 KB. Pass `gzipCompression: false` to opt out.

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

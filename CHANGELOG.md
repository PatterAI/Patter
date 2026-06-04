## Unreleased

### Security

- **The built-in metrics dashboard is now auto-protected with a generated token
  when it would be reachable beyond `127.0.0.1`.** The dashboard UI and the
  `/api/*` call-data routes serve call transcripts and metadata (PII).
  Previously, when the dashboard was enabled with no `dashboard_token` /
  `dashboardToken` on an off-host bind (a tunnel is active, an explicit public
  `webhook_url` / `webhookUrl` is configured, or `PATTER_BIND_HOST` is set to a
  non-loopback address), the SDK published those routes unauthenticated and only
  emitted a soft warning — a foot-gun easy to miss in a tunnelled demo or a
  containerised deploy. Now, in exactly that configuration, the SDK
  auto-generates a one-time token, mounts the dashboard behind it, and prints
  the ready-to-use URL (`http://127.0.0.1:<port>/?token=<token>`) in the startup
  banner. The dashboard remains available with zero config — it is no longer
  reachable unauthenticated by accident. This is **not a breaking change**: the
  dashboard is still always served and inbound/outbound calls are unaffected
  (the carrier webhook, media-stream, and `/health` routes always mount). The
  token is per-process — set `dashboard_token` / `dashboardToken` for a stable
  one across restarts. Loopback-only local dev is unchanged (still served open,
  zero-friction); an explicit `dashboard_token` serves behind that token as
  before.
  - `libraries/python/getpatter/server.py`,
    `libraries/typescript/src/server.ts`.

### Added

- **`allow_insecure_dashboard` / `allowInsecureDashboard` escape hatch (opt-in,
  default off).** New optional config on `Patter(...)` (Python) and `serve(...)`
  `ServeOptions` (TypeScript), defaulting to `False` / `false`. When the
  dashboard would be reachable beyond loopback without a configured token (see
  Security above), the SDK auto-generates a token to protect it; setting this
  flag to `True` / `true` instead serves the dashboard fully OPEN (no token,
  unauthenticated) on that exposed bind and logs a `warning`. This is for
  operators who deliberately run the dashboard open behind their own network
  controls (a tailnet, Cloudflare Access, an upstream auth proxy). It leaks call
  transcripts and metadata (PII) to anyone who can reach the URL, so it is NOT
  recommended on a public network — prefer the auto-generated token, or a stable
  `dashboard_token` / `dashboardToken`. Backward compatible: existing callers
  that pass no token and are loopback-only are unaffected; the flag only matters
  on an exposed bind. `libraries/python/getpatter/client.py` /
  `libraries/python/getpatter/server.py`,
  `libraries/typescript/src/types.ts` /
  `libraries/typescript/src/client.ts` /
  `libraries/typescript/src/server.ts`.

- **OpenAI Realtime input noise reduction — stop speakerphone / room noise from
  cutting the agent off.** New `noise_reduction` on the Realtime engine markers
  (`engines.openai.Realtime(noise_reduction="far_field")` /
  `engines.openai_realtime_2.Realtime2(...)` Python; `new Realtime({ noiseReduction:
  "far_field" })` TS) and a matching `Patter.agent(openai_realtime_noise_reduction=
  "far_field")` / `phone.agent({ openaiRealtimeNoiseReduction: "far_field" })`
  field. On a speakerphone, mouse clicks, phone shifts, and background chatter
  were being detected as the caller speaking and barging in over the agent;
  enabling OpenAI's native `far_field` reduction (recommended for conference /
  speakerphone audio; `near_field` for a close handset) filters that out before
  VAD sees it. `None` / `undefined` (default) omits the field entirely — today's
  behaviour, no reduction. The GA/v2 adapter nests it under
  `session.audio.input.input_audio_noise_reduction`; the v1-beta adapter emits it
  top-level — each at the correct path for that endpoint. Invalid values are
  rejected with a clear error. `libraries/python/getpatter/providers/openai_realtime.py`
  / `openai_realtime_2.py`, `libraries/typescript/src/providers/openai-realtime.ts`
  / `openai-realtime-2.ts`.

- **`RealtimeTurnDetection` — tune the Realtime VAD to reject false barge-in.**
  New immutable config (`from getpatter import RealtimeTurnDetection` Python;
  `RealtimeTurnDetection` interface TS) accepted by the Realtime engine markers
  (`Realtime(turn_detection=...)` / `new Realtime({ turnDetection })`) and
  `Patter.agent(realtime_turn_detection=...)` / `phone.agent({ realtimeTurnDetection })`.
  Raise `threshold` (server_vad — higher rejects more background noise) or switch
  to `type="semantic_vad"` with `eagerness="low"` so the model waits for the
  caller to actually finish before treating audio as speech — the missing knob
  for noisy speakerphone links. Each unset field falls back to the adapter's
  current default (server_vad, threshold 0.5, prefix_padding_ms 300,
  silence_duration_ms 300), so omitting it preserves today's behaviour exactly.
  `semantic_vad` emits `{type, eagerness}` only. Patter keeps its client-gated
  barge-in safety values (`create_response` / `interrupt_response` stay internal,
  not exposed). `libraries/python/getpatter/models.py`,
  `libraries/typescript/src/types.ts`.

- **Per-tool execution timeout — long (30-60s) browser-automation / external-API
  tools no longer drop the call at 10s.** New `timeout_s` on the Python `tool()`
  factory and `Tool` dataclass (`tool(name=..., handler=..., timeout_s=60.0)`) and
  `timeoutMs` on the TS `ToolDefinition` (`{ name, handler, timeoutMs: 60_000 }`).
  Previously the tool executor aborted every tool at a hardcoded 10s, killing
  slow tools mid-run; the per-tool value now governs both the handler await and
  the webhook request (clamped to a 300s / 300_000ms ceiling). A timeout is
  terminal — it is NOT retried (retrying would multiply the wait and stall the
  turn) and returns a structured `{error, fallback: true}` so the model can
  recover. Default `None` / `undefined` keeps the existing 10s behaviour. The
  per-tool timeout governs tool execution and is independent of any LLM
  provider's own stream ceiling. The carrier media stream stays open across the
  whole tool call — Twilio keeps the WS up for the call lifetime — so a long tool
  does not drop the leg; pair `timeout_s` with `reassurance` so the line doesn't
  sound dead. `libraries/python/getpatter/tools/tool_executor.py` /
  `services/llm_loop.py` / `_public_api.py`,
  `libraries/typescript/src/llm-loop.ts` / `public-api.ts`.

- **`reassurance` on the Python `tool()` factory — a verbal "one moment" while a
  slow tool runs.** The `Tool` dataclass and `ToolDefinition` already carried
  `reassurance`; it is now exposed on the Python `tool()` keyword factory and
  decorator (`tool(name=..., handler=..., reassurance="One moment while I check
  that for you.")`, or the dict form `{"message": str, "after_ms": int}`) for
  parity with TS object-literal usage. Honoured in Realtime mode (the agent
  speaks the filler if the tool hasn't returned within `after_ms`); pipeline-mode
  injection remains out of scope. `libraries/python/getpatter/_public_api.py`,
  `libraries/python/getpatter/client.py`.

- **`tool_call_preambles` on `Patter.agent(...)` (Realtime modes) — the agent
  speaks a short "let me check" line in its own voice before a slow tool call.**
  Opt-in `bool | str` (default `False`) on `Patter.agent(tool_call_preambles=...)`
  / `phone.agent({ toolCallPreambles })`. When `True`, Patter prepends a native
  "# Preambles" guidance block to the OpenAI Realtime session `instructions` so
  the reasoning model emits one short, action-describing sentence (e.g. "I'll
  check that order now.") immediately before a tool call that may take a moment —
  OpenAI's recommended, first-class UX for 30-60 s tools (most effective on
  `gpt-realtime-2`, where preambles are default-on), with no API field and no
  client-side timer. The block steers the model to vary wording, keep it to one
  sentence, skip it when it can answer immediately, and never imply the result
  before the tool returns. A `str` overrides the block verbatim. When a tool
  also carries a `reassurance` string, that phrase is surfaced to the model as a
  sample preamble in the tool's description. Default `False` leaves the prompt
  byte-identical to prior releases; pipeline mode is unaffected (it has its own
  phone preamble). `libraries/python/getpatter/models.py`,
  `.../client.py`, `.../stream_handler.py` (`apply_tool_call_preambles`,
  `DEFAULT_TOOL_CALL_PREAMBLE_BLOCK`),
  `libraries/typescript/src/types.ts`, `.../stream-handler.ts`
  (`applyToolCallPreambles`).

- **Built-in `consult` escalation tool — give an in-call agent an on-demand
  bridge back to your own back-office agent.** New `ConsultConfig`
  (`getpatter` Python / TS) on `Patter.agent(consult=...)` /
  `phone.agent({ consult })`. When set, Patter auto-injects a `consult_agent`
  tool (Realtime + Pipeline modes) that the in-call agent invokes mid-call to
  reach a customer-hosted HTTP endpoint for deeper reasoning or fresh
  information, then speaks the reply — the orchestrator stays off the per-turn
  path (consulted only on demand), so ordinary turns keep their low latency.
  The tool POSTs `{request, call_id, caller, callee}` and accepts a JSON
  `reply` / `response` / `text` string (or any JSON / plain text). Configurable
  `headers` (e.g. an `Authorization` bearer; never logged) and `timeout_s` /
  `timeoutMs` (default 30 s — higher than the generic webhook-tool 10 s because
  a consult may run deeper reasoning). The URL is SSRF-validated at call start;
  endpoint failures degrade to a spoken fallback rather than crashing the turn.
  ElevenLabs ConvAI is unsupported (its tools live on the ElevenLabs-hosted
  agent) and emits a warning. As a side effect, MCP and consult tools resolved
  per-call are now also advertised to the Realtime model (previously only the
  static `agent.tools` were). `libraries/python/getpatter/tools/consult.py`,
  `libraries/typescript/src/consult.ts`.

- **`ConsultConfig.allow_loopback` / `allowLoopback` — opt-in to point
  `consult` at a trusted local agent.** New optional flag (default `False` /
  `undefined`) on `ConsultConfig`. The consult URL is SSRF-validated at build
  time, which by default rejects loopback / private / link-local hosts. Set
  `allow_loopback=True` (Python) / `allowLoopback: true` (TS) to relax those
  host checks for the consult URL only — e.g. a back-office orchestrator on
  `127.0.0.1`, `localhost`, or an RFC1918 private host. Non-HTTP(S) schemes are
  still rejected even with the flag, and every other webhook path stays strict;
  the relaxation is scoped to the developer-configured consult endpoint, which
  is SDK-user config, not caller input. Cloud-metadata hostnames also become
  reachable when opted in — only enable for URLs you control.
  `libraries/python/getpatter/models.py`,
  `libraries/python/getpatter/tools/tool_executor.py`,
  `libraries/typescript/src/types.ts`, `libraries/typescript/src/server.ts`.

- **Native OpenClaw / OpenAI-compatible `consult` target — connect the voice
  layer to an OpenClaw agent with no hand-written adapter.**
  `ConsultConfig.openclaw(agent=...)` (Python) / `openclawConsult(agent)` (TS)
  point `consult` straight at an OpenClaw agent over its OpenAI-compatible
  `POST /v1/chat/completions` gateway: the handler sends
  `{model: "openclaw/<agent>", messages, user: call_id, stream: false}` and
  speaks `choices[0].message.content`. Built on a generic
  `OpenAICompatibleConsult` codec (`base_url` + `model` + optional `api_key` /
  `api_key_env` / `session_header`), so the same primitive drives any
  OpenAI-compatible gateway (vLLM, Ollama, Groq). The OpenClaw preset targets
  the loopback gateway (`http://127.0.0.1:18789/v1`) by default, auto-enables
  `allow_loopback` for that co-located gateway, reads the operator-grade bearer
  from `OPENCLAW_API_KEY` (never logged), sends the call id as both
  `x-openclaw-session-key` and the OpenAI `user` field (one OpenClaw session per
  call), and attaches a default "let me check" reassurance filler plus a
  consult-biased ("always consult for account facts; never answer them from
  memory") tool description. `timeout_s` / `timeoutMs` keep the phone-safe 30 s
  default. The generic `ConsultConfig(url=...)` webhook path is unchanged and
  remains the escape hatch for custom mappings; exactly one of `url` /
  `openai_compatible` must be set. A 404 from the gateway logs an actionable
  hint to enable `gateway.http.endpoints.chatCompletions`.
  `libraries/python/getpatter/models.py`,
  `libraries/python/getpatter/tools/consult.py`,
  `libraries/typescript/src/types.ts`, `libraries/typescript/src/consult.ts`.

- **OpenClaw post-call notify (`on_call_end` → OpenClaw).** New
  `openclaw_post_call_notifier(agent)` (Python) /
  `openclawPostCallNotifier(agent)` (TS) returns an `on_call_end` callback that
  POSTs the finished call's record (caller, dialed line, duration, transcript) to
  the same scoped OpenClaw agent over its OpenAI-compatible gateway, keyed to the
  call id (`user` + `x-openclaw-session-key`) so it lands in the SAME OpenClaw
  session as the in-call `consult` turns — the brain keeps the record and can
  follow up. Fire-and-forget: errors are logged by type only and never raised
  into call teardown. Wire it on `serve(on_call_end=...)`.
  `libraries/python/getpatter/tools/consult.py`,
  `libraries/typescript/src/consult.ts`.

- **Dashboard: Plivo carrier support in the UI.** The call dashboard now
  renders a Plivo `CarrierBadge` and maps Plivo calls across the cost,
  live-call, and metrics panels, alongside Twilio and Telnyx
  (`dashboard-app/`). Also added `"plivo"` to the PyPI / npm package keywords
  so the SDK surfaces in Plivo-related searches. (#123)
- **`PatterConfigError` (both SDKs).** Added to the TypeScript error taxonomy
  (`errors.ts`) to match Python's `PatterConfigError`, and exported from the
  Python package root — raised for invalid SDK configuration.
- **New Python package exports**: `resample_pcm`, `define_tool`, `DTMF_EVENTS`,
  barge-in strategy helpers, and pricing constants (`PricingUnit`,
  `PRICING_VERSION`, `PRICING_LAST_UPDATED`) are now importable from `getpatter`
  for parity with the TypeScript surface.

### Changed

- **Public config collections are now immutable.** `Agent.tools` / `guardrails`
  / `text_transforms` / `mcp_servers` and `Guardrail.blocked_terms` are tuples
  (Python, `frozen=True`) / `readonly` arrays (TypeScript). Code comparing these
  against a literal `list`/array must compare against a tuple / readonly array.
- **Default `Agent.model` is now `gpt-realtime-mini`** (was
  `gpt-4o-mini-realtime-preview`), aligning the Python dataclass default with the
  TypeScript adapter default. Both are aliases for the same OpenAI Realtime model
  family and share the same pricing row, so cost is unaffected — this removes a
  Python↔TypeScript default mismatch and an internal Python inconsistency (the
  `agent()` helper already defaulted to `gpt-realtime-mini`).

### Fixed

- **Realtime tool context now includes `callee` (TypeScript).** In Realtime
  mode the TypeScript tool-dispatch context passed `{ call_id, caller }` only,
  omitting `callee` (the dialed line) — Python Realtime and TypeScript Pipeline
  already included it. Tools that use the dialed line (e.g. the OpenClaw consult
  system message's "Line dialed: …", or post-call notify) now receive it in every
  mode and both SDKs. `libraries/typescript/src/stream-handler.ts`.

- **Reassurance filler no longer injects a phantom caller turn.** The slow-tool
  reassurance filler (`tool(reassurance=...)`) previously fired
  `conversation.item.create` with `role:"user"`, so the transcript falsely
  showed the caller saying "One moment." and the fake user turn could confuse
  the model. It now speaks via a dedicated assistant-attributed
  `send_reassurance()` / `sendReassurance()` path — a bare `response.create`
  with explicit instructions, no fake user item — matching how
  `send_first_message` already makes the agent speak a verbatim line without a
  `role:user` turn. The shared `send_text()` (used by guardrail replacement,
  tool progress, and DTMF) is untouched. Early-cancel is preserved: nothing is
  spoken if the tool returns before `after_ms`. On the GA adapter the filler
  uses the GA-valid `output_modalities` + re-injected voice (the GA endpoint
  rejects the v1 `modalities` key). `libraries/python/getpatter/providers/openai_realtime.py`
  / `openai_realtime_2.py`, `.../stream_handler.py` (`_schedule_reassurance`),
  `libraries/typescript/src/providers/openai-realtime.ts` / `openai-realtime-2.ts`,
  `.../stream-handler.ts`.
- **DeepFilterNet noise suppression was a silent no-op.** The resampler could
  never reach the 48 kHz the model requires, the error was swallowed, and raw
  audio passed through unsuppressed. Now resampled correctly
  (`providers/deepfilternet_filter.py`, `providers/deepfilternet-filter.ts`).
- **Parallel tool-calls returned a 400 from Anthropic and Gemini.** Multiple
  tool results in one turn produced consecutive same-role messages both
  providers reject; they are now merged into a single turn in both SDKs
  (`providers/anthropic_llm.py` / `anthropic-llm.ts`, `providers/google_llm.py`
  / `google-llm.ts`).
- **Anthropic prompt-cache savings were not billed in TypeScript.** The TS
  Anthropic provider now emits cache read/write token counts on its usage chunk
  (parity with Python), so cached-prompt cost reductions are reflected in the
  metrics (`providers/anthropic-llm.ts`).
- **MCP client URLs were not SSRF-guarded.** Both SDKs now validate the MCP
  server URL (blocking link-local, loopback, and private ranges) before opening
  the transport (`tools/mcp_client.py`, `tools/mcp-client.ts`).
- **AMD callbacks were clobbered across concurrent outbound calls.** The single
  answering-machine-detection slot is now a per-call map in both SDKs, keyed by
  call SID (`server.py` / `server.ts`, `client.ts`).
- **`FallbackLLMProvider` crashed in pipeline mode (Python).** `stream()` now
  accepts and forwards `cancel_event`, matching how the pipeline invokes every
  provider (`services/fallback_provider.py`).
- **Blocking model/inference calls ran on the event loop.** Krisp and Whisper
  now offload to `asyncio.to_thread` so audio is not dropped
  (`providers/krisp_filter.py`, `providers/whisper_stt.py`).
- **WebSocket reads could hang indefinitely.** Added read timeouts and surfaced
  background-task exceptions across providers and the remote-message path
  (`services/remote_message.py` / `remote-message.ts`).
- **ElevenLabs API key was a publicly readable field.** It is now private in
  both SDKs (`providers/elevenlabs_ws_tts.py`, `providers/elevenlabs-ws-tts.ts`).
- **Async done-callbacks raised `CancelledError` on normal shutdown.** They now
  guard `cancelled()` before reading `.exception()` (`observability/event_bus.py`,
  `providers/cartesia_stt.py`, `dashboard/store.py`).
- **Pipeline could accumulate orphaned conversation-history turns.** The
  speculative user turn is popped on the no-handler / vetoed path
  (`stream_handler.py`).
- **Py↔TS parity drift fixed**: `DTMF_EVENTS` ordering, the OpenAI TTS default
  model, and the end-of-utterance metric emit guard now match across SDKs.
- **Telnyx STT logs now use the `getpatter.*` namespace.**
  `providers/telnyx_stt.py` logged under the stale `patter.providers.telnyx_stt`
  namespace; aligned to `getpatter.providers.telnyx_stt` like every other module
  so `getpatter.*` log-level filters capture it.
- **Pipeline-mode turns after the first now record per-turn metrics and
  broadcast the live dashboard transcript.** `anchorUserSpeechStart()` re-opened
  a turn (set `_turnStart`) without clearing the `_turnAlreadyClosed` guard, so
  every user→agent turn after the `firstMessage` made `recordTurnComplete()` a
  no-op — silently dropping per-turn latency/cost AND the live SSE
  `turn_complete` event that feeds the dashboard transcript (the transcript only
  appeared after the call ended). Cleared the flag in `anchorUserSpeechStart()`,
  mirroring what `startTurn()` already does, in both SDKs
  (`libraries/typescript/src/metrics.ts`,
  `libraries/python/getpatter/services/metrics.py`). The post-commit barge-in
  guard is untouched — the method no-ops once the turn is committed.

### Security

- **Bumped vulnerable transitive/runtime dependencies in the TypeScript SDK**
  (`libraries/typescript/package-lock.json`). `ws` 8.20.0 → 8.21.0 closes an
  uninitialised-memory disclosure (GHSA-58qx-3vcg-4xpx); `qs` 6.15.1 → 6.15.2
  (pulled in by `express` / `body-parser`) closes a `qs.stringify` DoS
  (GHSA-q8mj-m7cp-5q26); `brace-expansion` → 5.0.6 / 2.1.0 closes a regex DoS
  (GHSA-jxxr-4gwj-5jf2). All within the existing semver ranges — no `package.json`
  change, no public API change. `npm audit` now reports 0 vulnerabilities for the
  shipped SDK.

## 0.6.3 (2026-05-29)

### Added

- **Plivo as a third telephony carrier (both SDKs), full Twilio/Telnyx
  parity.** `Patter(carrier=Plivo(), ...)` / `new Patter({ carrier: new
  Plivo(), ... })` — outbound dials via Plivo's REST API (`answer_url` /
  `hangup_url` / async `machine_detection_url`), inbound voice/status/AMD/
  transfer webhooks with **V3 HMAC-SHA256 signature verification** (fails
  closed), bidirectional media WebSocket (`playAudio` / `clearAudio` /
  `checkpoint`), native `sendDTMF` over the media socket (a capability
  Twilio Media Streams lacks), voicemail drop, and pricing reconciled from
  the Plivo CDR. `Plivo` / `PlivoAdapter` exported from both package roots.
  `call(wait=True)` resolves correctly for Plivo too (AMD → voicemail,
  status callback → no_answer / busy / failed). Contributed by
  @amalshaji-plivo (#121).
- **Completion-aware outbound calls: `call(wait=True)` → `CallResult`
  (both SDKs).** An AI agent can now place a call and `await` its real
  outcome in one line instead of hand-wiring `on_call_end`/`onCallEnd` to
  an event and remembering to tear the server down. `wait` defaults to
  `False` (fire-and-forget, returns `None`/`void` — unchanged), so this is
  fully backward compatible. When `wait=True` the call resolves to a new
  `CallResult` (`call_id`, `outcome`, `status`, `duration_seconds`,
  `transcript`, `cost`, `metrics`); `outcome ∈ answered / voicemail /
  no_answer / busy / failed`, every value derived from a real carrier
  signal — answered/voicemail from the AMD result plus the media-stream
  end, no_answer/busy/failed from the carrier status callback (Twilio) or
  `call.hangup` cause (Telnyx) when the call never reaches media.
  `wait=True` requires an active server (raises `PatterConnectionError`
  otherwise) and is backstop-timeout bounded at `ring_timeout + 1800 s`.
  Python `libraries/python/getpatter/{models,client,server}.py`; TypeScript
  `libraries/typescript/src/{types,client,server,stream-handler}.ts`.
- **`async with Patter(...)` (Python) / `await using` via
  `[Symbol.asyncDispose]` (TypeScript) for guaranteed teardown.** Exiting
  the block always runs `disconnect()` — on the normal path and on
  exception — so a still-running TTS WebSocket can no longer keep the user
  billed after the SDK is done. `disconnect()` now also fails any in-flight
  `call(wait=True)` awaiters instead of letting them hang to the backstop.

- **Docs: new `Integrations → OpenClaw` page at
  [`docs.getpatter.com/integrations/openclaw`](https://docs.getpatter.com/integrations/openclaw).**
  Shows the `npx skills add patterai/skills` one-liner for OpenClaw users,
  documents the alternative `openclaw mcp set patter ...` path with both
  stdio and streamable-http transports, and surfaces the
  `before_tool_call.requireApproval` caveat (MCP tools bypass OpenClaw's
  native approval hooks, so outbound `patter__make_call` should be in
  `tools.elevated`). Added under a new `Integrations` group in the Home
  product nav (`docs/docs.json`).

- **Telnyx `call.recording.saved` webhook handler — Python parity with
  TypeScript.** `libraries/python/getpatter/server.py` now handles the
  Telnyx `call.recording.saved` Call Control event and logs the recording
  URL (mirrors `libraries/typescript/src/server.ts:1115`). Fallback order:
  `recording_urls.mp3` → `recording_urls.wav` → `public_recording_urls.mp3`
  → `public_recording_urls.wav`. Closes the parity gap that left the
  Python webhook silent on recording completion while the bridge already
  POSTed to `actions/record_start` / `actions/record_stop`.

- **README: prominent "Skills for Coding Agents" section linking to
  [`PatterAI/skills`](https://github.com/PatterAI/skills).** Top-nav anchor,
  callout quote with one-line install (`npx skills add patterai/skills`),
  table of the five skills, version pinning instructions, and link to the
  `skills.sh` page. Mirrors the pattern adopted by ElevenLabs, Vapi,
  Cartesia, Anthropic, and Coinbase (every vendor ships skills in a
  dedicated `<org>/skills` repo rather than inside the SDK).

### Changed

- **Agent Skills moved out of the SDK into a dedicated repository:
  [`PatterAI/skills`](https://github.com/PatterAI/skills).** The five
  `SKILL.md` files (and references) previously under `skills/` in this
  repo have been migrated to their own repository, matching the industry
  pattern (ElevenLabs `elevenlabs/skills`, Vapi `VapiAI/skills`, Cartesia
  `cartesia-ai/skills`, Anthropic `anthropics/skills`). The dedicated repo
  gives skills independent versioning, a cleaner `npx skills add
  patterai/skills` install path, and avoids growing the SDK clone size
  with non-runtime files. Skill content is unchanged — only the location
  moved. The previous install path `npx skills add patterai/patter` no
  longer resolves; use `npx skills add patterai/skills` instead.

- **READMEs (root + `libraries/python/` + `libraries/typescript/`) no
  longer claim "Recording is Twilio-only".** Telnyx recording parity is
  now documented consistently across all three READMEs. The Python and
  TypeScript stream bridges already drove `actions/record_start` /
  `actions/record_stop` against the Telnyx Call Control API when
  `recording=True` was passed; the README copy lagged behind the code.

### Fixed

- **Plivo + Pipeline + ElevenLabs produced garbled/static outbound audio
  (TypeScript).** `StreamHandler.isTtsOutputFormatNativeForCarrier()` only
  handled `twilio` / `telnyx`, so for `plivo` it returned `false` and the
  pipeline re-encoded the already-μ-law ElevenLabs output as if it were
  PCM16 — mangling it. Added the `plivo → ulaw_8000` native-format case
  (`libraries/typescript/src/stream-handler.ts`). Python was unaffected
  (its Plivo bridge runs the handler with `for_twilio=True`).
- **`PatterTool` (Python) reported `cost_usd=None` and
  `duration_seconds=0.0` on every call.** The result builder probed the
  `on_call_end` `metrics` payload as a `dict`, but the live payload delivers
  a `CallMetrics` dataclass, so both fields were silently dropped. Rebuilt
  on the new `call(wait=True)` → `CallResult` path, which reads
  `cost.total` / `duration_seconds` as real attributes; the envelope now
  also carries `outcome`. `libraries/python/getpatter/integrations/patter_tool.py`.

- **Python: pipeline mode crashed immediately on stream start with
  `AttributeError: 'ClientConnection' object has no attribute 'closed'`
  (#111, #113).** Three WS-liveness checks
  (`libraries/python/getpatter/stream_handler.py:2192` /
  `:2230` and
  `libraries/python/getpatter/providers/elevenlabs_ws_tts.py:453`)
  still used the legacy `websockets<11` `.closed` property, but Patter
  pins `websockets>=14,<16` in
  `libraries/python/pyproject.toml` where `.closed` was removed in v12.
  Promoted the existing `_is_parked_ws_alive` helper out of
  `stream_handler.py` into
  `libraries/python/getpatter/utils/ws.py` as `is_ws_alive`, and
  re-used it at every call site. Handles modern (`state`,
  `close_code`), legacy (`closed`), and unknown shapes; never defaults
  to "alive" on unknown shapes so a dead socket can't be handed to the
  live adapter. 8 new unit tests in
  `libraries/python/tests/test_utils_ws.py`. Thanks
  [@knowsuchagency](https://github.com/knowsuchagency).

- **Python: pipeline mode did not inject the built-in `transfer_call`
  / `end_call` tools into the `LLMLoop`, so pipeline agents could not
  initiate a handoff or hangup no matter what the system prompt said
  (#110, #115).** Realtime mode had been injecting both built-ins at
  `libraries/python/getpatter/stream_handler.py:997`
  (`agent_tools + [TRANSFER_CALL_TOOL, END_CALL_TOOL]`), but the
  pipeline path at
  `libraries/python/getpatter/stream_handler.py:2426` was passing
  through only the user-provided tools. Added
  `_augment_with_builtin_handoff_tools` that builds handler closures
  with the `(arguments, call_context)` signature expected by
  `ToolExecutor._invoke_handler` and wires them to the existing
  telephony-level `_transfer_fn` / `_hangup_fn` already attached to
  `PipelineStreamHandler`. Built-ins are skipped when the
  corresponding telephony fn is missing (keeps the non-telephony test
  harness path clean). Verified end-to-end against `gpt-4o-mini` on
  Twilio: caller says "transfer me", LLM emits
  `transfer_call({"number": "+1..."})`, `_twilio_transfer` fires, the
  call bridges. 6 new unit tests in
  `libraries/python/tests/test_pipeline_builtin_tools.py`. Thanks
  [@knowsuchagency](https://github.com/knowsuchagency).

- **TypeScript: pipeline mode missing built-in `transfer_call` /
  `end_call` tools — parity fix for #115.** Both `new LLMLoop(...)`
  call sites in `libraries/typescript/src/stream-handler.ts:1891` and
  `:1906` were passing `agent.tools` through unchanged; the built-ins
  shipped in `server.ts` (now exported as `TRANSFER_CALL_TOOL` /
  `END_CALL_TOOL`) were only injected into the Realtime path at
  `server.ts:374`. Added `augmentWithBuiltinHandoffTools` in
  `libraries/typescript/src/stream-handler.ts` that mirrors the Python
  helper: appends the two built-ins with handler closures that
  validate E.164 / default `reason` and dispatch to the existing
  telephony bridge methods (`this.deps.bridge.transferCall` /
  `endCall`). 8 new unit tests in
  `libraries/typescript/tests/pipeline-builtin-tools.test.ts`. Closes
  the parity gap surfaced by #115.

- **Docs: `docs/typescript-sdk/events.mdx` advertised the same
  non-existent `phone.events.on(PatterEventType.X, handler)` API as
  the Python events page — TypeScript parity fix for #114.** The TS
  `Patter` class never exposed an `.events` attribute; `EventBus` is
  instantiated per `StreamHandler`. Replaced the broken `EventBus`
  section with documentation of the APIs that actually exist on the
  TypeScript `Patter` class: **Speech-edge events** via the attribute
  setters (`onUserSpeechStarted` / `onUserSpeechEnded` /
  `onUserSpeechEos` / `onAgentSpeechStarted` / `onAgentSpeechEnded` /
  `onLlmToken` / `onAudioOut`, proxied to `this.speechEvents` at
  `libraries/typescript/src/client.ts:241-330`) and **Tool events via
  `onTranscript`** (tool invocations surface with `role === "tool"`,
  `tool_name`, `tool_args`, `tool_result` — payload defined at
  `libraries/typescript/src/stream-handler.ts:2988-3010`).

- **Docs: `docs/python-sdk/events.mdx` advertised a non-existent
  `phone.events.on(PatterEventType.X, handler)` API that crashed
  immediately with `AttributeError: 'Patter' object has no attribute
  'events'` (#112, #114).** The `_EventBus` is instantiated per
  `StreamHandler` (`libraries/python/getpatter/stream_handler.py:517`)
  and never exposed on the `Patter` class. Replaced the broken
  `EventBus` section with documentation of the APIs that actually
  exist: **Speech-edge events** via the attribute setters on `Patter`
  (`on_user_speech_started` / `on_user_speech_ended` /
  `on_user_speech_eos` / `on_agent_speech_started` /
  `on_agent_speech_ended` / `on_llm_token` / `on_audio_out`, proxied
  to `self.speech_events` at
  `libraries/python/getpatter/client.py:351-410`) and **Tool events
  via `on_transcript`** (tool invocations surface with `role="tool"`,
  `tool_name`, `tool_args`, `tool_result` — payload defined at
  `libraries/python/getpatter/stream_handler.py:929`). Thanks
  [@knowsuchagency](https://github.com/knowsuchagency).

## 0.6.2 (2026-05-25)

### Added

- **`OpenAIRealtime2` / `OpenAIRealtime2Adapter` — Python GA Realtime API
  adapter (parity with TypeScript `OpenAIRealtime2` / `OpenAIRealtime2Adapter`
  in `libraries/typescript/src/engines/openai-2.ts` /
  `libraries/typescript/src/providers/openai-realtime-2.ts`).** The GA
  endpoint rejects the legacy `OpenAI-Beta: realtime=v1` header and speaks a
  different `session.update` wire shape (`output_modalities`, nested
  `audio.{input,output}` with MIME type strings, `session.type = "realtime"`).
  `OpenAIRealtime2Adapter` (in
  `libraries/python/getpatter/providers/openai_realtime_2.py`) subclasses
  `OpenAIRealtimeAdapter` and overrides `connect()`, `send_audio()`,
  `receive_events()`, and `send_first_message()` to speak the GA wire shape
  and perform bidirectional transcoding (mulaw 8 kHz ↔ PCM 24 kHz) required
  because the GA audio engine silently drops mulaw frames. `OpenAIRealtime2`
  engine marker (in `libraries/python/getpatter/engines/openai_realtime_2.py`)
  defaults to `gpt-realtime-2`. Both are exported from the top-level package:
  `from getpatter import OpenAIRealtime2, OpenAIRealtime2Adapter`. Wire up via
  `phone.agent(engine=OpenAIRealtime2(reasoning_effort="low"), ...)`.

### Changed

- **`OpenAIRealtime` default model changed from `gpt-4o-mini-realtime-preview`
  to `gpt-realtime-mini`** in
  `libraries/python/getpatter/engines/openai.py` and the `agent()` sentinel
  in `libraries/python/getpatter/client.py`. The beta
  `gpt-4o-mini-realtime-preview` model is deprecated on the GA endpoint as of
  2026-05. `gpt-realtime-mini` is the equivalent GA model. Existing callers
  that do not pin a model are automatically upgraded; callers that explicitly
  pass `model="gpt-4o-mini-realtime-preview"` should migrate to
  `model="gpt-realtime-mini"` or switch to `OpenAIRealtime2`.

- **`phone.ready` and `phone.tunnel_ready` — serve-ready awaitables for
  outbound call orchestration (Python parity with TypeScript).** Both
  SDKs have always exposed these futures on the `Patter` class, but the
  Python docs showed the `asyncio.sleep(2)` anti-pattern instead of the
  correct `await phone.ready` pattern. Updated `docs/python-sdk/local-mode.mdx`
  to replace the `asyncio.sleep` example with `await phone.ready`, document
  the reject-on-failure guarantee, and add a note on `await phone.tunnel_ready`
  for hostname-only use cases. Added 15 unit tests covering lazy creation,
  idempotent access, resolution, rejection, idempotent resolve/reject guards,
  static-webhook pre-resolution, and post-`disconnect()` future recreation —
  mirroring the TS `client.test.ts` ready/tunnelReady coverage.

### Fixed

- **TypeScript `TwilioAdapter.generateStreamTwiml` now accepts an optional
  `parameters` argument (parity with Python `generate_stream_twiml`).** The
  static method previously ignored caller/callee context — passing
  `parameters: Record<string, string>` now emits
  `<Parameter name="..." value="..."/>` children of `<Stream>`, which is the
  only reliable path for pre-populating `start.customParameters` on the WS
  `start` frame (Twilio strips query-string params from the `<Stream url=...>`
  before the WebSocket handshake). The inbound webhook path in `server.ts`
  already inlined this TwiML directly; `generateStreamTwiml` is now brought
  into full API-surface parity so callers who construct TwiML via the adapter
  get the same behaviour. File: `libraries/typescript/src/providers/twilio-adapter.ts`.

- **Python outbound Twilio calls crashed with `TypeError: unexpected
  keyword argument 'StatusCallback'` (and similar for `Timeout`,
  `MachineDetection`, `AsyncAmd`).** `libraries/python/getpatter/client.py`
  was building the `extra_params` dict with PascalCase keys matching
  Twilio's REST wire protocol, but `twilio-python`'s
  `Client.calls.create(**kwargs)` only accepts snake_case — it
  translates internally to PascalCase before hitting the wire. Every
  outbound call using machine detection, `ring_timeout`, or status
  callbacks crashed at the SDK boundary (reported externally on
  zenn.dev for SDK 0.5.4). Fixed at source: all keys in `extra_params`
  are now snake_case (`status_callback`, `machine_detection`,
  `timeout`, `async_amd`, `async_amd_status_callback`,
  `status_callback_method`). Added a defensive PascalCase →
  snake_case normalisation pass in
  `libraries/python/getpatter/providers/twilio_adapter.py` so any
  future caller passing the wire-protocol spelling is auto-corrected
  before reaching the SDK. TypeScript SDK is unaffected — it sends raw
  `URLSearchParams` directly to Twilio's REST endpoint where
  PascalCase is the correct on-wire form. Regression locked in by
  `libraries/python/tests/unit/test_twilio_adapter_snake_case_kwargs.py`.

- **Phantom barge-in: cellular noise within 100 ms post-pickup was
  triggering self-cancellation of the prewarmed greeting.** Bumped
  `MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_NO_AEC` from 100 ms → 500 ms
  in `libraries/typescript/src/stream-handler.ts` and
  `libraries/python/getpatter/stream_handler.py`. The 100 ms window was
  too tight — Twilio's media stream can emit background carrier noise
  (clicks, handshake tones, audio codec initialization) within the first
  100 ms after pickup, which the VAD read as speech-like energy and
  triggered a barge-in cancel. Extending to 500 ms allows the carrier
  audio path to stabilise before the agent's greeting becomes cancelable.

- **VAD telephony preset too sensitive: background room voices tripping
  barge-in.** `SileroVAD.forPhoneCall()` factory (TS) /
  `SileroVAD.for_phone_call` (Py) now raises activation threshold 0.5 →
  0.8 and deactivation threshold 0.35 → 0.65. The Silero model's
  upstream defaults (0.5 / 0.35) are tuned for studio audio; when
  running on 8 kHz telephony-band upsampled to 16 kHz, non-speech room
  noise (HVAC, background chatter, line buzz) was accumulating energy
  above the 0.5 threshold. Real-call acceptance testing showed natural
  pauses in the user's speech no longer trigger false barge-ins at the
  higher thresholds. Files: `libraries/typescript/src/providers/
  silero-vad.ts`, `libraries/python/getpatter/providers/silero_vad.py`.

- **`prewarmFirstMessage` default reverted to `false`.** An earlier
  0.6.2 attempt defaulted the flag to `true` in the factory; this
  proved incompatible with the above barge-in fixes. When the greeting
  is prewarmed but the phantom-barge-in (or VAD sensitivity) fires
  incorrectly on carrier-side noise, the agent cancels the cached
  audio without having spoken a character, leaving the caller in silence
  for 1–2 s while the agent recovers from the false cancel-and-restart
  cycle. Reverting to `prewarmFirstMessage: false` (TS) /
  `prewarm_first_message=False` (Py) at the factory level in
  `libraries/typescript/src/client.ts:Patter.agent()` and
  `libraries/python/getpatter/client.py:Patter.agent()`. Users who
  *want* the latency reduction should opt in explicitly: `phone.agent({
  prewarmFirstMessage: true })` — recommended for inbound calls and
  low-noise deployments. Realtime / ConvAI modes unaffected.

- **ElevenLabs HTTP TTS now auto-detects carrier and sets
  `outputFormat`.** Added `setTelephonyCarrier(carrierHint: string)`
  method to `ElevenLabsTTS` (TS) / `ElevenLabsTTS.set_telephony_carrier`
  (Py). When constructing `ElevenLabsTTS()` without an explicit
  `outputFormat` on Twilio, the factory `ElevenLabsTTS.forTwilio()`
  calls `setTelephonyCarrier("twilio")` to flip `outputFormat` to
  `"ulaw_8000"`, eliminating the per-frame resample + mulaw encode
  overhead. The plain constructor now only forwards `outputFormat` when
  the caller passed one explicitly — was unconditionally forwarding a
  `"pcm_16000"` fallback that disabled the carrier auto-flip logic.
  This matches the existing `ElevenLabsWebSocketTTS` carrier-aware
  behaviour. Files: `libraries/typescript/src/providers/elevenlabs-tts.ts`,
  `libraries/python/getpatter/providers/elevenlabs_tts.py`.

- **ElevenLabs WebSocket TTS now exposes `cancelActiveStream()` for
  barge-in cleanup.** The WebSocket variant held a live `activeStreamWs`
  reference but had no public way to abort it. `StreamHandler.cancelSpeaking`
  / `handleStop` / `handleWsClose` now call `tts.cancelActiveStream()`,
  unblocking the synthesizeStream generator's inner `await Promise<frame>`
  loop immediately when the carrier ends the call or the user barges in.
  Root cause of the post-hangup 30 s timeout error logs and stale token
  billing. Files: `libraries/typescript/src/providers/elevenlabs-ws-tts.ts`,
  `libraries/python/getpatter/providers/elevenlabs_ws_tts.py`.

- **Wrapper class TTS `outputFormat` field now conditional.** When an
  `ElevenLabsTTS` or `ElevenLabsWebSocketTTS` wrapper receives a carrier
  hint (e.g. Twilio), the wrapper's `outputFormat` field is set only if
  the caller passed it explicitly. Previous logic always forwarded a
  fallback value, which caused the carrier auto-flip to treat
  `outputFormat` as explicit and skip the optimization. Now the carrier
  auto-flip logic runs correctly: if no `outputFormat` was passed, the
  wrapper field remains `undefined`/`None` and the carrier-specific Twilio
  path activates naturally. Files: `libraries/typescript/src/tts/elevenlabs.ts`,
  `libraries/typescript/src/tts/elevenlabs-ws.ts`,
  `libraries/python/getpatter/tts/elevenlabs.py`.

- **`sendPacedFirstMessageBytes` timing rewritten: burst mode, no per-chunk
  sleep.** The original implementation paced each prewarm chunk with a
  `setTimeout` / `asyncio.sleep` of one chunk-equivalent of playout time
  (~40 ms for the 1280-byte default chunk). Combined with the
  `waitForMarkWindow` back-pressure await and JavaScript/asyncio timer
  jitter, effective delivery dropped BELOW Twilio's 8 kHz playout clock,
  producing repeated carrier-side underruns. Caller heard "slow, gravelly,
  and arriving more slowly than the rest". Twilio's docs (Media Streams →
  WebSocket Messages) state "media messages of any size" are "buffered
  and played in the order received" by the carrier-side media server — the
  carrier owns the playout clock. Rewrote to burst all prewarm chunks
  back-to-back with 20 ms frame granularity (no per-chunk sleep), matching
  the live-TTS streaming path that always worked. Per-chunk marks still
  emitted for fine-grained barge-in cut. Files: `libraries/typescript/src/
  stream-handler.ts`, `libraries/python/getpatter/stream_handler.py`.

- **Mulaw native fast path in audio encode: skip resample + encode when
  TTS outputs `ulaw_8000` natively.** When pipeline mode detects
  `tts.outputFormat === "ulaw_8000"` on Twilio, `encodePipelineAudio`
  skips the resample (16 kHz → 8 kHz) + mulaw encode chain entirely and
  base64-encodes the raw bytes. Probed once in `initPipeline` and cached
  as `ttsOutputFormatNativeForCarrier`. Saves ~1–2 ms per 20 ms frame,
  cumulative ~5–10 % CPU when deployed at scale. Files:
  `libraries/typescript/src/stream-handler.ts`, `libraries/python/
  getpatter/stream_handler.py`.

- **`handleStop` / `handleWsClose` now abort in-flight LLM and cancel TTS
  immediately.** When the carrier ends a call or the StreamHandler is torn
  down, both paths now call `llmAbort()` (to unblock any pending LLM stream)
  and `tts.cancelActiveStream()` (to unblock any pending TTS stream).
  Prevents stale token billing and 30 s timeout error logs from post-hangup
  tasks trying to drain a closed WebSocket. Files: `libraries/typescript/src/
  stream-handler.ts`, `libraries/python/getpatter/stream_handler.py`.

- **Python SDK parity sync for 2026-05-20 acceptance session.** All TS
  fixes landed during PSTN acceptance testing are now ported to Python:
  `ElevenLabsTTS.set_telephony_carrier` (HTTP variant, mirrors WS),
  `ElevenLabsWebSocketTTS.cancel_active_stream` + `_active_stream_ws`
  tracking, `_do_cancel_for_barge_in` / `cleanup` calling
  `cancel_active_stream` (duck-typed), `_is_tts_output_format_native_for_carrier`
  probe + `_tts_output_format_native_for_carrier` flag + audio-sender bypass
  in `PipelineStreamHandler.start`, `_spawn_prewarm_first_message` accepting
  `carrier=` and calling `set_telephony_carrier` before synthesis, and the
  `tts/elevenlabs.py` wrapper only forwarding `output_format` when explicitly
  passed. Files: `libraries/python/getpatter/providers/elevenlabs_tts.py`,
  `libraries/python/getpatter/providers/elevenlabs_ws_tts.py`,
  `libraries/python/getpatter/stream_handler.py`,
  `libraries/python/getpatter/tts/elevenlabs.py`,
  `libraries/python/getpatter/client.py`.

- **Bidirectional race guard on `recordTurnComplete` / `recordTurnInterrupted`.**
  The original guard (added earlier in this release) was one-directional:
  a late `recordTurnComplete` after `recordTurnInterrupted` was dropped,
  but the inverse ordering (a late interrupt after a completed turn)
  could still overwrite a just-emitted turn record. The current caller
  paths can't produce that ordering, but the symmetric guard hardens
  the accumulator against future refactors. Both `recordTurnComplete`
  and `recordTurnInterrupted` now set `_turnAlreadyClosed`/`
  _turn_already_closed` and check it on entry. Same fix in
  `libraries/python/getpatter/services/metrics.py` and
  `libraries/typescript/src/metrics.ts`; regression tests added in both
  suites.

### Fixed

- **Pipeline metrics: `transcript.jsonl` rows after a barge-in carried an
  empty `user_text` even when the user had clearly spoken.** Root cause
  was a race between the two turn-close paths: a VAD-driven barge-in
  fired `record_turn_interrupted` / `recordTurnInterrupted` synchronously
  inside the audio handler and `_reset_turn_state` cleared
  `_turn_user_text`, while the in-flight pipeline LLM stream kept
  unwinding on its own task and eventually reached
  `record_turn_complete` / `recordTurnComplete` — which then pushed a
  second turn for the same logical exchange carrying `user_text=""`.
  Both SDKs now flip a `_turn_already_closed` / `_turnAlreadyClosed`
  guard on `record_turn_interrupted` and have `record_turn_complete`
  return `None` / `null` until the next `start_turn` re-arms the
  accumulator. `_emit_turn_metrics` / `emitTurnMetrics` were already
  null-safe, so the late call becomes a silent no-op end-to-end.
  Regression tests pinning the bargein → llmAbort → late-complete
  ordering live in `libraries/python/tests/test_metrics.py` and
  `libraries/typescript/tests/metrics.test.ts`. See
  `patter-sdk-acceptance/BUGS.md` (2026-05-05 entry).

- **CI: Security Audit workflow could not upload Bandit SARIF to the GitHub
  Security tab.** The `bandit` job in `.github/workflows/audit.yml` was
  failing on `github/codeql-action/upload-sarif` with `Resource not
  accessible by integration` because the job inherited the repo-default
  read-only `GITHUB_TOKEN` permissions. Added an explicit
  `permissions: { contents: read, security-events: write }` block on the
  job so SARIF findings reach the Security tab as intended. Bumped the
  action from `@v3` to `@v4` to drop the deprecation warning ahead of the
  December 2026 sunset.

## 0.6.1 (2026-05-15)

### Fixed — `OpenAIRealtime2`: audio transcoding for Twilio + outbound chunking + VAD tuning (TypeScript only)

End-to-end audio support for `gpt-realtime-2` over Twilio. The GA endpoint
nominally accepts `audio/pcmu` (mulaw 8 kHz) in `session.update` but its
audio engine silently drops mulaw frames — `input_audio_buffer.commit`
reports *"buffer only has 0.00ms of audio"* even after several seconds
of valid mulaw appended, so the user's voice never reaches the model and
the model's response is generated as PCM-24 (regardless of the declared
output format) — Twilio plays raw PCM bytes interpreted as mulaw and the
caller hears nothing. Until OpenAI ships native g711 support on the GA
endpoint (community thread #1380750), we transcode on both directions
inside `OpenAIRealtime2Adapter`.

**Inbound (Twilio → model).** Override `sendAudio`:
- Decode mulaw → PCM-16 8 kHz (`mulawToPcm16`).
- Apply 2× gain to compensate for the reduced dynamic range of the
  decoded mulaw signal — telephony peaks land around ±8000 in PCM-16,
  the GA VAD is calibrated against studio audio peaking around ±16-24k.
- Direct 3× linear-interpolation upsample to 24 kHz with a one-sample
  carry across chunk boundaries (eliminates the DC step at every 20 ms
  Twilio frame boundary that previously kept the VAD pinned below
  threshold).
- Send `input_audio_buffer.append` with PCM-24 base64.
- `session.audio.input.format` is set to `{ type: "audio/pcm",
  rate: 24000 }` to match.

**Outbound (model → Twilio).** Wrap the audio-delta translation:
- Decode PCM-24 from `response.output_audio.delta`.
- Resample 24 k → 16 k → 8 k using two chained `StatefulResampler`
  instances. Direct 24 k → 8 k (one step) is available in
  `transcoding.ts` but uses only linear interpolation with no
  anti-alias filter; the two-step chain routes the signal through the
  16 k → 8 k path which carries a 5-tap FIR anti-alias filter,
  empirically the only configuration that produced audibly clean
  speech on the carrier leg.
- Encode PCM-8 → mulaw 8 kHz.
- Split the resulting mulaw into 20 ms (160-byte) slices and emit one
  synthetic `response.audio.delta` event per slice. Twilio's media
  pipeline expects ~20 ms frames; shipping one ~200-400 ms delta as a
  single frame stalls the playout scheduler and the caller hears
  either a silent gap then a burst, or nothing at all if Twilio drops
  the over-large frame.

**VAD tuning.** GA `server_vad` is too strict by default for
3×-upsampled telephony-band audio. We lower `threshold: 0.1` (from the
0.5 default) and raise `silence_duration_ms: 500` so phone-band speech
reliably triggers `speech_started` / `speech_stopped`.

**Engine wrapper:** `sendFirstMessage` continues to inject explicit
`output_modalities`, `audio.output.voice` and `reasoning.effort:"minimal"`
(see prior commit). The first-message audio path now also benefits from
the outbound transcoding + chunk-splitting changes — `firstMessage`
plays in the configured voice (`alloy`) at native cadence.

**Visibility bumps.** `OpenAIRealtimeAdapter` had a few more `private`
fields promoted to `protected` (`ws`, `armHeartbeatAndListener`,
`options`) so the subclass can install the wire-level shim and reuse
the parent's message dispatch unchanged.

**Known limitation.** The Twilio user's voice now reaches the GA model
audibly but the GA `server_vad` is still tuned for studio audio and the
caller side of the conversation requires a more aggressive workaround
(custom semantic VAD or carrier-side audio enhancement). Pipeline-mode
(STT + LLM + TTS) is the recommended production path for Twilio +
telephony in 0.6.1 until OpenAI ships native g711_ulaw on the GA
endpoint.

Files: `libraries/typescript/src/providers/openai-realtime-2.ts`,
`libraries/typescript/src/providers/openai-realtime.ts` (visibility
bumps only). Python parity remains a follow-up — `OpenAIRealtime2`
is still TS-only.

### Added — `OpenAIRealtime2` engine for `gpt-realtime-2` on the GA Realtime API (TypeScript only)

The 0.6.1 enum entry for `gpt-realtime-2` advertised parity with the existing
v1 Realtime adapter ("accepts the same v1 `session.update` wire shape so it
slots into the existing adapter without protocol changes"). That turned out
to be wrong: OpenAI promoted `gpt-realtime-2` to the **GA Realtime API**,
which (a) rejects the legacy `OpenAI-Beta: realtime=v1` header with
`invalid_model`, (b) requires `session.type === "realtime"` at the root of
`session.update`, (c) renames `modalities` → `output_modalities`, (d) nests
audio config under `session.audio.{input,output}` with MIME `type` strings
(`audio/pcmu`, `audio/pcma`, `audio/pcm`) instead of v1 enums (`g711_ulaw`,
`g711_alaw`, `pcm16`), and (e) renames the audio-delta event family from
`response.audio.*` / `response.audio_transcript.*` to
`response.output_audio.*` / `response.output_audio_transcript.*`. Going
through the v1 `OpenAIRealtime` engine with `model: "gpt-realtime-2"`
either timed out at `connect()` or completed the call with zero audio
forwarded to Twilio/Telnyx (events fell through to the no-op branch of
the v1 dispatcher).

New `OpenAIRealtime2` engine marker + `OpenAIRealtime2Adapter` subclass:

- **Separate engine marker.** `kind: "openai_realtime_2"`. The legacy
  `OpenAIRealtime` engine continues to serve `gpt-realtime`,
  `gpt-realtime-mini`, `gpt-realtime`, `gpt-4o-realtime-preview`, and
  `gpt-4o-mini-realtime-preview` against the v1-beta endpoint byte-for-byte
  unchanged; nothing in that path is touched.
- **`OpenAIRealtime2Adapter` extends `OpenAIRealtimeAdapter`.** Overrides
  only `connect()` (omits the beta header + sends the GA `session.update`
  payload) and `sendFirstMessage()` (uses `output_modalities`, re-injects
  `audio.output.voice` because the GA `response.create` does NOT inherit
  it from session, and forces `reasoning: { effort: "minimal" }` for the
  literal "say exactly X" greeting so TTFB is bounded by audio generation
  rather than the session-level reasoning tier). Everything else
  (`sendAudio`, `cancelResponse`, `sendText`, `sendFunctionResult`,
  heartbeat) is inherited unchanged.
- **WS-level event translation shim.** Wraps `ws.emit` to rewrite the
  incoming `type` field for the renamed events
  (`response.output_audio.{delta,done}` →
  `response.audio.{delta,done}`; same for `output_audio_transcript`)
  before the parent dispatcher sees the frame. Payloads are byte-identical
  so no further changes are needed in `StreamHandler`, metrics, or the
  dashboard.

Selection becomes opt-in: `phone.agent({ engine: new OpenAIRealtime2({ reasoningEffort: "low" }) })`.
Default model is `gpt-realtime-2`. Passing the GA marker to `Patter.agent`
auto-resolves `provider = "openai_realtime"` so the rest of the pipeline
(metrics, dashboard, cost line) treats the call identically to a v1
Realtime call.

Implementation: a handful of `private readonly` fields on the v1 adapter
(`apiKey`, `model`, `voice`, `instructions`, `tools`, `audioFormat`,
`options`, `ws`, `armHeartbeatAndListener`) were promoted to `protected`
so the subclass can reuse the heartbeat + message dispatch. No public
surface changed; both adapters still expose the exact same method set.

Files: `libraries/typescript/src/providers/openai-realtime-2.ts` (new,
~190 lines), `libraries/typescript/src/engines/openai-2.ts` (new, ~75
lines), `libraries/typescript/src/providers/openai-realtime.ts` (visibility
bumps only), `libraries/typescript/src/client.ts` (instanceof dispatch),
`libraries/typescript/src/server.ts` (`buildAIAdapter` selects the new
adapter when `engine.kind === "openai_realtime_2"`),
`libraries/typescript/src/types.ts` (engine union widened),
`libraries/typescript/src/index.ts` (re-export). Python parity is a
follow-up — `OpenAIRealtime2` is TS-only in this commit, the daily
`docs-feature-drift` job will flag it.

Verified end-to-end on a real Twilio PSTN call:
`Call ended: ... (13.6s, 3 turns, cost=$0.0255, p95 wait=642ms,
engine=openai_realtime_2)` — `firstMessage` plays in the configured voice
(`alloy`), language follows `systemPrompt`, audio flows both directions.

### Fixed — Dashboard MetricsPanel: Latency/Cost tabs render at the same height

Switching the MetricsPanel tabs between **Latency** and **Cost** caused a
visible vertical jump because each layout had a different natural height —
Latency (pipeline mode) renders 4 latency cards + a 3-row waterfall +
legend (~230 px), while Cost renders only the cost bar + 4-6 stack rows
(~180 px). The card outer height changed by ~50 px on every toggle.

Wrapped the tab content in a ``.metrics-panel-body`` container with
``min-height: 240px`` — sized to the tallest layout (pipeline Latency).
Both tabs now occupy exactly 321 px outer (body 240 px) and the tab
switch is purely a content swap.

Files touched:
  dashboard-app/src/components/MetricsPanel.tsx
  dashboard-app/src/styles/dashboard.css
  libraries/python/getpatter/dashboard/ui.html (resynced bundle)
  libraries/typescript/src/dashboard/ui.html (resynced bundle)

### Added — Dashboard: select & soft-delete calls (logs preserved as backup)

Operators can now select one or more calls in the dashboard call list and
remove them from the view + rolling metrics. The on-disk artefacts written
by ``CallLogger`` (``<log_root>/calls/YYYY/MM/DD/<call_id>/metadata.json``
and ``transcript.jsonl``) are intentionally NOT touched — they remain as a
durable backup that the operator can audit or re-import outside the
dashboard.

Behaviour:

- Soft-deleted ``call_id``s are excluded from ``get_calls`` / ``get_call`` /
  ``get_aggregates`` / ``get_calls_in_range`` / ``call_count``. The "Avg
  latency p95" and "Spend" cards recompute against the visible set, so the
  numbers always match what the operator sees in the table.
- Active calls are never deletable; a mid-call delete from the UI is
  silently skipped server-side so the live-transcript pane cannot be
  orphaned.
- The deleted set persists to ``<log_root>/.deleted_call_ids.json`` (atomic
  write). On process restart ``hydrate()`` reloads the set so previously
  deleted calls stay hidden, while the on-disk metadata is left intact.

API additions (parity across SDKs):

- ``DELETE /api/dashboard/calls/:call_id`` — remove one.
- ``POST /api/dashboard/calls/delete`` with ``{"call_ids": [...]}`` — batch.
- SSE event ``calls_deleted`` with payload ``{ "call_ids": [...] }`` so
  other tabs / external clients re-render immediately.

Store-level API:

- ``MetricsStore.delete_calls(call_ids)`` / ``deleteCalls(callIds)``
- ``MetricsStore.is_deleted(call_id)`` / ``isDeleted(callId)``
- ``MetricsStore.get_deleted_call_ids()`` / ``getDeletedCallIds()``

UI: the call table gains a checkbox column (live rows disabled). Selecting
≥1 row reveals a bulk-action bar with a clear-selection ghost button and a
peach destructive "Delete" button gated by an inline confirmation step that
explains the on-disk logs are preserved.

Files touched:
  libraries/typescript/src/dashboard/store.ts (deletedCallIds + filters)
  libraries/typescript/src/dashboard/routes.ts (DELETE + batch POST)
  libraries/python/getpatter/dashboard/store.py (parity)
  libraries/python/getpatter/dashboard/routes.py (parity)
  dashboard-app/src/components/CallTable.tsx (multi-select + bulk bar)
  dashboard-app/src/components/icons.tsx (IconTrash / IconCheck / IconX)
  dashboard-app/src/styles/dashboard.css (checkbox + bulk-bar styles)
  dashboard-app/src/hooks/useDashboardData.ts (calls_deleted SSE +
    removeCallsLocal optimistic update)
  dashboard-app/src/lib/api.ts (deleteCalls client)
  dashboard-app/src/App.tsx (wiring)
  CHANGELOG.md
  tests: dashboard-store delete coverage (TS + Py).

### Fixed — One-shot barge-in: VAD now reset between agent turns

After a successful barge-in on PSTN (no-AEC), subsequent barge-in attempts in the
same call silently failed. Root cause: PSTN echo of the agent's TTS played back
through the caller's phone speaker and returned through the mic, keeping
SileroVAD's smoothed probability above `deactivationThreshold` (0.35) for the
entire agent turn. The detector's `pubSpeaking` / `_pub_speaking` state stayed
`true` across turns, so the next user utterance never produced a fresh
`SILENCE → SPEECH` transition and `speech_start` never fired — barge-in
behaved as if it were "one shot".

Fix: added an optional `reset()` hook to the `VADProvider` interface
(TypeScript) / abstract base class (Python). `SileroVAD` implements it by
clearing the pending buffer, `pubSpeaking`, the speech/silence threshold
durations, the exponential smoothing filter, AND the ONNX model's RNN hidden
state + rolling context. `StreamHandler` invokes the reset in two places:

  1. **`beginSpeaking()` / `_begin_speaking()`** — every new agent turn starts
     with a clean VAD. The user's previous utterance has already been
     committed by STT so no audio is lost.
  2. **`endSpeakingWithGrace()` grace-timer fire** — natural turn end leaves
     VAD ready for the next spontaneous user utterance.

Failures in the optional `reset()` hook are logged and swallowed; a flaky
reset can never silently kill barge-in for the rest of the call.

Parity bonus: `_begin_speaking()` in Python now stamps `_first_audio_sent_at`
unconditionally (matching TypeScript `beginSpeaking()` since 2026-05-11). The
`is_first_message` parameter is kept for backward compat with callers but no
longer changes behaviour. Without this, a turn with a slow LLM was
un-interruptible for the entire LLM TTFT window because the barge-in gate
anchor stayed `None`.

Files: `libraries/typescript/src/types.ts`,
`libraries/typescript/src/providers/silero-vad.ts`,
`libraries/typescript/src/stream-handler.ts`,
`libraries/python/getpatter/providers/base.py`,
`libraries/python/getpatter/providers/silero_onnx.py`,
`libraries/python/getpatter/providers/silero_vad.py`,
`libraries/python/getpatter/stream_handler.py`.

### Fixed — Barge-in gate reduced 250 ms → 100 ms; suppressed speech flushed to STT on grace end

Two related barge-in defects on Twilio PSTN (no-AEC path):

1. **Gate too long.** `MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_NO_AEC` was 250 ms, blocking
   every `speech_start` VAD event for the first 250 ms after the agent began speaking.
   On short agent turns (< ~400 ms of audio) the gate expired only near the end of the
   turn, so the user's interruption was silently suppressed. Reduced to 100 ms, which is
   still enough to block PSTN echo loopback (~100–200 ms round-trip) while letting genuine
   user speech through on typical responses.

2. **Suppressed speech silently discarded.** When VAD fired `speech_start` during the
   agent's turn but barge-in was gate-suppressed, the user's audio accumulated in
   `inboundAudioRing` / `_inbound_audio_ring` but was never flushed. The ring is cleared
   at `beginSpeaking` / `_begin_speaking` (start of the next agent turn), so the user's
   words vanished without ever reaching STT. Added `suppressedSpeechPending` /
   `_suppressed_speech_pending` flag: set when speech_start is suppressed, cleared on
   barge-in or new turn, and on grace-timer expiry the ring is flushed to STT so the
   user's message is processed.

Files: `libraries/typescript/src/stream-handler.ts`,
`libraries/python/getpatter/stream_handler.py`.

### Fixed — StatefulResampler FIR cold-start transient on first TTS chunk

`StatefulResampler` (TypeScript, `libraries/typescript/src/audio/transcoding.ts`) seeded
its 5-tap FIR history with `input[0]` on the first call. When ElevenLabs HTTP streaming
delivers an audio chunk that starts at non-zero amplitude, this produced a startup
transient on the resampled output — audible as a brief crackle at the beginning of the
first TTS message. Fixed: FIR history is now seeded with zeros (the correct initial
condition for a filter that has received no prior input), eliminating the transient.
No Python equivalent — Python uses `scipy.signal.resample_poly` which handles boundary
conditions internally.

### Fixed — First-message crackling on Twilio PSTN: streaming path now uses simple sendAudio

Root cause: the streaming first-message path (non-prewarm) was routing every
ElevenLabs HTTP chunk through `sendPacedFirstMessageBytes` /
`_send_paced_first_message_bytes`. That function was designed for the prewarm
case (one large pre-synthesised buffer) and resets drain+counter state on each
call. Applied per streaming chunk (~128 ms each), the drain+reset destroyed
mark back-pressure continuity and the per-sub-chunk playout sleep slowed
delivery below Twilio's playout rate, causing periodic buffer underruns
(crackling on the first message only). Subsequent LLM responses used the
simpler `synthesizeSentence` / `_synthesize_sentence` path (plain `sendAudio`)
and never crackled, confirming the fix direction.

Fix: the streaming first-message path now uses the same plain
`encodePipelineAudio + sendAudio + markFirstAudioSent` pattern as subsequent
turns. The prewarm path (pre-synthesised buffer) is unchanged and still uses
`sendPacedFirstMessageBytes` / `_send_paced_first_message_bytes` because that
buffer can be several seconds long and needs mark-gated pacing. Files:
`libraries/typescript/src/stream-handler.ts`,
`libraries/python/getpatter/stream_handler.py`.

### Changed — Cerebras usage-chunk fallback: INFO-once + DEBUG per iteration (Python + TypeScript parity)

The char/4 fallback billing path in `services/llm_loop.py` /
`src/llm-loop.ts` previously emitted `logger.warning` /
`getLogger().warn` on every tool-loop iteration when the upstream
provider stream did not include a `usage` chunk. On Cerebras (the
common case for this fallback), a multi-tool turn could log 5-10
identical WARN lines for the same call — drowning real warnings.

Replaced with: first fallback in the call → INFO (so operators
still see it once with the full diagnostic context — `provider`,
`model`, `input_chars`, `output_chars`, `est_input_tokens`,
`est_output_tokens`); subsequent iterations → DEBUG with the
iteration index and a per-LLMLoop `_usage_missing_count` /
`_usageMissingCount` total so the volume is still visible at
DEBUG level. No behavioural change — billing still uses char/4
estimation. Files: `libraries/python/getpatter/services/llm_loop.py`,
`libraries/typescript/src/llm-loop.ts`.

### Changed — Krisp VIVA TypeScript scaffold: refreshed unavailability message (2026-05)

The `KrispVivaFilter` constructor in
`libraries/typescript/src/providers/krisp-filter.ts` already throws
with guidance because Krisp does not publish a Node.js server SDK as
of 2026-05. Refreshed the message to include the verification date,
explicitly distinguish "server Node SDK" from existing browser/RN
third-party wrappers, and note that those wrappers (browser WASM and
mobile client variants) are scoped to local microphone capture and
cannot process Patter's server-side PCM/mulaw audio. Python
`KrispVivaFilter` and TS `DeepFilterNetFilter` remain the only
shipped paths. No code behaviour change.

### Fixed — Barge-in gate regression test: prewarmed first message must remain interruptible

Locked in with parity tests on both SDKs that `_stream_prewarm_bytes` / `streamPrewarmBytes` open the barge-in gate (`_first_audio_sent_at` / `firstAudioSentAt`) once the first chunk reaches the wire. The gate was already opened by `_begin_speaking(is_first_message=True)` ahead of streaming, but a future refactor of the `_begin_speaking` path could regress the prewarm path silently — the per-chunk `_mark_first_audio_sent` call inside the streaming loop is the last line of defence and now has explicit coverage in `test_stream_prewarm_bytes_opens_barge_in_gate_on_first_chunk` (Python) and `opens the barge-in gate by stamping firstAudioSentAt after the first chunk` (TypeScript).

Files: `libraries/python/tests/test_prewarm.py`, `libraries/typescript/tests/unit/prewarm.test.ts`.

### Fixed — `ElevenLabsWebSocketTTS.adopt_websocket` leaked the previous parked WS when called outside an event loop

`ElevenLabsWebSocketTTS.adopt_websocket` (Python) closed any previously parked WS handle via `asyncio.create_task(prev.ws.close())`. When invoked from a sync context with no running event loop — e.g. cleanup hooks fired from `__del__`, atexit handlers, or signal-driven teardown — the `create_task` call raised `RuntimeError` which the code silently swallowed with a bare `except RuntimeError: pass`, leaking the socket FD. ElevenLabs would eventually close the remote side after the inactivity timeout, but the FD on our side stayed allocated until process exit.

The fix keeps the async fast path when a loop is running, and falls back to a best-effort synchronous `transport.close()` (non-blocking, skips the WS close handshake but cleans up the file descriptor) when no loop is available. A warning log is emitted on the fallback path so the FD-leak symptom shifts from "silent" to "logged".

The TypeScript counterpart `adoptWebSocket` is unaffected — `ws.close()` from the `ws` package is synchronous so the same scenario doesn't reach an analogous error branch.

Files: `libraries/python/getpatter/providers/elevenlabs_ws_tts.py`, `libraries/python/tests/unit/test_elevenlabs_ws_tts.py` (new `TestAdoptWebSocketCleanup`).

### Added — `patter.*` OTel attribute helpers in the TypeScript SDK (parity with Python)

The Python SDK ships `record_patter_attrs`, `patter_call_scope`, and `attach_span_exporter` (in `getpatter.observability.attributes`) for stamping `patter.cost.*` / `patter.latency.*` span attributes and wiring an OTel `SpanExporter` into the tracer provider. The TypeScript SDK previously had no equivalent surface — calling code that wanted to record those attributes had to no-op manually or import `@opentelemetry/api` directly, which broke cross-SDK parity per `.claude/rules/sdk-parity.md`.

This change ports the helpers to TypeScript as no-ops by default. When `PATTER_OTEL_ENABLED` is unset or `@opentelemetry/api` is not installed, every helper is a fast no-op, so existing call sites stay zero-cost. Available as:

```ts
import {
  recordPatterAttrs,
  patterCallScope,
  attachSpanExporter,
  DEFAULT_SIDE,
} from 'getpatter/observability';
```

Semantic mapping (1:1 with Python):

- `recordPatterAttrs(attrs)` ↔ `record_patter_attrs(attrs)`
- `patterCallScope({ callId, side }, fn)` ↔ `patter_call_scope(call_id=..., side=...)` (the JS form takes an async callback because JS lacks `with`-style context managers; the closure is the scope body)
- `attachSpanExporter(patterInstance, exporter, { side })` ↔ `attach_span_exporter(patter, exporter, side=...)`

Files: `libraries/typescript/src/observability/attributes.ts` (new), `libraries/typescript/src/observability/index.ts` (re-exports), `libraries/typescript/tests/unit/observability-attributes.test.ts` (new).

### Fixed — `EOUMetrics` field semantics + unit parity between Python and TypeScript SDKs

The Python implementation in `libraries/python/getpatter/services/metrics.py:_emit_eou_metrics` had `end_of_utterance_delay` and `transcription_delay` swapped relative to the TypeScript counterpart, and emitted them in seconds while the TypeScript SDK and the rest of the observability surface (`ttfb_ms`, `turn_ms`) use milliseconds. The dashboard, EventBus subscribers and any downstream exporter consuming both SDKs would have seen the two fields disagree by a factor of 1000× AND swapped — silently corrupting end-of-utterance latency dashboards on cross-SDK fleets.

The convention is now uniform across both SDKs (locked in by tests):

- `end_of_utterance_delay` / `endOfUtteranceDelay` = `stt_final − vad_stopped` (milliseconds)
- `transcription_delay` / `transcriptionDelay` = `turn_committed − vad_stopped` (milliseconds)
- `on_user_turn_completed_delay` / `onUserTurnCompletedDelay` = pipeline hook execution time (milliseconds)

Negative deltas from clock skew or out-of-order timestamps are now clamped to `0` on both sides (the TypeScript side already did this; Python now does too).

Files: `libraries/python/getpatter/services/metrics.py`, `libraries/python/getpatter/observability/metric_types.py` (docstring), `libraries/python/tests/test_metrics.py` (new `TestEOUMetricsEmission`), `libraries/typescript/tests/unit/metrics.test.ts` (new `emitEouMetrics field semantics` block).

### Fixed — Barge-in bug bundle: 6.8s latency outliers, double-talk dispatch, stale anchors, firstMessage uninterruptible (Python + TypeScript parity)

Real PSTN test (round 10f, 11 turns with user-initiated interruptions) surfaced four correlated bugs in the barge-in pipeline that the previous strategy work in 0.6.1 did not cover. Investigation report (`/private/tmp/.../a6fae04df253294f2.output`) traced all four to anchor mismanagement around the interrupt boundary plus an over-aggressive VAD threshold.

**Bug 1 — endpoint_ms == stt_ms == 6818 ms (dishonest p95 outliers).** `recordSttComplete` was fabricating `_endpointSignalAt = _sttComplete` when no legitimate VAD `speech_end` had fired, producing a synthetic anchor that then made `endpoint_ms = _turnCommittedMono − _turnStart` (the entire turn duration). Fix: never fake the anchor; let `endpoint_ms` be `undefined` on the affected turn and increment a `_endpointSignalMissingCount` counter for observability. Added a 100 ms post-barge-in gate (`_lastBargeinAt`): the next turn's `endpoint_ms` / `stt_ms` are dropped from the percentile distribution since post-barge-in anchors are inherently noisy. Files: `libraries/typescript/src/metrics.ts:412-422,538-549,572-596,870-984`, `libraries/python/getpatter/services/metrics.py:289,362,395,696`.

**Bug 2 — double-talk: agent answered the 1st sentence while user said the 2nd.** Deepgram emits `is_final` on any pause > a few ms; the SDK dispatched LLM immediately. Two fixes ship together:

1. **Bumped Silero `minSilenceDuration` default `0.1` → `0.4` s**. The previous 100 ms threshold fired VAD `speech_end` on natural inter-sentence pauses (typically 200-400 ms), which then prematurely finalised the user turn at the STT layer. 0.4 s is the industry-standard default for telephony agents: bridges intra-utterance pauses without delaying single-sentence turns by more than the natural conversational gap. Files: `libraries/typescript/src/providers/silero-vad.ts:366-378`, `libraries/python/getpatter/providers/silero_vad.py:125`.
2. **Synchronous STT-final → LLM dispatch (no debounce)**. An earlier 400 ms debounce attempt (`_scheduleTurnCommit` / `_runDeferredTurnCommit`) was prototyped and rolled back before release: the partial-transcript reschedule branch overwrote the dispatched FINAL text with the latest partial, silently dropping entire user turns during slow-LLM windows. Verified on real PSTN (round 10k, gpt-5-nano: 3 of 5 user turns dropped). The shipped behaviour dispatches on `is_final` immediately; when Deepgram emits two close-together finals like `"What's the"` then `"What's the best?"`, the SDK answers both (benign double-answer) instead of dropping the first (catastrophic). Tracked future improvements documented internally — options include raising Deepgram `endpointingMs` per-agent, queue-cancel semantics, and sentence-segment merge in `commitTranscript`. Files: `libraries/typescript/src/stream-handler.ts` (inline dispatch restored), `libraries/python/getpatter/stream_handler.py` (`_dispatch_turn` called inline from `_stt_loop`).

**Bug 3 — anchors stale after strategy-confirmed barge-in.** `runBargeInCancel` cleared anchors via `_resetTurnState()` but never re-anchored to the next legitimate VAD `speech_start`; the next turn either inherited stale anchors or anchored to the first inbound audio byte (adding ~250 ms ring-buffer delay to every post-barge-in turn). Added `anchorUserSpeechStart()` calls in three places: after `recordTurnInterrupted` in `runBargeInCancel`, and on the pending-barge-in timeout path. Also `_resetTurnState` now resets `_initialTtfbEmitted` (TS) / `_initial_ttfb_emitted` + `_llm_ttfb_emitted` + `_tts_ttfb_emitted` (Py) so EventBus TTFB re-fires after barge-in when `reportOnlyInitialTtfb=true`. Files: `libraries/typescript/src/stream-handler.ts:2034-2058`, `libraries/typescript/src/metrics.ts:846-867`, Python mirrors.

**Bug 4 — firstMessage was uninterruptible by VAD for 300-800 ms.** `canBargeIn()` gates on `firstAudioSentAt !== null`, but that field was only stamped when the first audio chunk arrived from the TTS provider — meaning the 250 ms anti-flicker timer didn't start until the user had already missed the TTFB window. Fix: `beginSpeaking(isFirstMessage=true)` now stamps `firstAudioSentAt = Date.now()` synchronously, so the gate timer runs in parallel with TTS TTFB. The firstMessage TTS loop already breaks on `!this.isSpeaking`, so user speech now propagates cancellation correctly. Files: `libraries/typescript/src/stream-handler.ts:357,1477`, `libraries/python/getpatter/stream_handler.py:3187,2043`.

### Changed — Dashboard percentile threshold lowered 5 → 2 turns

`LatencyPanel` and `MetricsPanel` displayed `—` for `p50` / `p95` until a call had ≥5 turns. On most PSTN calls (typically 4-7 turns) the detail pane showed dashes while the call-list `P95 LATENCY` column already showed a real number via `avg` fallback — confusing for users comparing the two surfaces. Lowered to 2 turns so the detail pane matches the list column. With n=2 the percentile is statistically thin but consistent with what the list shows.

Files: `dashboard-app/src/components/LatencyPanel.tsx:12`, `dashboard-app/src/components/MetricsPanel.tsx:76,127`. Bundle synced to `libraries/{typescript,python}/.../dashboard/ui.html` via `dashboard-app/scripts/sync.mjs`.

### Added — Krisp VIVA noise-suppression scaffold for TypeScript SDK

Mirrors the Python `KrispVivaFilter` API at `libraries/typescript/src/providers/krisp-filter.ts` for cross-SDK parity. Class signature accepts the same options (`modelPath`, `noiseSuppressionLevel`, `frameDurationMs`, `sampleRate`) but throws at construction time with guidance — Krisp does not publish an official Node.js SDK as of 2026-05. Opt-in, proprietary, license required, no default-on. Patter ships only the interface; users supply SDK + `.kef` model.

Available paths today:
1. **Python SDK**: `from getpatter.providers.krisp_filter import KrispVivaFilter` — fully implemented (existed prior to this change, unmodified). Requires `pip install getpatter[krisp]` + `KRISP_VIVA_SDK_LICENSE_KEY` + `KRISP_VIVA_FILTER_MODEL_PATH`.
2. **TypeScript today**: `new DeepFilterNetFilter({ modelPath })` from `getpatter` — community ONNX export, no license. `KrispVivaFilter` throws until a Node binding is available.

New top-level exports from `getpatter`: `KrispVivaFilter`, `KrispVivaFilterOptions`, `KrispSampleRate`, `KrispFrameDuration`, `DeepFilterNetFilter`, `DeepFilterNetOptions`. The TS scaffold closes when an official Krisp Node SDK ships or a community NAPI/WASM binding becomes available.

### Fixed — Dashboard live SSE update wiped transcripts + latency from prior calls

When a new call started, the live SSE refresh in
`dashboard-app/src/hooks/useDashboardData.ts:103` rebuilt the entire
calls array from `mergeCalls(active, recent)` without consulting the
previous state. If the new payload had any field as undefined — common
when the server-side `MetricsStore.updateCallStatus` writes a synthetic
"terminal" record with `metrics: undefined` ahead of the true
`recordCallEnd` — the prior call lost its transcripts and latency p50/
p95 in the UI. Added `mergeCallPreserving(prev, next)` that does
`next.field ?? prev.field` per critical field, masking the lossy
secondary records server-side. The SDK-side double-write race in
`libraries/typescript/src/dashboard/store.ts:134-186` is flagged with a
TODO for 0.6.2.

### Changed — `elevenlabs.TTS` facade now defaults to WebSocket streaming (Python + TypeScript parity)

Industry best practice for telephony agents is a long-lived WS connection — every other Patter STT adapter (Deepgram nova-3, Cartesia ink-whisper, Whisper streaming, AssemblyAI) already runs on persistent WS. ElevenLabs TTS was the outlier: the default `elevenlabs.TTS()` facade opened a fresh HTTP POST per sentence (TLS handshake, DNS, full request setup repeated on every turn), producing measured TTFB p50 ~265 ms on PSTN — vs ElevenLabs's published server-side TTFT of ~75 ms. The gap was almost entirely HTTP setup, not synthesis.

Flipped both SDKs to extend the WebSocket class (`_ElevenLabsWebSocketTTS` / `_ElevenLabsTTSWebSocket`) by default. Expected TTFB p50 drop: ~265 ms → ~80-100 ms (after the first turn pays one handshake; turn 2+ reuses the open WS).

- TS: `libraries/typescript/src/tts/elevenlabs.ts` — `class TTS extends _ElevenLabsWebSocketTTS`. Default `voiceId="EXAVITQu4vr4xnSDxMaL"`, `modelId="eleven_flash_v2_5"`, `outputFormat="pcm_16000"`, `autoMode=true`. `providerKey` flipped `"elevenlabs"` → `"elevenlabs_ws"`. `for_twilio`/`for_telnyx` signatures unchanged.
- Py: `libraries/python/getpatter/tts/elevenlabs.py` — `class TTS(_ElevenLabsWebSocketTTS)` with matching defaults. `chunk_size` kept as tolerated-but-ignored kwarg (the WS path doesn't use it) to avoid breaking pinned callers.
- Compatibility aliases preserved: `elevenlabs_ws.TTS` (TS + Py) now re-exports from `elevenlabs.TTS`. `ElevenLabsWebSocketTTS` top-level symbol unchanged.

**REST opt-out** — new top-level export `ElevenLabsRestTTS` in both SDKs. Use when:
- The free / starter tier (WS requires Pro plan; the WS class raises `PLAN_REQUIRED_MSG` directing callers to `ElevenLabsRestTTS`).
- The `eleven_v3` model (HTTP-only — the WS class rejects it at construction with the same redirect).

```ts
// TS
import { ElevenLabsRestTTS } from "getpatter";
const tts = new ElevenLabsRestTTS(process.env.ELEVENLABS_API_KEY!);
```
```python
# Python
from getpatter import ElevenLabsRestTTS
tts = ElevenLabsRestTTS(api_key=os.environ["ELEVENLABS_API_KEY"])
```

Dashboard label-prettifier extended in `dashboard-app/src/components/{CostPanel,MetricsPanel}.tsx` — `titleCase()` regex now strips `_ws` / `_rest` transport suffixes in addition to `_stt` / `_tts` / `_llm` role suffixes. `"elevenlabs_ws"` now renders as "Elevenlabs" without the suffix bleed into UI. Repeated `+` handles compound suffixes (`"cartesia_tts_ws"` → `"Cartesia"`).

Provider error messages in `providers/elevenlabs-ws-tts.{ts,py}` updated: `payment_required` and `eleven_v3` rejections now direct users to `ElevenLabsRestTTS` (was `ElevenLabsTTS` — which is now the WS facade itself, making the previous text recursive).

Tests: 173 Python (was 170) + 95 TS (was 93) pass. 1 REST-specific assertion in `test_tts_facade_language.py` migrated to `ElevenLabsRestTTS` (the `chunk_size == 4096` default). 2 new tests each side verify the flip semantics and the opt-out is not aliased.

Acceptance matrix: 25 duplicate `outbound-*-elevenlabs-ws.ts` scenarios removed (now functionally identical to `outbound-*-elevenlabs.ts`). Added 1 explicit regression `outbound-deepgram-cerebras-elevenlabs-rest.ts` to keep the REST path exercised. `_manifest.json` updated (78 entries).

Migration: **0 code changes** for callers using the default — they automatically benefit from the latency drop. **1-line import rename** for callers who deliberately want HTTP REST (`ElevenLabsTTS` → `ElevenLabsRestTTS`).

### Fixed — Console "Call ended" summary log p95 used `total_ms`, not `agent_response_ms` (Python + TypeScript parity)

The single-line `[PATTER] Call ended: ... p95=Xms` log emitted by `stream_handler.ts` (TS) and `telephony/{twilio,telnyx}.py` (Py) at the end of every call read `latency_p95.total_ms` — the round-trip duration that **includes** how long the user spoke (`user_speech_duration_ms`), not the system-controlled wait time. The metrics module itself flags this in `metrics.ts:85-89`: "Unlike `total_ms` (which spans the user's entire utterance and therefore grows with how long the user spoke), `agent_response_ms` isolates the system-controlled latency."

The dashboard already shows the correct field — `agent_response_ms` — under the **"p95 wait"** tile (`LatencyPanel.tsx:48`, `mappers.ts:221`). The console log diverged: a 51.6 s, 7-turn call where the user spoke ~1.2 s/turn printed `p95=2577ms` while the dashboard showed `p95 wait=1361ms` for the same call. The 1361 ms is the genuine user-perceived wait; the 2577 ms confused users into thinking the SDK was slow.

Switched both SDKs to read `latency_p95.agent_response_ms` (fallback `total_ms` for legacy short calls where the percentile isn't computed) and renamed the label to **`p95 wait=Xms`** to match the dashboard tile word-for-word. Files: `libraries/typescript/src/stream-handler.ts:2810-2820`, `libraries/python/getpatter/telephony/twilio.py:657-680`, `libraries/python/getpatter/telephony/telnyx.py:792-810`.

### Fixed — Telnyx pricing direction-aware: inbound 2× over-bill resolved

Audited against https://telnyx.com/pricing/elastic-sip (verified
2026-05-11). The previous flat `"telnyx": $0.007/min` over-billed
inbound calls by 2× ($0.0035 real) and approximately matched outbound
($0.005-0.009 range). Split into two entries:
- `telnyx_inbound`: $0.0035/min (US local termination)
- `telnyx_outbound`: $0.007/min (Pay-As-You-Go mid-range)
The legacy `telnyx` key is preserved at $0.007 for backward-compat with
users who override `pricing={"telnyx": {...}}` and don't know direction.
Billing granularity confirmed per-minute (not per-second as previous
internal docs claimed). Files: `libraries/python/getpatter/pricing.py`,
`libraries/typescript/src/pricing.ts`. Tests added at
`libraries/python/tests/test_pricing.py`,
`libraries/typescript/tests/pricing.test.ts`.

### Fixed — Python Twilio STT cost 4× over-bill (sample rate / bytes-per-sample mismatch)

`libraries/python/getpatter/telephony/twilio.py` configured the metrics
STT format as `(sample_rate=8000, bytes_per_sample=1)` (mulaw 8 kHz),
but `stream_handler.py` already decodes the inbound mulaw to PCM16
@ 16 kHz before feeding bytes to `metrics.add_stt_audio_bytes()`. With
the inverted format, every 60 s of real audio was reported as 240 s and
billed 4× the true cost ($0.0192 instead of $0.0048 against Deepgram
Nova-3 at $0.0048/min). TypeScript was unaffected (default 16000/2 was
never overridden); Python Telnyx was unaffected (already configured 16000/2).
Fix: `configure_stt_format(sample_rate=16000, bytes_per_sample=2)` in
the Twilio adapter, plus a regression test asserting 1.92 MB of PCM16
bytes = 60 s of audio. Customers were over-billed; refund window TBD.

### Fixed — Dashboard "−$X cached" badge dead for Realtime prompt-caching savings

The SDK emits `cost.llm_cached_savings` (Realtime / Anthropic prompt
caching discount) but `dashboard-app/src/lib/mappers.ts:computeCost()`
never read it, so `Call.cost.cached` was always undefined and the badge
in `CostPanel.tsx:64` never rendered. Wired the field through
`api.ts:CallCost` (added `llm_cached_savings?: number` + `parseCost`)
and the mapper now populates `result.cached`. The "−$0.00X cached" line
now appears next to the LLM cost row whenever a Realtime call has any
cached-token savings.

### Changed — LLM usage-chunk char/4 fallback log bumped DEBUG → WARN (Python + TypeScript)

The 0.6.1 char/4 fallback (added when Cerebras was observed dropping
the `usage` chunk on some streams) was logging at DEBUG, so silent-zero
incidents only showed up in dev runs. Bumped to WARN in
`libraries/python/getpatter/services/llm_loop.py` and
`libraries/typescript/src/llm-loop.ts` so production observability
surfaces it. Message: "LLM usage chunk missing from {provider}/{model};
estimating output_tokens=N via char/4 fallback".

### Fixed — Deepgram STT pricing reflected legacy standard rate, not the current PAYG promo (Python + TypeScript parity)

Audited against https://deepgram.com/pricing (verified 2026-05-11). Deepgram is currently running a "Limited-time promotional rates on streaming" tier that customers actually pay today; the prior $0.0077/min Nova-3 figure was the launch-era standard rate that has been struck through on the public page.

| Model | Old (USD / min) | New (USD / min) | Notes |
|-------|-----------------|-----------------|-------|
| `nova-3` (default) | $0.0077 | **$0.0048** | over 60% |
| `nova-3-multilingual` | $0.0092 | **$0.0058** | over 58% |
| `flux` (added) | — | **$0.0065** | Flux English; new event-driven STT (2026) |
| `flux-english` (added) | — | **$0.0065** | alias of `flux` |
| `flux-multilingual` (added) | — | **$0.0078** | new |
| `nova-2`, `nova`, `whisper-*` | unchanged | unchanged | legacy / non-Nova-3 tiers |

Dropped the `"deepgram"` provider-level default from $0.0077 to $0.0048 (Nova-3 monolingual is the Patter default model). A 25-minute call against the default would have reported $0.1925 in `cost.stt` instead of the actual $0.12 — over-reporting by ~60%. Customers were never undercharged; the dashboard line item was wrong. Files: `libraries/python/getpatter/pricing.py`, `libraries/typescript/src/pricing.ts`. Tests updated at `libraries/python/tests/test_pricing.py`, `libraries/typescript/tests/pricing.test.ts`, and the matching soak tests. Revisit when Deepgram removes the promo banner.

### Fixed — ElevenLabs pricing table overcharged Flash 20% and Multilingual v2 / v3 by 80-200% (Python + TypeScript parity)

Audited against the canonical public API pricing page at https://elevenlabs.io/pricing/api (verified 2026-05-11). The per-1K-character API/overage rate is flat across all plan tiers (Free → Business); only the included character bundle varies. Patter's 2026-05 table reflected legacy Creator-plan overage figures and a launch-era v3 quote that have since been consolidated.

| Model | Old (USD / 1K chars) | New (USD / 1K chars) | Notes |
|-------|----------------------|----------------------|-------|
| `eleven_flash_v2_5` | $0.06 | **$0.05** | Patter default; was 20% over |
| `eleven_turbo_v2_5` | $0.05 | $0.05 | unchanged ✓ |
| `eleven_multilingual_v2` | $0.18 | **$0.10** | was 80% over |
| `eleven_v3` | $0.30 | **$0.10** | grouped with multilingual v2 on the public page; was 200% over |
| `eleven_monolingual_v1` (legacy) | $0.18 | **$0.10** | matches multilingual tier |

Also dropped the `"elevenlabs"` / `"elevenlabs_ws"` provider-level default from $0.06 to $0.05 (flash_v2_5 is the Patter default model). A 5-turn call against the default would have reported $0.000060/char × N chars instead of the actual $0.000050/char — over-reporting LLM-bill-equivalent TTS cost by 20%. Customers were never undercharged, but the dashboard cost line was wrong. Files: `libraries/python/getpatter/pricing.py`, `libraries/typescript/src/pricing.ts`. Tests updated at `libraries/python/tests/test_pricing.py`, `libraries/typescript/tests/pricing.test.ts`, plus the matching soak tests.

### Fixed — Dashboard cost labels leaked provider-key suffix (`Cartesia_stt STT` → `Cartesia STT`)

The Cost panel's `titleCase()` helper rendered raw SDK `provider_key` literals (e.g. `cartesia_stt`, `elevenlabs_tts`) which the SDK uses to disambiguate provider-class lookups. The `_stt` / `_tts` / `_llm` suffix is internal noise: the panel already shows the role label next to the swatch ("STT", "TTS", "LLM"), so the suffix duplicated context and produced strings like "Cartesia_stt STT · ink-whisper". Stripped the suffix in both `dashboard-app/src/components/CostPanel.tsx` and `dashboard-app/src/components/MetricsPanel.tsx` `titleCase()` so labels render "Cartesia STT · ink-whisper" / "Elevenlabs TTS · eleven_flash_v2_5".

### Fixed — Phantom `speech_start` during agent TTS contaminated turn anchors (Python + TypeScript parity)

A real PSTN call surfaced `user_speech_duration_ms` of 5-7 seconds for utterances the caller actually spoke in ~1 second. Forensic timeline reconstruction (`releases/0.6.0/typescript/call-logs/.../CA6d7fc612...`) pinned the contamination to two bug classes uncovered by parallel-agent audit (forensic + architect + adversarial + provider-reviewer agreement):

1. **Phantom-speech-start anchor contamination** — `StreamHandler` called `metrics.start_turn_if_idle()` on EVERY VAD `speech_start` event, including the ones suppressed during the per-turn warmup gate (`_can_barge_in() == False`). With AEC enabled this is a ~1 s window; without AEC it is the 250 ms anti-flicker margin. Background noise / echo / agent self-loopback during that window emitted a `speech_start` that was correctly suppressed for the barge-in path BUT silently stamped `_turn_start` at the bleed-through instant. The legitimate user `speech_start` that fired seconds later then no-op'd because `start_turn_if_idle` only acts when `_turn_start is None`. Result: `user_speech_duration_ms = (endpoint_signal_at − stale_turn_start) * 1000`, often 5-7 s.

2. **Stale `_endpoint_signal_at` across dropped final transcripts** — when a final transcript arrived but `commitTranscript` / `_commit_transcript` returned False (dedup window / rejected barge-in / `afterTranscribe` veto, e.g. the "Okay." swallow on a strategy-pending barge-in), the previously-stamped VAD-end anchor was never cleared. The NEXT legitimate utterance inherited that stale anchor, so its `endpoint_ms` measured the silence gap between the dropped utterance and the real one.

Both classes fixed with a single new metrics primitive and two call-site swaps:

- **`anchor_user_speech_start()` (Python) / `anchorUserSpeechStart()` (TypeScript)** — Pipecat-style "every legitimate VAD `speech_start` re-anchors the turn pre-commit". Resets `_turn_start`, `_endpoint_signal_at`, `_vad_stopped_at`, `_stt_final_at`, `_stt_complete`, `_llm_first_token`, and the TTFB-emitted guard. No-ops once `_turn_committed_mono` is set (post-commit barge-ins follow the existing `record_turn_interrupted` path). Files: `libraries/python/getpatter/services/metrics.py`, `libraries/typescript/src/metrics.ts`.

- **`stream_handler.py` / `stream-handler.ts` VAD `speech_start` handler** — explicit `phantom_suppressed` boolean gates ALL metrics state mutation: suppressed events log only, legitimate events call `anchor_user_speech_start()` instead of the old `start_turn_if_idle()`. The strategy-pending barge-in branch also switched from `start_turn_if_idle` to the new primitive so re-anchoring happens consistently on every legitimate `speech_start`.

- **Dropped-final-transcript reset** — when `commitTranscript`/`_commit_transcript` returns False on an `is_final` / `speech_final` transcript, the same `anchor_user_speech_start()` is invoked so the discarded utterance's anchors don't leak into the next turn.

### Fixed — Cartesia STT `finalize()` exposed so VAD `speech_end` can force-flush (Python + TypeScript parity)

The 0.5.5 fast-path at `stream_handler.py:3070-3077` ("on VAD `speech_end`, call `stt.finalize()` so the provider doesn't wait for its natural-pause heuristic") was a no-op for Cartesia: `CartesiaSTT` only sent the `finalize` text frame from its private `close()` method on session shutdown, and `getattr(self._stt, "finalize", None)` returned None. The SDK's authoritative VAD silence detection (SileroVAD, 250 ms threshold) was being overridden by Cartesia's conservative internal endpointing (observed 2-7 s on PSTN audio with background hiss).

Added `async finalize()` to both `CartesiaSTT` (Python) and `CartesiaSTT` (TypeScript) that sends the canonical `finalize` text frame on the live WebSocket. The wired-but-no-op fast-path now triggers a deterministic VAD-driven STT finalisation, parity with Deepgram. Files: `libraries/python/getpatter/providers/cartesia_stt.py`, `libraries/typescript/src/providers/cartesia-stt.ts`.

### Fixed — Cerebras pricing table overcharged 1.5-2.4x across multiple models (Python + TypeScript parity)

Audited against the canonical per-model docs pages at `https://inference-docs.cerebras.ai/models/<model>`. Patter's 2026-05-08 table conflated launch-blog quotes with the current "Exploration pricing" banner shown on each model docs page. Corrections:

| Model | Old (in/out) | New (in/out) | Source |
|-------|--------------|--------------|--------|
| `gpt-oss-120b` | $0.85 / $1.20 | $0.35 / $0.75 | inference-docs.cerebras.ai/models/openai-oss |
| `llama3.1-8b` | $0.10 / $0.20 | $0.10 / $0.10 | inference-docs.cerebras.ai/models/llama-31-8b |
| `qwen-3-235b-a22b-instruct-2507` | $1.00 / $1.50 | $0.60 / $1.20 | inference-docs.cerebras.ai/models/qwen-3-235b-2507 |
| `qwen-3-coder-480b` | (missing → $0) | $2.00 / $2.00 | cerebras.ai/blog/qwen3-coder-480b |

Pre-fix a 5-turn pipeline call against the Patter-default `gpt-oss-120b` logged ~$0.000117 in `cost.llm` instead of the actual ~$0.000088 — over-reporting by a factor of ~1.3. Net: customers were never undercharged, but the dashboard line item was wrong (and 50% high for `gpt-oss-120b` specifically). Files: `libraries/python/getpatter/pricing.py`, `libraries/typescript/src/pricing.ts`. Tests updated at `libraries/python/tests/test_pricing.py`.

### Fixed — Dashboard cost rendering flattened sub-cent values to `$0.00` (`fmtCostUSD` adaptive precision)

The dashboard's per-row, per-stack, and aggregate spend tiles all used `toFixed(2)` or `toFixed(3)` for USD rendering. Cerebras `gpt-oss-120b` at $0.0001 / 5-turn-call rounds to `$0.00` under that rule, making the LLM cost line look as if billing was broken when in fact it was working end-to-end (token usage extracted from the streaming `usage` chunk, cost calculated, persisted to `metadata.json` at the correct precision).

Added `fmtCostUSD(value)` helper (`dashboard-app/src/components/format.ts`) with magnitude-adaptive precision: ≥$0.01 → 2 decimals, ≥$0.001 → 3 decimals, ≥$0.0001 → 4 decimals, smaller values → 5 decimals. Applied across all 12 cost render sites (`App.tsx` spend tile, `CallTable.tsx` row total, `Metric.tsx` headline + per-call row, `CostPanel.tsx` × 5, `MetricsPanel.tsx` × 4). A 5-turn Cerebras pipeline call now shows `$0.00012` instead of `$0.00`.

### Fixed — Dashboard latency metrics: real percentiles, correct waterfall, n<5 percentile gate

The "Latency · this call" panel was showing three different numbers labelled wrong:
1. **"p50" was the avg of total_ms**, not the median (`mappers.ts:194` read `latencyP50: latencyAvg.total_ms`).
2. **The "llm" bar in the waterfall was the same fake p50**, double-counting non-LLM time (waterfall `llm = call.latencyP50` instead of `avg(llm_ms)`). The bar was off by ~5x on real PSTN data.
3. **p50/p95 were rendered with as few as 1 turn**, where percentiles are statistical noise (linear interpolation between two samples).

Round-trip `total_ms` also includes the user-utterance duration on the speech-to-speech metric, which over-states user-perceived latency. The dashboard now exposes `agent_response_ms` (wait time after the user stops speaking) as a separate primary metric.

Fixes shipped:
- **SDK serialization** (`libraries/python/getpatter/server.py`, `libraries/typescript/src/server.ts`) — `metadata.json` now persists the full `LatencyBreakdown` per percentile (`avg`, `p50`, `p95`, `p99`) with all components (`stt_ms`, `llm_ms`, `tts_ms`, `total_ms`, `agent_response_ms`, `endpoint_ms`, `user_speech_duration_ms`). The flat `p50_ms / p95_ms / p99_ms` totals are kept for backward-compat with consumers that read only summaries.
- **Dashboard hydrate** (`libraries/python/getpatter/dashboard/store.py`, `libraries/typescript/src/dashboard/store.ts`) — `_metrics_from_top_level` reads the full breakdown when present; falls back to the synthetic single-`total_ms` shim only for legacy metadata that lacked the breakdown.
- **Dashboard UI** (`dashboard-app/src/lib/api.ts`, `mappers.ts`, `components/CallTable.tsx`, `components/LatencyPanel.tsx`, `components/MetricsPanel.tsx`) — `Call` gains `llmAvg`, `turnCount`, `agentResponseP50/P95`. `latencyP50` now reads from `latency_p50.total_ms` (true median); the waterfall `llm` bar uses `llmAvg`. Percentile boxes render `—` and a "n turns — percentiles need ≥5" hint when `turnCount < 5`. The Latency panel adds a `p50 wait / p95 wait` pair sourced from `agent_response_ms`, the user-perceived "time waited after I stopped speaking" metric.

Backward compat: legacy `metadata.json` (no `avg/p50/p95/p99` objects, only flat percentiles) still hydrates — those rows just lack the per-component breakdown in the panel and show `—` for `p50 wait / p95 wait`. No public API change.

### Changed — First-turn cold-start: keep prewarmed WebSockets OPEN and adopt them at call connect (Python + TypeScript parity)

Investigation of live PSTN-pipeline first-turn p95 latency (~3 s observed in production acceptance) showed the existing prewarm pattern (open WS, idle ~250 ms, close) saves only ~50-250 ms — DNS cache + edge-worker pinning at best. The dominant first-turn cost on PSTN pipeline is the synchronous TLS + WS-upgrade + protocol-handshake against STT (~150-400 ms) and TTS (~400-900 ms) when the call starts. Opening + closing a WS does NOT thread `session: <previousTicket>` across `new WebSocket()` calls in Node's `ws` package (and Python's `websockets` library has the same property at the TCP / TLS level), so each fresh open re-pays the full handshake.

Structural fix: the prewarm pipeline now keeps each provider WebSocket OPEN during the carrier ringing window and hands the live socket off to the per-call `StreamHandler` at `start`, skipping the cold handshake entirely on the first turn.

- **`Patter._prewarmed_connections` (Python) / `Patter.prewarmedConnections` (TS)** — new per-call_id cache holding pre-opened, fully-handshaked provider WebSockets. Populated by the new `_park_provider_connections(agent, call_id)` (Py) / `parkProviderConnections(agent, callId)` (TS), which runs in parallel with the carrier-side `initiate_call`. Each parked slot may hold up to three handles (`stt`, `tts`, `openai_realtime`); each is consumed exactly once. A 30 s safety TTL force-closes any slot whose carrier never fires `start`. Drained by `pop_prewarmed_connections(call_id)` on `start` (consumes the handles into the StreamHandler), `close_prewarmed_connections(call_id)` on call-failure paths (no-answer / busy / failed / canceled / AMD voicemail — wired through `_record_prewarm_waste`), and `disconnect()` on Patter teardown. Files: `libraries/python/getpatter/client.py` (cache + park task + helpers + helpers `_safe_close_handle`, `_close_parked_slot`), `libraries/typescript/src/client.ts` (parity, plus the new exported `ParkedProviderConnections` interface).

- **Provider-level `open_parked_connection()` and `adopt_websocket(...)`** shipped on the three streaming providers most-affected by the cold-start cost: `CartesiaSTT`, `ElevenLabsWebSocketTTS`, `OpenAIRealtimeAdapter`. `open_parked_connection` opens the WS, sends the EXACT initial config the live `connect()` / `synthesize()` path sends (BOS frame for ElevenLabs WS, `session.update` round-trip for OpenAI Realtime), then returns the OPEN socket WITHOUT arming any recv / keepalive task — the handle is parked. `adopt_websocket` takes that handle, installs the recv + keepalive plumbing, and hands the live socket back to `StreamHandler` as if `connect()` had just finished. The TTS adapter uses a single-slot adoption queue so the existing `for await (const chunk of agent.tts.synthesizeStream(...))` call site continues to work without signature changes — and the BOS-already-sent flag prevents a protocol error on adoption. Files: `libraries/python/getpatter/providers/{cartesia_stt,elevenlabs_ws_tts,openai_realtime}.py`, `libraries/typescript/src/providers/{cartesia-stt,elevenlabs-ws-tts,openai-realtime}.ts`.

- **`StreamHandler` adopt-or-connect** — the pipeline-mode initialisation path now polls `pop_prewarmed_connections(call_id)` BEFORE `stt.connect()` / TTS firstMessage. When a parked WS is still OPEN it is adopted (logged as `[CONNECT] callId=... source=adopted ms=0`); when the parked WS died between park and adopt (server timeout, network blip), the dead handle is discarded silently and the consumer falls back to a fresh `connect()` (logged as `source=fresh ms=<elapsed>`). When no parked slot exists at all (cache miss, prewarm task slower than carrier ringing, prewarm disabled), the path is byte-identical to the prior cold-start flow — backward-compatible. Realtime adapter adoption (separate `OpenAIRealtimeStreamHandler` code path) ships the API surface but is not yet wired through the realtime stream handler — pipeline mode dominates the affected use case and the realtime wiring is a follow-up. Files: `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/stream-handler.ts`.

### Fixed — Parallelise STT.connect with TTS firstMessage kickoff (Python + TypeScript parity)

Pipeline-mode initialisation previously did `await stt.connect()` then `tts.synthesizeStream(firstMessage)` serially. STT only needs to be ready to receive incoming user audio, not to send the first agent message out — running the two in parallel saves an additional 200-400 ms on the first turn (real cost of a Deepgram / Cartesia / AssemblyAI WS upgrade). The STT receive loop launcher now awaits the deferred connect task before installing the message pump, so a half-open WS never surfaces "Not connected" on the first audio frame. Files: `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/stream-handler.ts`.

### Fixed — Pre-import AEC module at `Patter.serve()` (Python + TypeScript parity)

The acoustic echo canceller (`getpatter.audio.aec.NlmsEchoCanceller`) was lazily imported on the first call when `agent.echo_cancellation=True`, costing ~150-400 ms of dynamic-import compile / link on the hot path. `Patter.serve()` now eagerly imports the module once when `echo_cancellation` is enabled, so the first call sees the cache-warm import. Pure data — no side effects on users who never enable AEC. Files: `libraries/python/getpatter/client.py`, `libraries/typescript/src/client.ts`.

### Fixed — `[PREWARM]` and `[CONNECT]` timing instrumentation (Python + TypeScript parity)

INFO-level log lines added to `_park_provider_connections` and the StreamHandler adopt-or-connect path so operators can attribute first-turn latency to specific providers without strace / packet capture. Format: `[PREWARM] callId=<id> provider=stt ms=<elapsed_to_park>`, `[CONNECT] callId=<id> provider=stt source=adopted|fresh ms=<connect_time>`. Files: `libraries/python/getpatter/client.py` + `stream_handler.py`, `libraries/typescript/src/client.ts` + `stream-handler.ts`.

Tests: `libraries/python/tests/test_prewarm_handoff.py` (6 unit tests — park task invokes `open_parked_connection` on STT + TTS, parked WS stays OPEN past 250 ms, `pop_prewarmed_connections` consumes once, `close_prewarmed_connections` and `_record_prewarm_waste` drain parked sockets, no-op when neither provider supports parking) and `libraries/typescript/tests/unit/prewarm-handoff.test.ts` (6 parity tests). Both suites use authentic real code paths — only the WS handle is stubbed (it has no business in a unit test) — per `.claude/rules/authentic-tests.md`. Defaults preserved: `agent.prewarm` is still `true` by default, all existing tests pass without modification, and providers that do not implement `open_parked_connection` (everything except the three above) fall through to the prior `warmup()`-then-cold-`connect()` flow.

### Fixed — Prewarm-firstMessage cache safety (5 issues, Python + TypeScript parity)

The `Agent.prewarm_first_message` opt-in shipped earlier in 0.6.1 had five edge cases where the TTS bill was paid but the cached bytes either leaked, never reached the wire, or silently wasted spend. Each fix is per FIX number from the parity audit. Defaults preserved across the board: cap = 200 concurrent prewarmed calls, TTL = `ring_timeout + 5 s`. Tests added in `libraries/python/tests/test_prewarm.py` (14 new tests) and `libraries/typescript/tests/unit/prewarm.test.ts` + `libraries/typescript/tests/unit/server-routes.test.ts` (12 new tests).

- **FIX #91 — cache eviction on abnormal hangup.** `_record_prewarm_waste` was only called from `Patter.end_call(call_sid)`. When a call went to `no-answer` / `busy` / `failed` / `canceled` (Twilio) or hit `call.hangup` / AMD voicemail (Telnyx), the `_prewarm_audio[call_id]` entry leaked until the user explicitly invoked `end_call`. Twilio status callback handler now invokes `record_prewarm_waste` for the four abnormal `CallStatus` values and on the AMD `machine_end_*` paths; Telnyx webhook handler now does the same on `call.hangup` (any `hangup_cause`) and on `call.machine.detection.ended` with `result == "machine"`. `_record_prewarm_waste` is now idempotent — the `_prewarm_consumed` set is checked first, so the status callback firing before `end_call` (or vice-versa) does not double-WARN. Files: `libraries/python/getpatter/server.py` (status / AMD / call.hangup branches), `libraries/python/getpatter/client.py` (`_record_prewarm_waste` idempotency guard, `_prewarm_consumed` set, server forwarding), `libraries/typescript/src/server.ts` (parity), `libraries/typescript/src/client.ts` (parity).

- **FIX #92 — race start-vs-prewarm-task → orphan bytes.** When the carrier's `start` event arrived BEFORE the prewarm TTS task completed, `pop_prewarm_audio` returned `None`, the `StreamHandler` correctly fell back to live TTS, BUT the prewarm task continued in the background and eventually wrote bytes to `_prewarm_audio[call_id]` — orphaning them in the cache until `end_call` ran. Combined with FIX #91, every fast-pickup call leaked the prewarm bytes. **Option A (race-guard)** chosen for the minimum-fix bound. New `_prewarm_consumed: set[str]` tracks every `pop_prewarm_audio` call (hit OR miss). The prewarm task checks membership before writing bytes; on race-finish, the bytes are dropped and a WARN names the `call_id` plus byte count so the wasted spend is observable. Option B (200 ms wait window for the synth to land) was rejected as adding more latency-coupled state for marginal recovery. Files: `libraries/python/getpatter/client.py` (`_spawn_prewarm_first_message._run`), `libraries/typescript/src/client.ts` (parity).

- **FIX #93 — `disconnect()` did not clean up prewarm.** Across `serve()` → `disconnect()` → `serve()` cycles within the same `Patter` instance, in-flight `_prewarm_tasks` continued to run (the TTS WebSocket stayed open and billed) and stale `_prewarm_audio` entries leaked. `disconnect()` now cancels every task in `_prewarm_tasks`, `await`s the cancellation via `asyncio.gather(..., return_exceptions=True)` (Py) / `Promise.allSettled` with a 1 s safety timeout (TS), cancels any pending TTL eviction tasks, and clears `_prewarm_audio` + `_prewarm_consumed`. The instance is fully reusable: a follow-up `serve()` sees a clean cache. Files: `libraries/python/getpatter/client.py` (`Patter.disconnect`), `libraries/typescript/src/client.ts` (parity).

- **FIX #94 — Realtime/ConvAI silently waste TTS spend.** `agent.prewarm_first_message=True` paired with `agent.provider="openai_realtime"` or `"elevenlabs_convai"` paid the TTS bill on every outbound call but never streamed the cached bytes — the `StreamHandler` for those modes runs the firstMessage emit through the provider's own audio path, never consulting `pop_prewarm_audio`. `Patter.call` now checks the provider mode at `_spawn_prewarm_first_message` entry; when `provider != "pipeline"` it logs a WARN and refuses to spawn the synth task. Both SDK docstrings (`Agent.prewarm_first_message`, `AgentOptions.prewarmFirstMessage`) updated to document the constraint. Files: `libraries/python/getpatter/client.py`, `libraries/python/getpatter/models.py` (docstring), `libraries/typescript/src/client.ts`, `libraries/typescript/src/types.ts` (JSDoc).

- **FIX #96 — prewarm cache unbounded (memory DoS).** A flood of `Patter.call(...)` invocations (legitimate or attacker-controlled) could pile up tens of MB of orphan TTS bytes that never evicted when the carrier never fired `start`. Two bounds added: (a) **size cap** at `_PREWARM_CACHE_MAX = 200` (Py) / `PREWARM_CACHE_MAX = 200` (TS) concurrent entries (live cache + in-flight synth tasks). When the cap is reached, new prewarm spawns are refused with a WARN and the call still proceeds — only the optimisation is skipped. (b) **TTL eviction**: a per-entry timer scheduled `ring_timeout + _PREWARM_TTL_GRACE_S` (default 5 s) after the synth task completes. If the cache entry is still present when the timer fires, it is dropped and a WARN names the byte count. The timer is cancelled on normal consumption (`pop_prewarm_audio`) and on `_record_prewarm_waste`, so spurious WARNs never fire after a clean drain. Both `_PREWARM_CACHE_MAX` and `_PREWARM_TTL_GRACE_S` (Py) / `PREWARM_CACHE_MAX` and `PREWARM_TTL_GRACE_MS` (TS) are exported as module-level constants for tests and operator visibility. Files: `libraries/python/getpatter/client.py` (cap check + TTL eviction in `_spawn_prewarm_first_message`, `_evict_prewarm_after`), `libraries/typescript/src/client.ts` (parity).

### Changed — Concrete STT/TTS WebSocket prewarm overrides + OpenAI Realtime native warmup (Python + TypeScript parity)

The first prewarm pass (above) shipped LLM HTTPS-GET warmup but left STT and TTS providers on the no-op default. A second look at the cold-start latency budget revealed a priority inversion: an HTTPS GET against an LLM `/models` endpoint warms only DNS + TLS + connection pool — it does NOT prime the inference path itself, while a streaming-STT or streaming-TTS WebSocket pre-handshake (full TLS + auth + initial config exchange) saves 200-500 ms per call on cold start. OpenAI's Realtime API exposes a native warmup primitive (`response.create` with `generate: false`) that prepares request state without billing tokens. This entry rebalances the prewarm pipeline to put the wins where they actually live.

- **STT WebSocket prewarms** — concrete `warmup()` overrides shipped on `DeepgramSTT`, `CartesiaSTT`, and `AssemblyAISTT`. Each opens the streaming WebSocket (full DNS + TLS + auth handshake), idles ~250 ms so the provider edge keeps the session warm in its routing table, then closes cleanly. By the time `connect()` is invoked at call-pickup the resolver and TLS session are hot — net wire time saving of 200-500 ms vs a cold WS open. **Billing safety**: all three providers bill on streamed audio seconds (Deepgram per [pricing](https://deepgram.com/pricing); Cartesia per [STT API reference](https://docs.cartesia.ai/2025-04-16/api-reference/stt/stt); AssemblyAI per [pricing](https://www.assemblyai.com/pricing)). Opening + closing the WebSocket without sending any audio frames does not consume billable seconds. The override docstrings reference the per-provider billing model so future contributors don't accidentally regress this. Files: `libraries/python/getpatter/providers/{deepgram_stt,cartesia_stt,assemblyai_stt}.py`, `libraries/typescript/src/providers/{deepgram-stt,cartesia-stt,assemblyai-stt}.ts`.

- **TTS prewarms — WebSocket and HTTP** — concrete `warmup()` overrides shipped on `ElevenLabsWebSocketTTS` (WS), `CartesiaTTS` (HTTP `/tts/bytes`), and `InworldTTS` (HTTP `/tts/v1/voice:stream`). The ElevenLabs WS variant opens the stream-input WebSocket, sends the protocol-required single-space keepalive `{"text": " "}` so the server creates and warms the session, idles ~250 ms, then closes. The HTTP-only providers (Cartesia, Inworld) issue a lightweight `GET /voices` (Cartesia) or `HEAD` against the streaming base host (Inworld) to warm DNS + TLS + HTTP/2 — smaller win (~50-150 ms) than the WS variant (~200-500 ms) but still real on cold-start calls. **Billing safety**: ElevenLabs bills on synthesised characters delivered via `audio` frames (per [pricing](https://elevenlabs.io/pricing)) — the keepalive primer is the documented session-establishment frame and does NOT commit synthesis (no `flush: true`, no real text). Cartesia `GET /voices` is a free metadata read; Inworld `HEAD` does not invoke the synthesis pipeline. Tests in both SDKs explicitly assert no `flush: true`, no audio frames, and no synthesis POST during warmup. Files: `libraries/python/getpatter/providers/{elevenlabs_ws_tts,cartesia_tts,inworld_tts}.py`, `libraries/typescript/src/providers/{elevenlabs-ws-tts,cartesia-tts,inworld-tts}.ts`.

- **OpenAI Realtime native warmup (`response.create` with `generate: false`)** — concrete `warmup()` override shipped on `OpenAIRealtimeAdapter`. Per OpenAI's documented [websocket-mode warmup pattern](https://developers.openai.com/api/docs/guides/websocket-mode), the canonical warm step on the Realtime API is to open a session and send `response.create` with `response.generate=false` — this prepares the model's request state and primes inference far more effectively than a generic HTTPS GET. Implementation: open the WS, wait for `session.created`, send `{"type": "response.create", "response": {"generate": false}}`, capture the `response.id` from the resulting `response.created` event into `self._prewarm_response_id` (Py) / `this.prewarmResponseId` (TS), then close. The id is stored so a future call can chain it as `previous_response_id` when the chaining path is wired through `connect()`; for now we capture-and-discard, taking half the win (priming the global session state) without the cross-session-state plumbing complexity. **Billing safety**: `response.create` with `generate: false` is documented as a no-token warmup variant that does not invoke the model — no per-token cost is accrued. Files: `libraries/python/getpatter/providers/openai_realtime.py`, `libraries/typescript/src/providers/openai-realtime.ts`.

- **LLM HTTP-GET warmup retained but documented as low-impact** — the existing `OpenAILLMProvider.warmup` (and its Anthropic / Google / Cerebras / Groq subclasses) still issues a 5 s-bounded `GET <base_url>/models` to warm DNS + TLS + connection pool, saving ~150-400 ms on cold start. The docstring on the base implementation now explicitly calls out that an HTTPS GET does NOT warm the inference path itself — for true inference warmup a real low-token request is needed, left as a follow-up. STT / TTS WebSocket prewarms (above) dominate the cold-start latency budget. Files: `libraries/python/getpatter/services/llm_loop.py`, `libraries/typescript/src/llm-loop.ts`.

Tests: `libraries/python/tests/unit/test_provider_warmup.py` (14 unit tests — Deepgram / Cartesia / AssemblyAI / ElevenLabs WS / Cartesia TTS / Inworld / OpenAI Realtime each verified for: warmup completes, WS opened + closed, no audio / no synthesis-commit frames sent during warmup, errors swallowed at DEBUG) and `libraries/typescript/tests/unit/provider-warmup.mocked.test.ts` (15 parity tests). Tests are tagged `@pytest.mark.mocked` (Py) / filename `*.mocked.test.ts` (TS) per `.claude/rules/authentic-tests.md` — only the network boundary is mocked; protocol negotiation, URL construction, and the warmup logic itself run real code. Defaults preserved: `agent.prewarm` is still `true` by default, warmup remains a no-op for unmodified custom providers, and existing tests pass without modification.

### Added — Pre-warm and pre-synth firstMessage (Python + TypeScript parity)

Cold-start latency on the first turn of an outbound call is dominated by DNS / TLS / HTTP-keepalive handshakes against the LLM and TTS providers (typical: 200-700 ms TTS first-byte plus 150-400 ms LLM connection setup, on top of the carrier's 3-15 s ringing window). The `Agent.prewarm` and `Agent.prewarm_first_message` flags let `Patter.call(...)` reclaim that lost latency by working in parallel with the carrier-side `initiate_call`.

- **`Agent.prewarm: bool = True`** (Python) / **`agent.prewarm?: boolean`** (TypeScript, default `true` when undefined). When `True`, `Patter.call` spawns a fire-and-forget task that invokes the optional `warmup()` method on the configured STT, TTS, and LLM providers in parallel via `asyncio.gather(..., return_exceptions=True)` (Python) / `Promise.allSettled` (TS). Built-in LLM providers ship a real warmup that issues a 5 s-bounded HTTPS `GET /models` to the upstream — OpenAI (`https://api.openai.com/v1/models`), Anthropic (`https://api.anthropic.com/v1/models`), Google (`https://generativelanguage.googleapis.com/v1beta/models`), Cerebras (`https://api.cerebras.ai/v1/models`), Groq (`https://api.groq.com/openai/v1/models`). STT and TTS providers inherit a no-op default; concrete providers can override `async warmup() -> None` (Python ABC) / `warmup?(): Promise<void>` (TS interface) to prime their own connections. Failures are logged at DEBUG and never abort the call — the feature is pure latency optimisation. Files: `libraries/python/getpatter/providers/base.py` (default `warmup` on `STTProvider`, `TTSProvider`), `libraries/python/getpatter/services/llm_loop.py` (`OpenAILLMProvider.warmup`), `libraries/python/getpatter/providers/anthropic_llm.py`, `libraries/python/getpatter/providers/google_llm.py`, `libraries/python/getpatter/client.py` (`Patter._spawn_provider_warmup`), `libraries/typescript/src/llm-loop.ts` (`LLMProvider.warmup` optional + `OpenAILLMProvider.warmup`), `libraries/typescript/src/providers/cerebras-llm.ts`, `libraries/typescript/src/providers/groq-llm.ts`, `libraries/typescript/src/providers/anthropic-llm.ts`, `libraries/typescript/src/providers/google-llm.ts`, `libraries/typescript/src/provider-factory.ts` (`STTAdapter.warmup`, `TTSAdapter.warmup` optional), `libraries/typescript/src/client.ts` (`Patter.spawnProviderWarmup`).

- **`Agent.prewarm_first_message: bool = False`** (Python) / **`agent.prewarmFirstMessage?: boolean = false`** (TypeScript). Off by default to preserve the prior cost surface. When `True`, after `Patter.call` resolves the carrier-issued `call_id` it spawns a background task that calls `agent.tts.synthesize(agent.first_message)` (Python) / `agent.tts.synthesizeStream(agent.firstMessage)` (TS), accumulates the bytes, and stores the buffer in `Patter._prewarm_audio[call_id]` / `Patter.prewarmAudio.set(callId, buffer)`. The synth is bounded by `ring_timeout` (default 25 s) so a never-answered call can't tie up the TTS connection. The per-call `StreamHandler` (`PipelineStreamHandler` Python / `StreamHandler.runPipeline` TS) now checks the cache via `pop_prewarm_audio(call_id)` / `popPrewarmAudio(callId)` at the start of the firstMessage emit; on a cache hit the buffer is sent directly through the carrier-side audio sender (which handles native-rate → carrier-rate resampling identically to the live TTS path), the `tts.synthesize` round-trip is skipped, and TTS first-byte latency drops to ~0 ms. **Cost implication**: the TTS bill for `agent.first_message` is paid as soon as the synth task completes, even when the call is never answered (no-answer / busy / AMD voicemail). When the call ends without consuming the cache, `Patter.end_call` / `Patter.endCall` log a WARN naming the wasted call_id and approximate byte count so operators see the cost surface explicitly. Files: `libraries/python/getpatter/client.py` (`Patter._prewarm_audio`, `pop_prewarm_audio`, `_record_prewarm_waste`, `_spawn_prewarm_first_message`), `libraries/python/getpatter/server.py` (`EmbeddedServer.pop_prewarm_audio` forward), `libraries/python/getpatter/telephony/twilio.py` + `telnyx.py` (bridge accepts `pop_prewarm_audio`), `libraries/python/getpatter/stream_handler.py` (`PipelineStreamHandler` consumes cache in firstMessage emit), `libraries/typescript/src/client.ts` (`Patter.prewarmAudio`, `popPrewarmAudio`, `recordPrewarmWaste`, `spawnPrewarmFirstMessage`), `libraries/typescript/src/server.ts` (`EmbeddedServer.popPrewarmAudio`), `libraries/typescript/src/stream-handler.ts` (`StreamHandlerDeps.popPrewarmAudio`, firstMessage emit consumes cache).

Tests: `libraries/python/tests/test_prewarm.py` (14 unit tests covering default flag values, no-op default `warmup`, all-three-providers warmup invocation, opt-out via `prewarm=False`, exception swallow at DEBUG, cache populate / skip / empty-message / timeout, one-shot pop semantics, waste-warn log, StreamHandler cache-hit short-circuit + cache-miss live-TTS fallback) and `libraries/typescript/tests/unit/prewarm.test.ts` (11 parity tests). Both suites use authentic real code paths — only the network boundary is exercised through stubs — per `.claude/rules/authentic-tests.md`. Defaults preserved: `agent.prewarm` is `true` and warmup is a no-op for unmodified providers, so existing tests pass without modification; `agent.prewarm_first_message` is `false`, so the new TTS-bill cost surface is strictly opt-in.

### Changed — Dashboard: STT and TTS rendered as separate cost rows

The cost breakdown panel previously combined STT and TTS spend into a single "STT / TTS" line, which hid which side of the audio pipeline dominated cost. The two providers are typically distinct (e.g. Cartesia STT + ElevenLabs TTS) and bill at very different rates per second of audio. The panel now renders them as two adjacent rows labeled with the actual provider name (e.g. "Cartesia STT" / "ElevenLabs TTS"), driven by `record.metrics.stt_provider` / `tts_provider` already exposed by the backend. The legacy `CallCostUi.sttTts` field is kept in `dashboard-app/src/lib/mappers.ts` for the few aggregate-spend callers (`callSpend`, totals bar) and is now derived as `stt + tts` after both granular fields are populated. Files: `dashboard-app/src/lib/mappers.ts`, `dashboard-app/src/components/CostPanel.tsx`.

### Changed — `stt_ms` is now finalization-only (Python + TypeScript parity)

⚠️ Semantic change to `LatencyBreakdown.stt_ms`. Previously the value measured `stt_complete - turn_start`, which conflated user speech duration with STT processing — a 5 s utterance produced `stt_ms ≈ 5000` even when Cartesia / Deepgram finalized in 200 ms after end-of-speech. The legacy interpretation was misleading: industry benchmarks (Picovoice, Deepgram, Gladia, Speechmatics, Daily.co) all report STT latency as the **finalization window** — `final_transcript - end_of_speech` — independent of how long the user spoke. `stt_ms` now matches that definition: it measures from the endpoint signal (VAD `speech_stop` or STT `speech_final`, whichever comes first) to the final transcript delivery. When the endpoint signal is unavailable (degraded provider, batch STT) the metric falls back to the legacy `turn_start` anchor so dashboards never see a spuriously zero value.

A new optional field `LatencyBreakdown.user_speech_duration_ms` (`userSpeechDurationMs` over the wire stays `user_speech_duration_ms` for SDK parity) carries the displaced "how long did the user speak" number, populated only when the endpoint signal is present. Together with the existing `agent_response_ms` (silence detection + LLM TTFT + TTS first-byte) and `total_ms` (turn_start → first agent audio byte), the breakdown now cleanly separates the four orthogonal slices a voice-AI dashboard needs: utterance length, STT finalization, LLM TTFT, TTS first-byte. Files: `libraries/python/getpatter/models.py`, `libraries/python/getpatter/services/metrics.py`, `libraries/typescript/src/metrics.ts`.

### Added — OTel `patter.*` span attributes (Python only; TS parity follow-up)

⚠️ Parity gap: this lands in the Python SDK only. TypeScript follow-up is tracked separately and will land in a subsequent release. Per `.claude/rules/sdk-parity.md` every public feature must reach both SDKs; this is a known time-boxed exception.

- **`getpatter.observability.attributes`** — three new helpers added: `record_patter_attrs(attrs)`, `patter_call_scope(call_id, side)` (context manager), and `attach_span_exporter(patter, exporter, side)`. Lazy-OTel-guarded; no-op when the `[tracing]` extra is not installed. Two ContextVars (`patter.call_id`, `patter.side`) propagate through the asyncio task tree so spans emitted by deeply nested provider code inherit the active call's identity automatically. File: `libraries/python/getpatter/observability/attributes.py`. The three symbols are re-exported from `getpatter.observability` for direct import.
- **`Patter._attach_span_exporter(exporter, *, side="uut")`** — public-but-underscore hook for tools that observe Patter from outside (e.g. an out-of-process agent runner). Default `side="uut"` preserves all existing behaviour. The leading underscore signals it is not part of the customer-facing API surface. File: `libraries/python/getpatter/client.py`.
- **Per-provider cost emission (19 surfaces)** — `patter.cost.{telephony_minutes, stt_seconds, tts_chars, llm_input_tokens, llm_output_tokens, realtime_minutes}` are now stamped on the active span across the provider lineup (Twilio + Telnyx telephony adapters; Deepgram, AssemblyAI, Whisper, OpenAI Transcribe, Soniox, Speechmatics, Cartesia STT; ElevenLabs, OpenAI, Cartesia, LMNT, Rime TTS; OpenAI/Anthropic/Google/Groq/Cerebras LLM; OpenAI Realtime + ElevenLabs ConvAI realtime). Provider tag emitted alongside as `patter.{telephony,stt,tts,llm,realtime}.provider`. All call sites are wrapped in defensive `try/except` so observability cannot kill a live call.
- **Per-turn latency** — `patter.latency.{ttfb_ms, turn_ms}` stamped from `StreamHandler._emit_turn_metrics` via a new `PipelineHookExecutor.record_turn_latency(*, ttfb_ms, turn_ms)` method. `ttfb_ms` maps to `total_ms` (turn-start → first TTS audio byte, the user-perceptible TTFB); `turn_ms` maps to `tts_total_ms` and falls back to `total_ms` when null. Files: `libraries/python/getpatter/services/pipeline_hooks.py`, `libraries/python/getpatter/stream_handler.py`.
- **`patter_call_scope` enters at the bridge level** so the entire WebSocket bridge lifetime — including hangup / cleanup — is bound to `patter.call_id` and `patter.side`. The scope is opened on the Twilio `start` / Telnyx `streaming.started` event (when the call_id is known) and closed in the `finally:` block via `contextlib.ExitStack`, so cleanup-emitted spans (handler.cleanup, telephony cost queries, on_call_end) inherit the call identity. Files: `libraries/python/getpatter/telephony/twilio.py`, `libraries/python/getpatter/telephony/telnyx.py`.
- **`TwilioAdapter.record_call_end_cost` / `TelnyxAdapter.record_call_end_cost`** — adapter-level helpers used by the bridge to emit `patter.cost.telephony_minutes` once the call's wall-clock duration is known. Files: `libraries/python/getpatter/providers/twilio_adapter.py`, `libraries/python/getpatter/providers/telnyx_adapter.py`.
- **Docs**: `docs/python-sdk/tracing.mdx` updated with a new "Cost and latency attributes (`patter.*`)" section and an "Attach a custom exporter" example showing how to wire `Patter._attach_span_exporter` to an `InMemorySpanExporter` for tests or to an `OTLPSpanExporter` in production.

### Added — Opt-in barge-in confirmation strategies (Python + TypeScript)

- **Opt-in barge-in confirmation strategies** (Python + TypeScript parity, fully backward-compatible). Cloud TTS providers take 200-700 ms to emit the first audio byte and PSTN background noise routinely fires VAD before any real interruption is happening; the legacy "any VAD speech_start during TTS cancels the agent" contract therefore produced frequent false-positive cancels — the agent was cut by cough/click/HVAC/breath and lost the conversational thread. The new ``Agent.barge_in_strategies`` (Python) / ``agent.bargeInStrategies`` (TypeScript) tuple lets callers opt into a two-stage confirmation pipeline: VAD speech_start during TTS now marks the barge-in as *pending* (TTS keeps streaming naturally, the in-flight LLM stream is preserved), every STT transcript is fed to each configured strategy, and the first strategy that returns ``True`` cancels the agent and runs the existing flush sequence; if no strategy confirms within ``barge_in_confirm_ms`` (default 1500 ms) the pending state is dropped and the agent finishes its sentence. New module ``getpatter.services.barge_in_strategies`` exposes the ``BargeInStrategy`` Protocol, the ``MinWordsStrategy`` reference implementation (filters short backchannels — "okay", "uh-huh", "yeah" — by requiring N words while the agent is speaking and letting any single word through while the agent is silent), and ``evaluate_strategies`` / ``reset_strategies`` helpers with short-circuit-OR composition and per-strategy error isolation. TS twin in ``src/services/barge-in-strategies.ts``. Wiring lives in ``stream_handler.py`` ``_handle_barge_in`` / ``stream-handler.ts`` ``handleBargeIn`` — both keep the existing canBargeIn gate and only add the confirm step when at least one strategy is configured. Defaults preserved: ``barge_in_strategies=()`` matches the prior cancel-immediately behaviour byte-for-byte, so existing users see no change unless they opt in. New regressions: 14 unit tests for ``MinWordsStrategy`` + composition (Py); 15 parity tests (TS); 10 end-to-end tests covering pending lifecycle, confirmation, timeout, idempotency, and threshold parametrization (Py); 10 TS twins. Files: ``libraries/python/getpatter/services/barge_in_strategies.py``, ``libraries/python/getpatter/models.py``, ``libraries/python/getpatter/__init__.py``, ``libraries/python/getpatter/stream_handler.py``, ``libraries/typescript/src/services/barge-in-strategies.ts``, ``libraries/typescript/src/types.ts``, ``libraries/typescript/src/index.ts``, ``libraries/typescript/src/stream-handler.ts``.

### Fixed

- **Dashboard live-transcript: live pane now accumulates user/assistant lines across every turn** (TypeScript-only, frontend + backend, dashboard BUG 1). The live-transcript fallback in `dashboard-app/src/lib/mappers.ts` derived UI rows from `record.turns[]` (the `TurnMetrics` shape), but the primary mapper path checked `record.transcript.length > 0` — which was always empty for in-flight calls because the active record only carried `turns[]`. On every `turn_complete` SSE the pane re-rendered from a single source of truth that flickered between "fallback derived from one turn" and "primary path with empty transcript", producing the symptom that the most recent user/agent pair would replace the previously-rendered turn instead of appending. Fix: `MetricsStore.recordTurn` now mirrors each completed round-trip into a flat `transcript` array on the active record (one `{role:'user', text, timestamp}` entry when `user_text` is non-empty, one `{role:'assistant', text, timestamp}` entry when `agent_text` is non-empty and not the `[interrupted]` sentinel). The mapper's primary path therefore sees an accumulating `user → assistant → user → assistant → …` history live, identical in shape to what completed calls expose. Files: `libraries/typescript/src/dashboard/store.ts`. New regressions: `libraries/typescript/tests/dashboard-store.test.ts` — `recordTurn appends both user and assistant lines to active.transcript across turns` (3-turn round-trip; asserts 5 entries in the right order) and `recordTurn skips '[interrupted]' agent_text and empty user_text from active.transcript` (filters first-message/interrupted edge cases).

- **Dashboard live-transcript: pane no longer goes blank in the carrier-statusCallback → recordCallEnd race window** (TypeScript-only, frontend + backend, dashboard BUG 2). The Twilio `statusCallback` for `CallStatus=completed` runs `MetricsStore.updateCallStatus(callId, 'completed', …)`, which moved the active record into the completed buffer WITHOUT preserving its running `turns[]` / `transcript[]`. The subsequent WS-driven `recordCallEnd` then overwrote the row in place — but in the race window between those two events the completed entry had no transcript, and any `useTranscript` fetch in that window cleared the live-pane render. Three coupled fixes: (1) `updateCallStatus` now copies `active.turns` and `active.transcript` into the new completed entry on the terminal-status branch; (2) `recordCallEnd` falls back to the running active/existing transcript when `data.transcript` is empty (e.g. `endCall` invoked without an authoritative history payload); (3) the `useTranscript` hook in `dashboard-app/src/hooks/useTranscript.ts` now subscribes to SSE `call_end` events (in addition to `turn_complete`) and refetches the call detail the moment `recordCallEnd` lands the SDK-authoritative `history.entries` transcript. Files: `libraries/typescript/src/dashboard/store.ts`, `dashboard-app/src/hooks/useTranscript.ts`. New regressions: `libraries/typescript/tests/dashboard-store.test.ts` — three new cases covering `updateCallStatus('completed')` carry-over, `recordCallEnd` running-transcript fallback when `data.transcript` is missing, and the explicit `data.transcript` taking precedence over the running fallback.

- **Dashboard sparkline tooltip: per-card metric-specific aggregate (count / avg latency / total cost)** (TypeScript-only, frontend, dashboard BUG 4). Every metric card's hover tooltip showed the same generic "N call(s)" headline and a per-call sample list — so the spend card and the latency card were indistinguishable from the calls card. The tooltip now reports a metric-specific aggregate above the per-call sample list: `TOTAL COST $X.XXX` (sum of per-call cost in the bucket) for the spend card, `AVG LATENCY <p95-mean> ms` (mean of per-call P95 in the bucket) for the latency card, and `N CALLS` for the count cards (existing behaviour, made explicit). Headline label uppercased, monospace, and styled to match the existing time-range header so the tooltip reads consistently with the rest of the site. New `MetricKind` type (`'count' | 'latency' | 'spend'`) drives the headline calculation in pure form via the new `bucketHeadline` export, callable from tests. Files: `dashboard-app/src/components/Metric.tsx`, `dashboard-app/src/App.tsx`, `dashboard-app/src/styles/dashboard.css`.

- **Dashboard: outbound call disappeared from the recent-calls table after end** (Python + TypeScript parity, BUG C, behavioural fix). The Twilio `statusCallback` for `CallStatus=completed` arrives a moment before the WS `stop` frame and runs `update_call_status` / `updateCallStatus`, which already moves the row from `_active_calls` / `activeCalls` into the completed list. Shortly after, the WS-stop path runs `record_call_end` / `recordCallEnd` for the same call_id — but the active record is already gone, so the prior implementations appended a SECOND row with `started_at=0`, empty caller/callee, and freshly captured metrics. `MetricsStore.get_calls` returns newest-first and the dashboard SPA's `mergeCalls` keeps only the first match by call_id, so the older well-formed row was masked by the malformed duplicate; the duplicate's `startedAtMs=0` then dropped it out of the 24h time-range filter and the call vanished from the UI altogether. `record_call_end` / `recordCallEnd` now look up the existing entry in `_calls` / `calls[]` and update it in place (preserving caller/callee/started_at, merging in the just-collected metrics) instead of appending a duplicate. Files: `libraries/python/getpatter/dashboard/store.py`, `libraries/typescript/src/dashboard/store.ts`. New regressions: `libraries/python/tests/unit/test_dashboard_store_unit.py::TestRecordCallEndDeduplication` (2 tests — exercises the full `record_call_initiated → record_call_start → update_call_status → record_call_end` sequence and asserts (a) `call_count == 1` (no duplicate), (b) caller/callee/started_at preserved, (c) the call survives the 24h time-range filter); equivalent 2-test describe in `libraries/typescript/tests/dashboard-store.test.ts`.

- **Dashboard: live transcript pane stayed empty during in-flight calls** (Python + TypeScript parity, BUG A, frontend + backend). Two coupled bugs hid streaming transcripts from the dashboard SPA while a call was in progress: (1) `GET /api/dashboard/calls/:callId` only consulted the completed-call buffer (`store.get_call` / `store.getCall`), returning 404 for any active call; the SPA's `useTranscript` hook polled this route every 2 s and rendered `[]` for the entire call lifetime. (2) The completed-call shape exposes `transcript: TranscriptEntry[]` while active records expose `turns: TurnMetrics[]` (the per-round-trip metrics shape), and `toUiTranscript` in `dashboard-app/src/lib/mappers.ts` only knew how to read `transcript`. Both routes (`/api/dashboard/calls/:callId` and the v1 `/api/v1/calls/:callId`) now fall through to `get_active` / `getActive` when the completed lookup misses, so the live record is reachable. `toUiTranscript` now falls back to `record.turns` when `record.transcript` is empty, deriving user/agent message rows from `user_text` / `agent_text` (skipping the sentinel `[interrupted]` turns). `useTranscript` additionally subscribes to `/api/dashboard/events` for `turn_complete` events filtered by `call_id` so new turns appear within ~50 ms of the round-trip ending — the existing 2 s polling stays in place as a backstop for SSE drops. Files: `libraries/python/getpatter/dashboard/routes.py`, `libraries/typescript/src/dashboard/routes.ts`, `dashboard-app/src/lib/mappers.ts`, `dashboard-app/src/hooks/useTranscript.ts`. New regression: `libraries/python/tests/unit/test_dashboard_store_unit.py::TestActiveCallDetail::test_get_active_returns_record_with_turns` (verifies the accessor the route now falls back to exposes the live turns).

- **Dashboard logs: outbound calls persisted with empty `caller` / `callee` in `metadata.json`** (Python + TypeScript parity, BUG B). Inline TwiML for outbound calls (`<Connect><Stream url="…/ws/stream/outbound"/></Connect>`) carries no `<Parameter>` tags and no query-string metadata, so the WS bridge's `caller` / `callee` are empty strings on outbound. The `_on_call_start` (Py) / `wrapLoggingCallbacks` (TS) wrappers passed those empty strings straight to `CallLogger.log_call_start` / `logCallStart`, and every outbound call's persisted `metadata.json` ended up with `caller=""` / `callee=""` even though the in-memory store had the correct numbers (populated at dial time by `record_call_initiated`). Wrappers now resolve caller/callee from the active store record when the bridge data is empty, so `metadata.json` is faithful to the dial. As a related parity fix: `record_call_start` (Py) was also clobbering existing caller/callee with the empty strings on the upgrade-from-initiated path; it now mirrors the existing TS behaviour and only overwrites when the new value is non-empty. Files: `libraries/python/getpatter/server.py`, `libraries/python/getpatter/dashboard/store.py`, `libraries/typescript/src/server.ts`. New regressions: `libraries/python/tests/unit/test_server_unit.py::TestWrapCallbacks::test_call_log_start_pulls_caller_from_active_record` (real `CallLogger` writes a real `metadata.json`; asserts the masked phone numbers end with the original last-4) and `libraries/typescript/tests/server.test.ts` (parity test in `EmbeddedServer wraps logging callbacks with active-record fallback`).

- **Barge-in: `InterruptionMetrics.detection_delay_ms` corrupted to ~0 on strategy-confirmed cancel** (Python + TypeScript parity, FIX #88, fully backward-compatible). When `agent.barge_in_strategies` was non-empty, the two-stage barge-in flow stamped T1 via `record_overlap_start()` from `_start_pending_barge_in` (VAD speech_start) and then stamped T2 via a SECOND `record_overlap_start()` from `_do_cancel_for_barge_in` after the strategy confirmed — overwriting T1. The downstream `record_overlap_end()` therefore computed `T2 → now ≈ 0`, hiding the real ~150-500 ms VAD-to-confirm latency on every confirmed barge-in. The cancel path now captures the pending state BEFORE clearing it and skips the redundant `record_overlap_start()` when VAD already started the overlap window. Legacy path (`barge_in_strategies=()`, no VAD pending phase) is unchanged — `_do_cancel_for_barge_in` is still the sole caller of `record_overlap_start` there. New regressions in `libraries/python/tests/unit/test_barge_in_two_stage.py::TestBargeInOverlapStartPreserved` (3 tests including end-to-end via real `CallMetricsAccumulator` asserting detection_delay reflects the ~200 ms VAD→confirm window, not ~0) and `libraries/typescript/tests/unit/barge-in-two-stage.test.ts` (`StreamHandler — overlap window preserved across VAD → strategy confirm`, 2 tests). Files: `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/stream-handler.ts`.

- **Barge-in: leaked pending-confirmation task on call end** (Python + TypeScript parity, FIX #89, fully backward-compatible). `PipelineStreamHandler.cleanup` (Py) and `StreamHandler.handleStop` / `handleWsClose` (TS) tore down STT / TTS / remote adapters but never dropped the pending barge-in timeout. If a call ended while a barge-in was in pending-timeout state (waiting for strategy confirmation), the asyncio.Task / `setTimeout` remained scheduled and fired `record_overlap_end` / `recordOverlapEnd` on a finalised metrics object `barge_in_confirm_ms` later (default 1500 ms) — a slow leak in long-running servers and a race producing spurious overlap_end events on unrelated subsequent calls if the metrics object got GC'd and reused. Both SDKs now call `_clear_pending_barge_in` / `clearPendingBargeIn` at the top of cleanup, before any other tear-down. Idempotent: safe to call when no pending state exists, so the legacy non-strategy flow is unchanged byte-for-byte. New regressions in `libraries/python/tests/unit/test_barge_in_two_stage.py::TestCleanupClearsPendingBargeIn` (2 tests) and `libraries/typescript/tests/unit/barge-in-two-stage.test.ts` (`StreamHandler — handleStop / handleWsClose drops pending barge-in timer`, 2 tests). Files: `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/stream-handler.ts`.

- **Docs drift: `Agent.barge_in_strategies` docstring claimed "TTS is paused"** (Python + TypeScript parity, FIX #90). The docstring on `Agent.barge_in_strategies` (Python) / `agent.bargeInStrategies` (TypeScript) said "the agent's TTS is paused" while a barge-in was pending strategy confirmation, but the implementation does the opposite: TTS continues streaming naturally and only the strategy-confirmed cancel path stops TTS. Replaced with "the agent's TTS continues streaming naturally" so users opting into the confirm pipeline aren't surprised by uninterrupted audio during the pending window. Surgical text-only fix — no behaviour change. Files: `libraries/python/getpatter/models.py`, `libraries/typescript/src/types.ts`.

- **Pipeline first-message prewarm: cached audio sent as a single multi-second buffer (cancel granularity lost)** (Python + TypeScript parity, FIX #97). On a prewarm cache hit, `PipelineStreamHandler.start` (Py) and `StreamHandler.initPipeline` (TS) called `audio_sender.send_audio(prewarm_bytes)` / `bridge.sendAudio(...)` with the full multi-second buffer in one shot, while the live TTS path streams 20-128 ms chunks paced by the upstream provider. A `send_clear` issued mid-buffer therefore had nothing to clear from Twilio's mark/clear bookkeeping, manifesting as "the agent keeps talking after barge-in" on the very first turn only. New private helper `_stream_prewarm_bytes` (Py) / `streamPrewarmBytes` (TS) splits the prewarm buffer into 1280-byte chunks (40 ms PCM16 @ 16 kHz mono — sized to mirror the smallest live-TTS boundary) and forwards each through the existing `audio_sender` / `bridge.sendAudio` so cancel granularity is identical regardless of cache hit vs miss. Same `_is_speaking` guard at every iteration so a barge-in mid-prewarm stops chunking immediately. New regressions: `libraries/python/tests/test_prewarm.py::test_stream_prewarm_bytes_chunks_buffer` + `test_stream_prewarm_bytes_stops_on_barge_in_mid_buffer` (2 tests) and `libraries/typescript/tests/unit/prewarm.test.ts` (`streamPrewarmBytes — chunked send for cancel granularity`, 2 tests). Both assert ≥100 `send_audio`/`bridge.sendAudio` calls for a 5-second buffer (vs 1 in the regression) and that the loop honours mid-prewarm `_is_speaking=False`. Files: `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/stream-handler.ts`.

- **OpenAI Realtime warmup: replaced billing-unsafe `response.create` with `session.update`** (Python + TypeScript parity). The earlier prewarm pass sent `{"type": "response.create", "response": {"generate": false}}` to "prime model state", but the `generate` field is NOT in the OpenAI Realtime API schema. Two failure modes were possible: (a) the server silently ignores the unknown field and invokes a real model response, billing tokens on every prewarm, or (b) the request is rejected with `invalid_request_error`, which makes the prewarm a no-op beyond TLS warm. Either way the prior implementation was wrong. The new flow opens the WS, waits for `session.created`, sends a single `session.update` whose body matches what `connect()` sends at production call-pickup (`input_audio_format`, `output_audio_format`, `voice`, `instructions`, `turn_detection`, `input_audio_transcription`, plus any opt-in fields populated on the adapter), waits for the matching `session.updated` ack, then closes cleanly. `session.update` only mutates session configuration — it does not invoke the model, does not consume any audio buffer, and does not trigger token generation, so the warmup is byte-for-byte billing-safe. The `_prewarm_response_id` (Py) / `prewarmResponseId` (TS) field is removed since it was only ever populated by the (broken) `response.create` path. New regression: `tests/unit/test_provider_warmup.py::test_openai_realtime_warmup_does_not_send_response_create` (Py) and the corresponding `does not send response.create on the wire` case in `tests/unit/provider-warmup.mocked.test.ts` (TS) — both fail loudly if a future change reintroduces `response.create` in the warmup path. Files: `libraries/python/getpatter/providers/openai_realtime.py`, `libraries/typescript/src/providers/openai-realtime.ts`.

- **ElevenLabs WS warmup: BOS frame now byte-identical to live `synthesize` BOS** (Python + TypeScript parity). The live `synthesize()` / `synthesizeStream()` path attaches `voice_settings` and (when `auto_mode=False`) `generation_config` to the protocol-required BOS frame, but the warmup variant only sent `{"text": " "}`. Because ElevenLabs may instantiate a different per-session worker depending on the BOS configuration, the warmed worker could end up unrelated to the worker that handles the live request — defeating the edge-warm goal entirely. Both paths now share a single `_build_bos_frame` (Py) / `buildBosFrame()` (TS) helper, so the warmup BOS is byte-identical to the production BOS for any given adapter configuration. New regression: `tests/unit/test_provider_warmup.py::test_elevenlabs_ws_warmup_bos_frame_matches_live_synthesize` (Py) and `warmup BOS bytes are byte-identical to synthesizeStream BOS bytes (regression)` in `tests/unit/provider-warmup.mocked.test.ts` (TS) — both capture the BOS bytes in each path and assert byte-equality. Files: `libraries/python/getpatter/providers/elevenlabs_ws_tts.py`, `libraries/typescript/src/providers/elevenlabs-ws-tts.ts`.

- **Inworld TTS warmup: replaced HEAD against POST-only endpoint with `GET /tts/v1/voices`** (Python + TypeScript parity). The earlier warmup issued `HEAD https://api.inworld.ai/tts/v1/voice:stream`, but that endpoint is POST-only — Inworld returned `405 Method Not Allowed` on every call, completing the TLS handshake but spamming 405s into Inworld's audit logs and into our own logs. The new path issues `GET /tts/v1/voices` (a documented free metadata read that returns the configured voice catalogue) so the response is 2xx-clean. Billing surface is unchanged — the synthesis pipeline is invoked only by `POST /tts/v1/voice:stream` with non-empty `text`. Tests assert the URL targets `/tts/v1/voices` and the response status is 2xx, with explicit asserts that the warmup does NOT target `voice:stream` and does NOT use HEAD. Files: `libraries/python/getpatter/providers/inworld_tts.py`, `libraries/typescript/src/providers/inworld-tts.ts`.

- **Cartesia STT + AssemblyAI STT warmup: API key no longer leaks into logs on handshake failure** (Python + TypeScript parity, security). Both providers authenticate via a query-string parameter on the WS upgrade URL (Cartesia: `?api_key=...`, AssemblyAI optional: `?token=...`). When the WS handshake failed (e.g. 401 from a rotated key), `aiohttp.WSServerHandshakeError.__str__` (Py) and the `ws` library `Error.message` (TS) typically include the full request URL — and `logger.debug("warmup failed: %s", exc)` therefore wrote the API key straight into application logs. The fix catches the handshake-error class specifically before the generic `Exception` handler and logs only the HTTP status code (or, for non-handshake errors, just the exception class name) — never the full message or URL. New regression tests in both SDKs install a custom logger / `caplog` capture, force a 401 handshake error during warmup, and assert (a) the API key never appears in any captured log message, (b) the URL with `?api_key=` / `?token=` never appears either, and (c) the status code is still surfaced so operators see why the warmup failed. Files: `libraries/python/getpatter/providers/{cartesia_stt,assemblyai_stt}.py`, `libraries/typescript/src/providers/{cartesia-stt,assemblyai-stt}.ts`.

- **Dashboard hydrate: hydrated calls no longer lose `cost` and `latency`** (Python + TypeScript parity, fully backward-compatible). `CallLogger.log_call_end` writes `cost`, `latency`, `duration_ms`, and `telephony_provider` as **top-level keys** of `metadata.json`, but `MetricsStore.hydrate` (`libraries/python/getpatter/dashboard/store.py:535`, `libraries/typescript/src/dashboard/store.ts:421-424`) read them only from `meta.metrics.cost` / `meta.metrics.latency`. Result: every call rebuilt from disk landed in the store with `metrics=null`, so the local dashboard rendered `$0.00` and `—` for cost/latency on all hydrated rows; only the in-flight call (which never goes through hydrate) showed real numbers. Caught during 0.6.0 acceptance testing — 48 of 49 calls in the dashboard had blank P95/cost columns. Fix promotes the top-level fields into a synthesized `metrics` dict (`metrics_from_top_level` / `metricsFromTopLevel`) when `meta.metrics` is missing, mapping `latency.p95_ms` → `metrics.latency_avg.total_ms` so the existing UI fields populate. Explicit `meta.metrics` (legacy/future shape) is preserved untouched. New regressions: `tests/unit/test_metrics_store_hydrate.py::test_hydrate_lifts_top_level_cost_and_latency_into_metrics` + `test_hydrate_preserves_explicit_metrics_when_present` (Py); two `MetricsStore.hydrate` cases in `tests/dashboard-store.test.ts` (TS). Files: `libraries/python/getpatter/dashboard/store.py`, `libraries/typescript/src/dashboard/store.ts`.

- **Pipeline early barge-in: VAD self-cancellation before TTS first byte arrived** (Python + TypeScript parity, behavioural change for pipeline mode). Cloud TTS providers (ElevenLabs, Cartesia, …) take 200–700 ms to emit the first audio byte. The barge-in anti-flicker gate was anchored on `_speaking_started_at` / `speakingStartedAt` (set inside `_begin_speaking` / `beginSpeaking`), so a 250 ms gate without AEC expired BEFORE TTS produced any audio. VAD then picked up background noise, room ambience, or a "hello?" from the operator and triggered `[VAD] speech_start during TTS → BARGE-IN` → `cancelSpeaking` → `isSpeaking=false` → the `for await (chunk of tts.synthesizeStream(...))` loop exited at `if (!this.isSpeaking) break`, emitting **zero bytes**. From the SDK's perspective the agent "spoke" the first message; from the caller's perspective the line went silent until the next turn. Reproduced on a pipeline outbound call (~47 s — first message never reached the wire). Fix introduces `_first_audio_sent_at` / `firstAudioSentAt`, set in a new `_mark_first_audio_sent` / `markFirstAudioSent` helper invoked AFTER `audio_sender.send_audio` / `bridge.sendAudio` succeeds at all four pipeline emit sites (firstMessage, streaming response, regular response, WebSocket remote). `_can_barge_in` / `canBargeIn` now refuses to open the gate while `_first_audio_sent_at` is null — VAD speech_start before the first wire-time byte is suppressed regardless of how much wall-clock has elapsed since `_begin_speaking`. The 250 ms / 1000 ms gate values are unchanged — only the anchor moves. New regressions: `tests/unit/test_stream_handler_unit.py::test_barge_in_suppressed_before_first_audio_emitted` (Py); `canBargeIn() false before the first TTS chunk has hit the wire` in `tests/unit/stream-handler.test.ts` (TS). Existing `_handle_barge_in` / `handleBargeIn` tests updated to set both timestamps to reflect the new contract. Files: `libraries/python/getpatter/stream_handler.py`, `libraries/typescript/src/stream-handler.ts`.

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

- **Speech-edge events for turn-taking instrumentation** (Python + TypeScript parity, additive — no breaking changes). Patter now exposes seven optional async callbacks on every `Patter` instance plus a read-only `conversation_state` (Py) / `conversationState` (TS) snapshot, covering the standard voice-agent metric set (user/agent state transitions, turn boundaries, TTFT, audio first-byte) and aligning with OpenAI Realtime (`input_audio_buffer.speech_started/_stopped/_committed`) where applicable. The seven events: `on_user_speech_started` (raw VAD positive edge), `on_user_speech_ended` (raw VAD trailing edge — *not* end-of-utterance), `on_user_speech_eos` (committed EOU — VAD edge + trailing silence + optional semantic turn-detector agreement; the canonical "user finished" signal that anchors `eos_to_first_token_ms`), `on_agent_speech_started` (first wire-time chunk of the agent turn — what the user actually hears, distinct from TTS warmup), `on_agent_speech_ended` (last wire chunk; payload includes `interrupted: bool` for barge-in), `on_llm_token` (TTFT marker, fires once per turn on the first LLM token), `on_audio_out` (first TTS audio chunk per turn — TTS warmup, distinct from wire-time). Each event also records an OpenTelemetry span event on the current call span (`patter.event.user_speech_started`, …, `patter.event.llm_first_token` carrying `gen_ai.request.model` + `gen_ai.provider.name` per the OTel GenAI semconv) when `PATTER_OTEL_ENABLED=1` and the `opentelemetry` peer dep is installed; otherwise the OTel branch is a zero-cost no-op. The dispatcher is callback-safe — observer exceptions are caught and logged, never propagated to the live call. State machine tracks per-side `conversation_state` (`user`: `listening`/`speaking`/`thinking`/`away`, `agent`: `initializing`/`idle`/`listening`/`thinking`/`speaking`) and a monotonically-increasing `turn_idx` that increments on every committed EOU. Wired into the realtime stream handler so `user_speech_started/_ended/_eos` and `agent_speech_started/_ended` fire automatically on the OpenAI Realtime + Twilio/Telnyx path; `on_llm_token` and `on_audio_out` are exposed on the dispatcher for adapter / pipeline-mode integrations to call. New files: `libraries/python/getpatter/_speech_events.py`, `libraries/typescript/src/_speech-events.ts`. Public exports: `SpeechEvents`, `SpeechEventCallback`, `ConversationStateSnapshot`, `UserState`, `AgentState`, `EouTrigger`. 16 unit tests Py + 15 unit tests TS covering every event payload, idempotency (LLM/audio fire-once-per-turn), state transitions, OTel attach contract, callback-exception isolation, chained-callback wrapping, and Patter-level proxy mirroring. Motivated by the `patter-agent-runner` acceptance suite which ships 15 turn-taking assertion verbs (barge-in latency, silence-gap, cross-talk, eos-to-first-token, MOS, WER) that previously auto-skipped because the SDK did not surface per-side speech edges.

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

- All external license headers removed from source files. New rule `.claude/rules/no-competitor-references.md` codifies the policy.
- Root `LICENSE` updated to `Copyright (c) 2026 Patter Contributors`.
- `Dockerfile` + `docker-compose.yml` simplified; non-public-repo scripts removed.
- `playwright.config.ts` + `@playwright/test` devDep dropped (E2E lives in downstream test repo).

## 0.5.5 (2026-04-28)

Latency-pass 1: TTFA optimisations grounded in the ElevenLabs latency posts and a head-to-head review of competing production voice-AI stacks. All changes are additive or opt-in — existing call sites keep their current behaviour unchanged.

### Improved — sentence chunker

- **Italian abbreviations** added to the prefix list (Sig, Sgr, Dott, Prof, Avv, Ing, Geom, Rag, Arch, On, Egr, Spett, Gent, Ill) and the suffix list (ecc, cit, cap, sez, art, pag, fig, tab, cfr, vol, ed). Sentences like _"Ho incontrato il Sig. Rossi alla riunione di stamattina."_ are no longer split on the abbreviation period.
- **English abbreviations** expanded with the standard NLTK Punkt abbreviation set: `Gen.`, `Sen.`, `Rep.`, `Lt.`, `Cpt.`, `Capt.`, `Col.`, `Cmdr.`, `Adm.`, `vs.`, `etc.`, `No.`, `Vol.`, `pp.`, `cf.`, `ca.`, `op.`, plus address forms `Mt.`, `Hwy.`, `Rt.`, `Pl.`, `Ave.`, `Blvd.`, `Sq.`. Phrases like _"Compare A vs. B"_ and _"Met Gen. Smith and Sen. Davis"_ no longer split mid-abbreviation.
- **Suffix + starter pattern preserves the period** (e.g. _"Patter Inc. He left."_ now keeps `Inc.` in the emitted sentence — previously dropped to `Inc`).
- **All-caps name flush fixed**. Previously the gate-5 acronym guard blocked *any* uppercase-preceded period, so _"I was speaking with RAMESH."_ would sit in the buffer forever. Now only purely-uppercase ASCII words ≤3 chars (U, US, USA, NATO patterns) are treated as acronyms.
- **Multilingual terminator support**. The terminator set now includes ASCII semicolon `;`, Unicode ellipsis `…`, full-width semicolon `；`, full-width period `．`, half-width Japanese period `｡`, plus the non-Latin terminator set: Hindi/Devanagari `। ॥`, Arabic `؟ ؛ ۔ ؏`, Armenian `։`, Ethiopic `። ፧`, Khmer `។ ៕`, Burmese `။`, Tibetan `༎ ༏`. Hindi text like _"यह हिन्दी का एक वाक्य है।"_ now flushes correctly.
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
- **Replace 5-tap binomial FIR with Kaiser-windowed half-band (31-tap)** — industry stopband is 60-80 dB; our binomial is ~20 dB. `soxr` or `scipy.signal.resample_poly` if available.
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

### Added — observability
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

### Added — observability
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

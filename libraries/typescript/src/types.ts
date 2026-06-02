/**
 * Public type definitions for the Patter SDK — agent options, pipeline hooks,
 * provider config envelopes, and serve/call request/response shapes.
 */

import type { Carrier as TwilioCarrier } from "./telephony/twilio";
import type { Carrier as TelnyxCarrier } from "./telephony/telnyx";
import type { Carrier as PlivoCarrier } from "./telephony/plivo";

/** Discriminator string carried on every {@link Carrier}.kind and threaded
 *  through every per-carrier dispatch in the SDK. The single source of truth
 *  for "which carriers exist" — extending the SDK to a new carrier should
 *  only require adding a literal here and to ``Carrier`` union sites. */
export type CarrierKind = "twilio" | "telnyx" | "plivo";
import type { Realtime } from "./engines/openai";
import type { Realtime2 } from "./engines/openai-2";
import type { ConvAI } from "./engines/elevenlabs";
import type { CloudflareTunnel, Static as StaticTunnel } from "./tunnels";
import type { Tool as ToolInstance } from "./public-api";
import type { STTAdapter, TTSAdapter } from "./provider-factory";
import type { LLMProvider } from "./llm-loop";
import type { BargeInStrategy } from "./services/barge-in-strategies";
import type { CallMetrics, CostBreakdown } from "./metrics";

/** Inbound message handed to a `MessageHandler` per turn (legacy single-turn API). */
export interface IncomingMessage {
  readonly text: string;
  readonly callId: string;
  readonly caller: string;
}

/** STT provider configuration envelope (provider name + key + language + provider-specific options). */
export interface STTConfig {
  readonly provider: string;
  readonly apiKey: string;
  readonly language: string;
  /**
   * Serialise the config into a JSON-compatible dict for the wire protocol.
   * Mandatory — matches Python's ``STTConfig.to_dict()``. Concrete classes
   * returned by ``stt(...)``/``deepgram(...)`` etc. all implement it.
   */
  toDict(): Record<string, string | Record<string, unknown>>;
  /** Provider-specific knobs (e.g. Deepgram endpointing). */
  options?: Record<string, unknown>;
}

/** TTS provider configuration envelope (provider name + key + voice + provider-specific options). */
export interface TTSConfig {
  readonly provider: string;
  readonly apiKey: string;
  readonly voice: string;
  /**
   * Serialise the config into a JSON-compatible dict for the wire protocol.
   * Mandatory — matches Python's ``TTSConfig.to_dict()``.
   */
  toDict(): Record<string, string | Record<string, unknown>>;
  options?: Record<string, unknown>;
}

/** Single-turn message handler — receives the user's transcript, returns the agent's reply. */
export type MessageHandler = (msg: IncomingMessage) => Promise<string>;
/** Generic call-lifecycle callback (start/end/transcript/metrics). */
export type CallEventHandler = (data: Record<string, unknown>) => Promise<void>;

/**
 * Public MCP server configuration. ``string`` is shorthand for
 * ``{ url: <string>, transport: 'streamable-http' }``. Re-exported from
 * ``tools/mcp-client`` to keep a single source of truth.
 */
export type MCPServerConfig =
  | string
  | {
      readonly url: string;
      readonly transport?: 'streamable-http';
      /** Headers attached to every transport request — typically auth. */
      readonly headers?: Record<string, string>;
      /** Optional logical name for telemetry / log lines. */
      readonly name?: string;
    };

/** Internal shape of a tool definition (matches `Tool` from `public-api.ts`). */
export interface ToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly parameters: Readonly<Record<string, unknown>>;
  /** Webhook URL — called when the LLM invokes this tool. Mutually exclusive with handler. */
  readonly webhookUrl?: string;
  /**
   * Local handler — called instead of ``webhookUrl`` when present.
   *
   * Two forms:
   *
   *  - **Async function**: returns the final result as a JSON string.
   *    The model receives only the final return value.
   *
   *  - **Async generator**: yields zero or more progress updates before
   *    returning. Each ``yield`` of ``{ progress: string }`` is spoken
   *    inline by the agent (Realtime: via ``adapter.sendText``) so the
   *    caller hears live status during long-running tools. The final
   *    ``return`` value (or last ``yield`` if no return) is the
   *    function-call result sent to the model. Pipeline mode currently
   *    ignores the progress yields — the final value is still used as
   *    the tool result.
   */
  readonly handler?:
    | ((args: Record<string, unknown>, context: Record<string, unknown>) => Promise<string>)
    | ((
        args: Record<string, unknown>,
        context: Record<string, unknown>,
      ) => AsyncGenerator<{ progress?: string; result?: string }, string | void, unknown>);
  /**
   * "Reassurance" filler the agent speaks while a slow tool call runs.
   * Bridges the silence when a handler or webhook takes longer than
   * humans naturally tolerate (~1.5 s) without sounding dead.
   *
   * Two forms:
   *  - string: shorthand for ``{ message: <string>, afterMs: 1500 }``.
   *  - object: explicit ``{ message, afterMs? }``. ``afterMs`` is the
   *    grace window before the reassurance fires; if the tool returns
   *    earlier, no message is spoken.
   *
   * Currently honoured only in **Realtime mode** — the SDK enqueues the
   * message via ``OpenAIRealtimeAdapter.sendText`` so the model
   * synthesises it inline. Pipeline mode has no clean injection point
   * mid-turn yet; the option is silently ignored there. Off by default.
   */
  readonly reassurance?: string | Readonly<{ message: string; afterMs?: number }>;
  /**
   * Enable OpenAI strict mode for this tool's function schema. When ``true``
   * the model is constrained to emit arguments that exactly match the
   * declared schema — no missing required fields, no extra properties, no
   * type coercion. Defaults to ``false`` for backward compatibility.
   *
   * Strict mode requires the schema to satisfy OpenAI's structural rules:
   * - root must be ``type: "object"``
   * - every nested object must have ``additionalProperties: false``
   * - every property listed in ``properties`` must also be in ``required``
   *
   * Patter validates these requirements at ``agent()`` build time when
   * ``strict: true`` is set; an invalid schema raises immediately rather
   * than failing silently mid-call. Use ``null`` in a union (``["string",
   * "null"]``) to express "optional" — strict mode does not allow truly
   * optional fields.
   *
   * Recommended for any tool whose handler/webhook can't safely tolerate
   * malformed arguments (DB writes, payment, transfers).
   */
  readonly strict?: boolean;
}

/**
 * Configuration for the built-in ``consult`` escalation tool.
 *
 * When set on an agent, Patter auto-injects a tool (default name
 * ``consult_agent``) that the in-call agent can invoke mid-call to reach the
 * caller's own back-office agent over HTTP for deeper reasoning, fresh
 * information, or an action beyond the call. Patter keeps STT + LLM/voice +
 * TTS + carrier; the back-office agent is consulted only on demand (never on
 * the per-turn path). The tool POSTs ``{ request, call_id, caller, callee }``
 * to {@link url}; the endpoint returns JSON with a ``reply`` / ``response`` /
 * ``text`` string (or any JSON / plain text) and the agent speaks it.
 *
 * Injected in **Realtime** and **Pipeline** modes only — ElevenLabs ConvAI
 * tools live on the ElevenLabs-hosted agent, so ``consult`` does not apply
 * there (a warning is emitted if set with that provider).
 */
export interface ConsultConfig {
  /** HTTP(S) endpoint Patter POSTs to. SSRF-validated at call start. */
  readonly url: string;
  /** Optional headers (e.g. an ``Authorization`` bearer). Never logged. */
  readonly headers?: Readonly<Record<string, string>>;
  /**
   * Per-consult HTTP timeout in milliseconds. Higher than the generic
   * webhook-tool default (10 000 ms) because a consult may run deeper
   * reasoning. Default ``30000``.
   */
  readonly timeoutMs?: number;
  /** Name the LLM sees for the tool. Default ``"consult_agent"``. */
  readonly toolName?: string;
  /** Description the LLM sees — tune to steer when the agent escalates. */
  readonly description?: string;
  /**
   * Opt-in: allow {@link url} to point at a loopback / private / link-local
   * host (e.g. a back-office agent on ``127.0.0.1`` or an RFC1918 LAN host).
   *
   * Default ``false`` (or ``undefined``) — the URL is SSRF-validated and
   * loopback/private/link-local targets are rejected, preserving the strict
   * default behaviour. Set ``true`` ONLY for a trusted, developer-configured
   * local agent: the URL is your own config, not caller-derived input.
   *
   * Even when ``true``, non-HTTP(S) schemes (``file:``, ``javascript:`` …)
   * are still rejected. Note: opting in also makes cloud-metadata hostnames
   * (``metadata``, ``metadata.google.internal``, ``metadata.azure.com``) and
   * the IMDS IP ``169.254.169.254`` reachable — an accepted tradeoff for a URL
   * you control. Scopes ONLY to
   * the consult tool; the generic webhook-tool path stays strict.
   */
  readonly allowLoopback?: boolean;
}

// === Local mode ===

/** Constructor options for `new Patter({...})` in local-server mode. */
export interface LocalOptions {
  /**
   * Telephony carrier instance. Required.
   *
   * @example
   * ```ts
   * import { Patter, Twilio } from "getpatter";
   * const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+1..." });
   * ```
   */
  readonly carrier: TwilioCarrier | TelnyxCarrier | PlivoCarrier;
  /**
   * Tunnel configuration. Accepts a tunnel instance, ``true`` (alias for
   * ``new CloudflareTunnel()``), or ``false`` / omitted (no tunnel).
   */
  readonly tunnel?: CloudflareTunnel | StaticTunnel | boolean;
  readonly phoneNumber: string;
  readonly webhookUrl?: string;
  /**
   * On-disk persistence for the dashboard's call history. The dashboard
   * itself is in-memory, but enabling ``persist`` writes per-call records
   * (metadata.json, transcript.jsonl, events.jsonl) to disk and rebuilds
   * the in-memory cache on startup so the dashboard survives process
   * restarts without an external database.
   *
   * Accepted values:
   * - omitted / ``false`` (default): no disk writes; the dashboard resets
   *   on every restart. Backward-compatible with prior behaviour.
   * - ``true``: write under the platform default location
   *   (``~/Library/Application Support/patter`` on macOS,
   *   ``%LOCALAPPDATA%\\patter`` on Windows,
   *   ``$XDG_DATA_HOME/patter`` on Linux). Equivalent to setting
   *   ``PATTER_LOG_DIR=auto``.
   * - string: write under the supplied absolute path. Equivalent to
   *   setting ``PATTER_LOG_DIR=<path>``.
   *
   * The ``PATTER_LOG_DIR`` env var still works as a deployment-time
   * override and takes precedence over an unset ``persist``. When
   * ``persist`` is set explicitly the env var is ignored.
   *
   * Retention: defaults to 30 days, controlled by
   * ``PATTER_LOG_RETENTION_DAYS`` (set to ``0`` to keep forever).
   * Phone numbers are masked by default; control via
   * ``PATTER_LOG_REDACT_PHONE``.
   */
  readonly persist?: boolean | string;
  /**
   * @internal — allows ``StreamHandler`` to build the default OpenAI
   * ``LLMLoop`` when no ``onMessage`` handler is supplied. The
   * ``OpenAIRealtime`` engine instance carries its own key when one is
   * used via ``phone.agent({ engine: new OpenAIRealtime({ apiKey }) })``.
   */
  readonly openaiKey?: string;
}

/** Internal shape of a guardrail (matches `Guardrail` class from `public-api.ts`). */
export interface Guardrail {
  /** Name for logging when triggered */
  readonly name: string;
  /** List of terms that trigger the guardrail (case-insensitive) */
  readonly blockedTerms?: ReadonlyArray<string>;
  /** Custom check function — return true to block the response */
  readonly check?: (text: string) => boolean;
  /** Replacement text spoken when guardrail triggers */
  readonly replacement?: string;
}

/** Per-call context passed to every pipeline hook. */
export interface HookContext {
  readonly callId: string;
  readonly caller: string;
  readonly callee: string;
  readonly history: ReadonlyArray<{ role: string; text: string }>;
}

/**
 * Streaming-friendly post-LLM transform hook. Three tiers, all optional:
 *
 * - **`onChunk`** — per-token pure transform. Sync, must be fast (~0 ms
 *   budget). Use for: regex replace, markdown strip, profanity char-swap.
 * - **`onSentence`** — per-sentence rewrite. Runs between the sentence
 *   chunker and TTS. Returns rewritten text or `null` to keep original;
 *   ``""`` (empty string) drops the sentence silently. Latency budget
 *   ~50–300 ms. Use for: PII redaction, persona overlay, refusal swap.
 * - **`onResponse`** — per-full-response rewrite. **Blocks streaming TTS**
 *   until the LLM stream completes, then runs once on the full text.
 *   Latency cost: 500 ms – 2 s. Use only when sentence-level rewrite is
 *   insufficient (e.g. structured output validation). Avoid in latency-
 *   sensitive paths.
 *
 * The legacy single-callable signature `(text, ctx) => string` is still
 * accepted; it maps to `onResponse` and emits a deprecation warning.
 */
export interface AfterLLMHook {
  onChunk?: (chunk: string) => string;
  onSentence?: (sentence: string, ctx: HookContext) => string | null | Promise<string | null>;
  onResponse?: (text: string, ctx: HookContext) => string | null | Promise<string | null>;
}

/** Legacy single-callable form of after_llm. Maps to `onResponse`. @deprecated Pass `{ onResponse }` instead. */
export type AfterLLMLegacy = (text: string, ctx: HookContext) => string | null | Promise<string | null>;

/** Optional callbacks fired at each stage of the STT→LLM→TTS pipeline. */
export interface PipelineHooks {
  /** Called with the raw PCM audio chunk before it is forwarded to the STT provider.
   *  Return null to drop the chunk (e.g., for custom VAD gating). */
  beforeSendToStt?: (audio: Buffer, ctx: HookContext) => Buffer | null | Promise<Buffer | null>;
  /** Called after STT produces a transcript, before LLM. Return null to skip this turn. */
  afterTranscribe?: (transcript: string, ctx: HookContext) => string | null | Promise<string | null>;
  /** Called with the messages list before the LLM call.
   *  Return null to keep them, or return a new list to replace
   *  (useful for prompt injection, message filtering, RAG augmentation). */
  beforeLlm?: (
    messages: Array<Record<string, unknown>>,
    ctx: HookContext,
  ) => Array<Record<string, unknown>> | null | Promise<Array<Record<string, unknown>> | null>;
  /**
   * Post-LLM transform. Pass either:
   * - the new **3-tier object** (`{ onChunk, onSentence, onResponse }`) for
   *   streaming-friendly per-chunk / per-sentence / per-response transforms;
   * - or the **legacy callable** `(text, ctx) => string` (deprecated) which
   *   maps to `onResponse` semantics and blocks streaming TTS.
   *
   * See `AfterLLMHook` for the full tier contract.
   */
  afterLlm?: AfterLLMHook | AfterLLMLegacy;
  /** Called before TTS, per-sentence in streaming mode. Return null to skip TTS for this sentence. */
  beforeSynthesize?: (text: string, ctx: HookContext) => string | null | Promise<string | null>;
  /** Called after TTS produces an audio chunk. Return null to discard this chunk. */
  afterSynthesize?: (audio: Buffer, text: string, ctx: HookContext) => Buffer | null | Promise<Buffer | null>;
}

/** Voice activity event emitted by a VADProvider. */
export interface VADEvent {
  readonly type: 'speech_start' | 'speech_end' | 'silence';
  readonly confidence?: number;
  readonly durationMs?: number;
}

/** Server-side voice activity detector. Integrated before STT in pipeline mode. */
export interface VADProvider {
  processFrame(pcmChunk: Buffer, sampleRate: number): Promise<VADEvent | null>;
  close(): Promise<void>;
  /**
   * Optional: reset all per-utterance state so the next ``processFrame``
   * starts from a clean SILENCE state. Useful between agent turns to
   * prevent a "stuck SPEECH" condition where PSTN echo / loopback kept the
   * detector's internal probability above the deactivation threshold for
   * the full agent turn, leaving the VAD unable to emit ``speech_start``
   * on the next user utterance (one-shot barge-in bug).
   */
  reset?(): Promise<void> | void;
}

/** Pre-STT audio filter — noise cancellation, gain, EQ. */
export interface AudioFilter {
  process(pcmChunk: Buffer, sampleRate: number): Promise<Buffer>;
  close(): Promise<void>;
}

/** Mixes background audio (hold music, thinking cues) with TTS output. */
export interface BackgroundAudioPlayer {
  start(): Promise<void>;
  mix(agentPcm: Buffer, sampleRate: number): Promise<Buffer>;
  stop(): Promise<void>;
}

/**
 * Configuration for a local-mode voice AI agent.
 *
 * Several fields (``voice``, ``model``, ``language``) are also carried by
 * engine markers (``OpenAIRealtime``, ``ElevenLabsConvAI``) and by the
 * server-instantiated adapters. When the same setting is set in two places,
 * precedence is:
 *
 * 1. **Explicit field on** ``phone.agent({ voice, model, language })`` always wins.
 * 2. Otherwise, when an ``engine`` is passed, the engine's value is used
 *    (see ``Patter.agent()`` for the resolution).
 * 3. Otherwise, the AgentOptions default is used.
 */
/** Configuration for a local-mode voice AI agent (passed to `phone.agent({...})`). */
export interface AgentOptions {
  readonly systemPrompt: string;
  /**
   * Voice preset. When ``engine`` is provided, its ``voice`` is used unless
   * explicitly overridden here. Format depends on the engine:
   * OpenAI Realtime accepts a name (``'alloy'``, ``'echo'``, ...);
   * ElevenLabs ConvAI accepts a voice ID.
   */
  readonly voice?: string;
  /**
   * LLM / Realtime model. When ``engine`` is provided, its ``model`` is used
   * unless explicitly overridden here.
   */
  readonly model?: string;
  /**
   * BCP-47 language code (e.g. ``'en'``, ``'it'``). Forwarded to STT (in
   * pipeline mode) and to the engine adapter at call time. STTConfig has its
   * own ``language`` field for the rare case where STT must use a different
   * language than the rest of the pipeline.
   */
  readonly language?: string;
  readonly firstMessage?: string;
  /** Tool definitions — ``Tool`` class instances from ``getpatter``. */
  readonly tools?: ReadonlyArray<ToolInstance>;
  /**
   * Model Context Protocol (MCP) servers to plug into this agent. Each
   * server is queried at call start via ``tools/list`` and its tools
   * are merged into ``tools`` with synthetic handlers that dispatch
   * back through the MCP client. Lets you connect to existing MCP
   * servers (Google Workspace, PayPal, GitHub, Postgres, …) without
   * writing a wrapper handler.
   *
   * Each entry is either a URL string (shorthand for
   * ``{ url, transport: 'streamable-http' }``) or an explicit object
   * with optional ``headers`` for auth and a ``name`` for telemetry.
   *
   * Requires the optional dependency ``@modelcontextprotocol/sdk``.
   * When unset, MCP is fully disabled and the SDK ships without the
   * dependency installed.
   *
   * Cost: one HTTP handshake + ``tools/list`` round-trip per server at
   * call start (~50-200 ms × N servers). Future iterations may cache
   * the discovered list process-wide.
   */
  readonly mcpServers?: ReadonlyArray<MCPServerConfig>;
  /**
   * Optional back-office "consult" escalation. When set, Patter auto-injects a
   * ``consult_agent`` tool (Realtime + Pipeline modes) that the in-call agent
   * can invoke to reach the caller's own orchestrator over HTTP for deeper
   * reasoning / fresh info, then speak the reply. The orchestrator stays off
   * the per-turn path — consulted only on demand. ``undefined`` (default)
   * disables it. See {@link ConsultConfig}.
   */
  readonly consult?: ConsultConfig;
  /**
   * When ``true``, ship ``systemPrompt`` to the LLM verbatim. Default
   * (``false``) prepends a phone-friendly preamble that instructs the
   * model to avoid markdown, emojis, bullet lists, and verbose replies —
   * the conventions live phone calls require.
   */
  readonly disablePhonePreamble?: boolean;
  /**
   * Acoustic echo cancellation. When `true` (pipeline mode only) the SDK
   * instantiates an `NlmsEchoCanceller` that subtracts the agent's own
   * TTS bleed from the inbound mic stream before VAD/STT see it.
   * Strongly recommended for speakerphone / tunnel deployments where the
   * bleed otherwise keeps VAD permanently in "speaking" state and
   * barge-in only fires during natural TTS pauses. Off by default —
   * handset / headset deployments don't have the bleed, and the 0.5–2 s
   * convergence period would briefly attenuate caller speech if they
   * spoke before any TTS played.
   */
  readonly echoCancellation?: boolean;
  /**
   * Realtime / ConvAI engine instance. When present, the agent runs in the
   * matching mode (``openai_realtime`` or ``elevenlabs_convai``). When absent,
   * pipeline mode is selected if ``stt`` and ``tts`` are provided.
   */
  readonly engine?: Realtime | Realtime2 | ConvAI;
  /**
   * Provider mode. Normally derived from ``engine`` / ``stt`` + ``tts``. Pass
   * ``'pipeline'`` explicitly when building a pipeline-mode agent without
   * an engine instance.
   */
  readonly provider?: 'openai_realtime' | 'elevenlabs_convai' | 'pipeline';
  /** Pre-instantiated STT adapter (e.g. ``new DeepgramSTT({ apiKey })``). */
  readonly stt?: STTAdapter;
  /** Pre-instantiated TTS adapter (e.g. ``new ElevenLabsTTS({ apiKey })``). */
  readonly tts?: TTSAdapter;
  /**
   * Pipeline-mode LLM provider (e.g. ``new AnthropicLLM()``). When set, the
   * built-in LLM loop uses this provider instead of the OpenAI default.
   * Mutually exclusive with ``onMessage`` passed to ``serve()``. Ignored
   * when ``engine`` is set (realtime mode bypasses the pipeline LLM).
   */
  readonly llm?: LLMProvider;
  /** Dynamic variables for ``{placeholder}`` substitution in systemPrompt at call time. */
  readonly variables?: Readonly<Record<string, string>>;
  /** Output guardrails — ``Guardrail`` class instances from ``getpatter``. */
  readonly guardrails?: ReadonlyArray<Guardrail>;
  /** Pipeline hooks — intercept and transform data at each pipeline stage (pipeline mode only). */
  readonly hooks?: PipelineHooks;
  /** Text transforms applied to LLM output before TTS (pipeline mode only).
   *  Each function receives a string and returns the transformed string.
   *  Applied in order before the ``beforeSynthesize`` hook. */
  readonly textTransforms?: ReadonlyArray<(text: string) => string>;
  /** Optional server-side VAD (e.g., Silero). Pipeline mode only. */
  readonly vad?: VADProvider;
  /** Optional pre-STT audio filter (noise cancellation). Pipeline mode only. */
  readonly audioFilter?: AudioFilter;
  /** Optional background audio mixer (hold music, thinking cues). Pipeline mode only. */
  readonly backgroundAudio?: BackgroundAudioPlayer;
  /**
   * Minimum sustained voice (ms) before treating caller audio as a barge-in
   * and interrupting TTS. `0` disables barge-in entirely — useful on noisy
   * links (ngrok tunnels, speakerphone) where the agent can hear itself.
   * Default: 300.
   */
  readonly bargeInThresholdMs?: number;
  /**
   * Opt-in barge-in confirmation strategies (pipeline mode). With the
   * default empty array the SDK falls back to the legacy
   * "interrupt immediately on VAD speech_start" behaviour. When at
   * least one strategy is provided, a VAD speech_start during TTS
   * marks the barge-in as *pending* — the agent's TTS continues
   * streaming naturally and its in-flight LLM stream is preserved —
   * and the strategies are consulted on every STT transcript. The first strategy that
   * returns ``true`` confirms the barge-in (cancels TTS, flushes the
   * inbound ring buffer); if none confirm within
   * ``bargeInConfirmMs`` the pending state is dropped and TTS resumes.
   *
   * See ``getpatter`` exports ``BargeInStrategy`` /
   * ``MinWordsStrategy`` for the protocol and a reference
   * implementation.
   */
  readonly bargeInStrategies?: readonly BargeInStrategy[];
  /**
   * Maximum time (ms) to wait for at least one strategy to confirm a
   * pending barge-in before discarding the pending state and resuming
   * TTS. Only consulted when ``bargeInStrategies`` is non-empty.
   * Default: 1500.
   */
  readonly bargeInConfirmMs?: number;
  /**
   * When ``true`` (default), ``Patter.call`` warms up the STT, TTS, and
   * LLM provider connections in parallel with the carrier-side
   * ``initiateCall`` request so DNS, TLS, and HTTP/2 handshakes are
   * already complete by the time the callee answers. Adapters expose a
   * ``warmup()`` method returning ``Promise<void>`` (default no-op) —
   * providers can override to dial open a persistent connection ahead
   * of the WebSocket bridge. Best-effort: warmup failures are logged
   * at debug level and never abort the call. Default: ``true``.
   */
  readonly prewarm?: boolean;
  /**
   * When ``true`` (default since 0.6.2 in pipeline mode), ``Patter.call``
   * pre-renders ``firstMessage`` to TTS audio bytes during the ringing
   * window and streams the cached buffer immediately when the carrier
   * emits ``start``. Eliminates the 200-700 ms TTS first-byte latency
   * on the greeting that dominated first-turn ``p95`` on every pipeline
   * acceptance run. The trade-off is paying the TTS bill even if the
   * call is never answered (silently logged at warn level when the call
   * fails) — typically $0.001-$0.005 per ringing call depending on TTS
   * provider. Opt out by passing ``prewarmFirstMessage: false`` (e.g.
   * for very high-volume outbound where un-answered TTS spend matters).
   *
   * **Pipeline mode only.** Realtime / ConvAI provider modes never
   * consume the prewarm cache (the StreamHandler for those modes runs
   * its first-message emit through the provider's own audio path), so
   * ``Patter.call`` refuses to spawn the prewarm task and emits a warn
   * when ``provider !== 'pipeline'``.
   */
  readonly prewarmFirstMessage?: boolean;
  /**
   * When true, the sentence chunker emits the first clause of each response
   * on a soft punctuation boundary (",", em-dash, en-dash) once ~40 chars
   * have accumulated. Saves 200–500 ms TTFA on the first sentence of each
   * turn at the cost of slightly clipping prosody on the very first chunk.
   * Hard-disabled when ``language`` starts with ``"it"`` (Italian decimal
   * comma would split mid-number). Default: false.
   *
   * See SentenceChunker constructor for the full guard list (decimal,
   * currency, balanced delimiter, ellipsis).
   */
  readonly aggressiveFirstFlush?: boolean;
}

/** Pipeline-mode message handler — given full turn context, returns the agent's reply. */
export type PipelineMessageHandler = (data: Record<string, unknown>) => Promise<string>;

/** Options for `Patter.serve({...})`. */
export interface ServeOptions {
  readonly agent: AgentOptions;
  readonly port?: number;
  /** When true, start a cloudflared tunnel automatically (requires `cloudflared` npm package). */
  readonly tunnel?: boolean;
  readonly onCallStart?: (data: Record<string, unknown>) => Promise<void>;
  readonly onCallEnd?: (data: Record<string, unknown>) => Promise<void>;
  readonly onTranscript?: (data: Record<string, unknown>) => Promise<void>;
  /** Pipeline mode only — called with the user's transcript; return value is spoken.
   *  Can also be a URL string for remote webhook/WebSocket integration. */
  readonly onMessage?: PipelineMessageHandler | string;
  /** Called after each turn with per-turn metrics. */
  readonly onMetrics?: (data: Record<string, unknown>) => Promise<void>;
  /** When true, record calls via the Twilio Recordings API. */
  readonly recording?: boolean;
  /** If set, spoken as a voicemail message when AMD detects a machine. */
  readonly voicemailMessage?: string;
  /** Custom pricing overrides for cost calculation. */
  readonly pricing?: Readonly<Record<string, Record<string, unknown>>>;
  /** When true (default), serve a dashboard UI at /dashboard. */
  readonly dashboard?: boolean;
  /** Bearer token for dashboard/API authentication. */
  readonly dashboardToken?: string;
  /** Path to SQLite database for dashboard persistence (not used in TS yet). */
  readonly dashboardDb?: string;
  /** When true (default), persist dashboard data. */
  readonly dashboardPersist?: boolean;
  /**
   * When true (default), `serve()` calls the carrier's API on startup to
   * point the configured phone number's webhook URL at this server. Set
   * to `false` when the webhook is managed externally (Terraform, an edge
   * gateway / voice-router, or any infra-as-code system) — otherwise every
   * boot will silently overwrite the externally-managed value.
   *
   * Required `false` when:
   *   - Twilio's voice_url should point at a router/gateway in front of
   *     this server rather than directly at it.
   *   - Multiple replicas share the same Twilio number; only one should
   *     write the webhook.
   *   - Compliance forbids the runtime from holding write credentials
   *     against the carrier console.
   *
   * Ignored (treated as true) when `tunnel: true`, because the tunnel
   * hostname is dynamic and only known at runtime — the carrier MUST be
   * reconfigured for inbound calls to land.
   */
  readonly manageWebhook?: boolean;
}

/**
 * Normalised AMD (answering-machine detection) result emitted to
 * ``LocalCallOptions.onMachineDetection`` once the carrier reports back.
 * The ``raw`` field preserves the provider value verbatim so callers can
 * apply provider-specific logic; ``classification`` is the SDK's
 * carrier-agnostic projection that test/acceptance code should check.
 */
export interface MachineDetectionResult {
  readonly call_id: string;
  readonly carrier: CarrierKind;
  /** Carrier-agnostic projection. Use this in app code unless you really need the raw provider value. */
  readonly classification: 'human' | 'machine' | 'fax' | 'unknown';
  /**
   * Raw provider value:
   * - Twilio: ``human``, ``machine_start``, ``machine_end_beep``,
   *   ``machine_end_silence``, ``machine_end_other``, ``fax``, ``unknown``.
   * - Telnyx: ``human``, ``machine``, ``not_sure``.
   */
  readonly raw: string;
  /** Unix epoch seconds at which the result was received from the carrier. */
  readonly detected_at: number;
}

/** Options for `Patter.call({...})` to place an outbound call. */
export interface LocalCallOptions {
  readonly to: string;
  readonly agent: AgentOptions;
  /**
   * Enable answering-machine detection. **Defaults to ``true``** — the SDK
   * asks Twilio (``MachineDetection=DetectMessageEnd`` + Async AMD) or
   * Telnyx (``answering_machine_detection=greeting_end``) to classify
   * whoever picks up. Async AMD on Twilio adds ~0 answer-latency on human
   * pickups (the call connects immediately and the result arrives via
   * webhook 2-5 s later), so ON-by-default is safe. Pass ``false`` to
   * disable when you want to skip per-call AMD billing or you already
   * know the destination is a human.
   */
  readonly machineDetection?: boolean;
  /**
   * Called once when the carrier finishes the AMD check. Fires for both
   * ``human`` and ``machine`` outcomes. Combine with ``voicemailMessage``
   * to get both the legacy voicemail-drop AND a result callback (the SDK
   * fires the callback after the drop is queued). Acceptance tests use
   * this to mark a run INVALID when ``classification !== 'human'``.
   */
  readonly onMachineDetection?: (result: MachineDetectionResult) => void | Promise<void>;
  /** If set, spoken as a voicemail message when AMD detects a machine. Implicitly enables ``machineDetection``. */
  readonly voicemailMessage?: string;
  /** Dynamic variables merged into agent.variables before call. Override agent-level variables. */
  readonly variables?: Readonly<Record<string, string>>;
  /**
   * Ring timeout in seconds. Forwarded to Twilio as `Timeout` and to Telnyx
   * as `timeout_secs`. Defaults to **25 s** — the production-recommended
   * value that limits phantom calls. Pass `60` for legacy carrier-default
   * parity, or `null` to omit the parameter entirely (carrier picks its
   * own default).
   */
  readonly ringTimeout?: number | null;
  /**
   * When `true`, block until the call reaches a terminal state and resolve
   * to a {@link CallResult} (`outcome` ∈ answered / voicemail / no_answer /
   * busy / failed, plus duration, transcript, cost). **Requires an active
   * server** — call `serve(...)` first or use `await using phone = ...`
   * (the {@link Patter[Symbol.asyncDispose]} disposer) — because the
   * terminal signals (carrier status callback, AMD, media-stream end) are
   * delivered to the embedded server's webhooks. The default (`false`) is
   * fire-and-forget and resolves to `void` the instant the carrier accepts
   * the dial (unchanged behaviour).
   *
   * Mirrors Python's `Patter.call(..., wait=True)`.
   */
  readonly wait?: boolean;
}

/**
 * Carrier-agnostic terminal outcomes for an outbound call. `answered` means a
 * human (or at least a live connection) picked up and the conversation ran;
 * `voicemail` means AMD classified the callee as a machine; the remaining
 * three come straight from the carrier status callback when the call never
 * reaches the media stream. Mirrors `CallOutcome` in
 * `libraries/python/getpatter/models.py`.
 */
export type CallOutcome = 'answered' | 'voicemail' | 'no_answer' | 'busy' | 'failed';

/**
 * Structured outcome of an outbound call placed with `call({ wait: true })`.
 *
 * Resolved only when `call({ ..., wait: true })` is awaited — a
 * fire-and-forget `call()` (the default, `wait: false`) still resolves to
 * `void` for backward compatibility. Every field is derived from a real
 * carrier signal: `answered` / `voicemail` from the AMD result + media-stream
 * end, `no_answer` / `busy` / `failed` from the carrier status callback when
 * the call terminates before any media flows.
 *
 * Mirrors `CallResult` in `libraries/python/getpatter/models.py` (snake_case
 * fields there, same positions).
 */
export interface CallResult {
  readonly callId: string;
  readonly outcome: CallOutcome;
  /**
   * Carrier-raw final status verbatim (e.g. "completed", "no-answer",
   * "busy", "failed"). `outcome` is the carrier-agnostic projection to check
   * in code; `status` is preserved for logging / debugging.
   */
  readonly status: string;
  readonly durationSeconds: number;
  readonly transcript: readonly { role: string; text: string; timestamp?: number }[];
  /**
   * Populated only when the call connected (`answered` / `voicemail`).
   * `cost.total` is the headline USD figure. `null` for calls that never
   * reached media (`no_answer` / `busy` / `failed`).
   */
  readonly cost: CostBreakdown | null;
  readonly metrics: CallMetrics | null;
}

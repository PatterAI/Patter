import type { Carrier as TwilioCarrier } from "./carriers/twilio";
import type { Carrier as TelnyxCarrier } from "./carriers/telnyx";
import type { Realtime } from "./engines/openai";
import type { ConvAI } from "./engines/elevenlabs";
import type { CloudflareTunnel, Static as StaticTunnel } from "./tunnels";
import type { Tool as ToolInstance } from "./public-api";
import type { STTAdapter, TTSAdapter } from "./provider-factory";
import type { LLMProvider } from "./llm-loop";

export interface IncomingMessage {
  readonly text: string;
  readonly callId: string;
  readonly caller: string;
}

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

export type MessageHandler = (msg: IncomingMessage) => Promise<string>;
export type CallEventHandler = (data: Record<string, unknown>) => Promise<void>;

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  /** Webhook URL — called when the LLM invokes this tool. Mutually exclusive with handler. */
  webhookUrl?: string;
  /** Local handler function — when provided, called instead of webhookUrl. */
  handler?: (args: Record<string, unknown>, context: Record<string, unknown>) => Promise<string>;
}

// === Local mode ===

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
  carrier: TwilioCarrier | TelnyxCarrier;
  /**
   * Tunnel configuration. Accepts a tunnel instance, ``true`` (alias for
   * ``new CloudflareTunnel()``), or ``false`` / omitted (no tunnel).
   */
  tunnel?: CloudflareTunnel | StaticTunnel | boolean;
  phoneNumber: string;
  webhookUrl?: string;
  /**
   * @internal — allows ``StreamHandler`` to build the default OpenAI
   * ``LLMLoop`` when no ``onMessage`` handler is supplied. The
   * ``OpenAIRealtime`` engine instance carries its own key when one is
   * used via ``phone.agent({ engine: new OpenAIRealtime({ apiKey }) })``.
   */
  openaiKey?: string;
}

export interface Guardrail {
  /** Name for logging when triggered */
  name: string;
  /** List of terms that trigger the guardrail (case-insensitive) */
  blockedTerms?: string[];
  /** Custom check function — return true to block the response */
  check?: (text: string) => boolean;
  /** Replacement text spoken when guardrail triggers */
  replacement?: string;
}

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

export interface AgentOptions {
  systemPrompt: string;
  /**
   * Voice preset. When ``engine`` is provided, its ``voice`` is used unless
   * explicitly overridden here.
   */
  voice?: string;
  /**
   * LLM / Realtime model. When ``engine`` is provided, its ``model`` is used
   * unless explicitly overridden here.
   */
  model?: string;
  language?: string;
  firstMessage?: string;
  /** Tool definitions — ``Tool`` class instances from ``getpatter``. */
  tools?: Array<ToolInstance>;
  /**
   * Realtime / ConvAI engine instance. When present, the agent runs in the
   * matching mode (``openai_realtime`` or ``elevenlabs_convai``). When absent,
   * pipeline mode is selected if ``stt`` and ``tts`` are provided.
   */
  engine?: Realtime | ConvAI;
  /**
   * Provider mode. Normally derived from ``engine`` / ``stt`` + ``tts``. Pass
   * ``'pipeline'`` explicitly when building a pipeline-mode agent without
   * an engine instance.
   */
  provider?: 'openai_realtime' | 'elevenlabs_convai' | 'pipeline';
  /** Pre-instantiated STT adapter (e.g. ``new DeepgramSTT({ apiKey })``). */
  stt?: STTAdapter;
  /** Pre-instantiated TTS adapter (e.g. ``new ElevenLabsTTS({ apiKey })``). */
  tts?: TTSAdapter;
  /**
   * Pipeline-mode LLM provider (e.g. ``new AnthropicLLM()``). When set, the
   * built-in LLM loop uses this provider instead of the OpenAI default.
   * Mutually exclusive with ``onMessage`` passed to ``serve()``. Ignored
   * when ``engine`` is set (realtime mode bypasses the pipeline LLM).
   */
  llm?: LLMProvider;
  /** Dynamic variables for ``{placeholder}`` substitution in systemPrompt at call time. */
  variables?: Record<string, string>;
  /** Output guardrails — ``Guardrail`` class instances from ``getpatter``. */
  guardrails?: Array<Guardrail>;
  /** Pipeline hooks — intercept and transform data at each pipeline stage (pipeline mode only). */
  hooks?: PipelineHooks;
  /** Text transforms applied to LLM output before TTS (pipeline mode only).
   *  Each function receives a string and returns the transformed string.
   *  Applied in order before the ``beforeSynthesize`` hook. */
  textTransforms?: Array<(text: string) => string>;
  /** Optional server-side VAD (e.g., Silero). Pipeline mode only. */
  vad?: VADProvider;
  /** Optional pre-STT audio filter (noise cancellation). Pipeline mode only. */
  audioFilter?: AudioFilter;
  /** Optional background audio mixer (hold music, thinking cues). Pipeline mode only. */
  backgroundAudio?: BackgroundAudioPlayer;
  /**
   * Minimum sustained voice (ms) before treating caller audio as a barge-in
   * and interrupting TTS. `0` disables barge-in entirely — useful on noisy
   * links (ngrok tunnels, speakerphone) where the agent can hear itself.
   * Default: 300.
   */
  bargeInThresholdMs?: number;
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
  aggressiveFirstFlush?: boolean;
}

export type PipelineMessageHandler = (data: Record<string, unknown>) => Promise<string>;

export interface ServeOptions {
  agent: AgentOptions;
  port?: number;
  /** When true, start a cloudflared tunnel automatically (requires `cloudflared` npm package). */
  tunnel?: boolean;
  onCallStart?: (data: Record<string, unknown>) => Promise<void>;
  onCallEnd?: (data: Record<string, unknown>) => Promise<void>;
  onTranscript?: (data: Record<string, unknown>) => Promise<void>;
  /** Pipeline mode only — called with the user's transcript; return value is spoken.
   *  Can also be a URL string for remote webhook/WebSocket integration. */
  onMessage?: PipelineMessageHandler | string;
  /** Called after each turn with per-turn metrics. */
  onMetrics?: (data: Record<string, unknown>) => Promise<void>;
  /** When true, record calls via the Twilio Recordings API. */
  recording?: boolean;
  /** If set, spoken as a voicemail message when AMD detects a machine. */
  voicemailMessage?: string;
  /** Custom pricing overrides for cost calculation. */
  pricing?: Record<string, Record<string, unknown>>;
  /** When true (default), serve a dashboard UI at /dashboard. */
  dashboard?: boolean;
  /** Bearer token for dashboard/API authentication. */
  dashboardToken?: string;
  /** Path to SQLite database for dashboard persistence (not used in TS yet). */
  dashboardDb?: string;
  /** When true (default), persist dashboard data. */
  dashboardPersist?: boolean;
}

export interface LocalCallOptions {
  to: string;
  agent: AgentOptions;
  machineDetection?: boolean;
  /** If set, spoken as a voicemail message when AMD detects a machine. Requires machineDetection=true. */
  voicemailMessage?: string;
  /** Dynamic variables merged into agent.variables before call. Override agent-level variables. */
  variables?: Record<string, string>;
  /**
   * Ring timeout in seconds. Forwarded to Twilio as `Timeout` and to Telnyx
   * as `timeout_secs`. Defaults to **25 s** — the production-recommended
   * value that limits phantom calls. Pass `60` for legacy carrier-default
   * parity, or `null` to omit the parameter entirely (carrier picks its
   * own default).
   */
  ringTimeout?: number | null;
}

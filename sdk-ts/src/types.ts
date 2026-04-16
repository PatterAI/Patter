export interface IncomingMessage {
  readonly text: string;
  readonly callId: string;
  readonly caller: string;
}

export interface STTConfig {
  readonly provider: string;
  readonly apiKey: string;
  readonly language: string;
  toDict(): Record<string, string>;
}

export interface TTSConfig {
  readonly provider: string;
  readonly apiKey: string;
  readonly voice: string;
  toDict(): Record<string, string>;
}

export type MessageHandler = (msg: IncomingMessage) => Promise<string>;
export type CallEventHandler = (data: Record<string, unknown>) => Promise<void>;

export interface PatterOptions {
  apiKey: string;
  backendUrl?: string;
  restUrl?: string;
}

export interface ConnectOptions {
  onMessage: MessageHandler;
  onCallStart?: CallEventHandler;
  onCallEnd?: CallEventHandler;
  provider?: string;
  providerKey?: string;
  providerSecret?: string;
  number?: string;
  country?: string;
  stt?: STTConfig;
  tts?: TTSConfig;
}

export interface CallOptions {
  to: string;
  onMessage?: MessageHandler;
  firstMessage?: string;
  fromNumber?: string;
  agentId?: string;
  machineDetection?: boolean;
}

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  /** Webhook URL — called when the LLM invokes this tool. Mutually exclusive with handler. */
  webhookUrl?: string;
  /** Local handler function — when provided, called instead of webhookUrl. */
  handler?: (args: Record<string, unknown>, context: Record<string, unknown>) => Promise<string>;
}

export interface CreateAgentOptions {
  name: string;
  systemPrompt: string;
  model?: string;
  voice?: string;
  voiceProvider?: string;
  language?: string;
  firstMessage?: string;
  tools?: ToolDefinition[];
}

export interface Agent {
  id: string;
  name: string;
  systemPrompt: string;
  model: string;
  voice: string;
  voiceProvider: string;
  language: string;
  firstMessage: string | null;
  tools: ToolDefinition[] | null;
}

export interface PhoneNumber {
  id: string;
  number: string;
  provider: string;
  country: string;
  status: string;
  agentId: string | null;
}

export interface Call {
  id: string;
  direction: string;
  caller: string;
  callee: string;
  startedAt: string;
  endedAt: string | null;
  durationSeconds: number | null;
  status: string;
  transcript: Array<{ role: string; text: string; timestamp: string }> | null;
}

// === Local mode ===

export interface LocalOptions {
  mode: 'local';
  twilioSid?: string;
  twilioToken?: string;
  openaiKey?: string;
  phoneNumber: string;
  webhookUrl?: string;
  telephonyProvider?: 'twilio' | 'telnyx';
  telnyxKey?: string;
  telnyxConnectionId?: string;
  /**
   * Telnyx Ed25519 public key (base64-encoded, DER/SPKI format) for webhook
   * signature verification. When provided, unauthenticated requests are rejected.
   */
  telnyxPublicKey?: string;
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

export interface PipelineHooks {
  /** Called after STT produces a transcript, before LLM. Return null to skip this turn. */
  afterTranscribe?: (transcript: string, ctx: HookContext) => string | null | Promise<string | null>;
  /** Called before TTS, per-sentence in streaming mode. Return null to skip TTS for this sentence. */
  beforeSynthesize?: (text: string, ctx: HookContext) => string | null | Promise<string | null>;
  /** Called after TTS produces an audio chunk. Return null to discard this chunk. */
  afterSynthesize?: (audio: Buffer, text: string, ctx: HookContext) => Buffer | null | Promise<Buffer | null>;
}

export interface AgentOptions {
  systemPrompt: string;
  voice?: string;
  model?: string;
  language?: string;
  firstMessage?: string;
  tools?: ToolDefinition[];
  provider?: 'openai_realtime' | 'elevenlabs_convai' | 'pipeline';
  elevenlabsKey?: string;
  elevenlabsAgentId?: string;
  deepgramKey?: string;
  /** STT provider config for pipeline mode. Use ``Patter.deepgram()`` or ``Patter.whisper()``. */
  stt?: STTConfig;
  /** TTS provider config for pipeline mode. Use ``Patter.elevenlabs()`` or ``Patter.openaiTts()``. */
  tts?: TTSConfig;
  /** Dynamic variables for ``{placeholder}`` substitution in systemPrompt at call time. */
  variables?: Record<string, string>;
  /** Output guardrails — filter AI responses before TTS */
  guardrails?: Guardrail[];
  /** Pipeline hooks — intercept and transform data at each pipeline stage (pipeline mode only). */
  hooks?: PipelineHooks;
  /** Text transforms applied to LLM output before TTS (pipeline mode only).
   *  Each function receives a string and returns the transformed string.
   *  Applied in order before the ``beforeSynthesize`` hook. */
  textTransforms?: Array<(text: string) => string>;
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
}

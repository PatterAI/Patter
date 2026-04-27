export { Patter } from "./client";
export { defineTool } from "./tool-decorator";
export type { DefineToolInput, ParamSpec } from "./tool-decorator";
export type { Logger } from "./logger";
export { getLogger, setLogger } from "./logger";
export type {
  IncomingMessage,
  STTConfig,
  TTSConfig,
  LocalOptions,
  AgentOptions,
  ServeOptions,
  LocalCallOptions,
  MessageHandler,
  CallEventHandler,
  PipelineMessageHandler,
  ToolDefinition,
  PipelineHooks,
  HookContext,
} from "./types";
// `Guardrail` is intentionally not re-exported from `./types` — the public
// `Guardrail` identifier is the class from `./public-api` (exported below),
// which is structurally compatible with the internal interface.
export { SentenceChunker, DEFAULT_MIN_SENTENCE_LEN } from "./sentence-chunker";
export { PipelineHookExecutor } from "./pipeline-hooks";
export { filterMarkdown, filterEmoji, filterForTTS } from "./text-transforms";
export {
  PatterError,
  PatterConnectionError,
  AuthenticationError,
  ProvisionError,
  RateLimitError,
} from "./errors";
export {
  deepgram,
  whisper,
  elevenlabs,
  openaiTts,
  soniox,
  speechmatics,
  assemblyai,
  cartesia,
  rime,
  lmnt,
  ultravox,
  geminiLive,
} from "./providers";
export type { RealtimeConfig } from "./providers";
export { DEFAULT_PRICING, mergePricing, calculateSttCost, calculateTtsCost, calculateRealtimeCost, calculateTelephonyCost } from "./pricing";
export type { ProviderPricing } from "./pricing";
export { CallMetricsAccumulator } from "./metrics";
export type { LatencyBreakdown, CostBreakdown, TurnMetrics, CallMetrics, CallControl } from "./metrics";
export type { LocalConfig } from "./server";
export { MetricsStore } from "./dashboard/store";
export type { CallRecord, SSEEvent } from "./dashboard/store";
export { makeAuthMiddleware } from "./dashboard/auth";
export { callsToCsv, callsToJson } from "./dashboard/export";
export { mountDashboard, mountApi } from "./dashboard/routes";
export { notifyDashboard } from "./dashboard/persistence";
export { LLMLoop, OpenAILLMProvider, DefaultToolExecutor } from "./llm-loop";
export type {
  LLMProvider,
  LLMChunk,
  ToolExecutor,
  DefaultToolExecutorOptions,
} from "./llm-loop";
export { FallbackLLMProvider, AllProvidersFailedError, PartialStreamError } from "./fallback-provider";
export type { FallbackLLMProviderOptions } from "./fallback-provider";
export { RemoteMessageHandler, isRemoteUrl, isWebSocketUrl } from "./remote-message";
export { TestSession } from "./test-mode";
export { ElevenLabsConvAIAdapter } from "./providers/elevenlabs-convai";
export { OpenAIRealtimeAdapter } from "./providers/openai-realtime";
export { GeminiLiveAdapter, GEMINI_DEFAULT_INPUT_SR, GEMINI_DEFAULT_OUTPUT_SR } from "./providers/gemini-live";
export type { GeminiLiveEventHandler } from "./providers/gemini-live";
export { UltravoxRealtimeAdapter, ULTRAVOX_DEFAULT_API_BASE, ULTRAVOX_DEFAULT_SR } from "./providers/ultravox-realtime";
export type { UltravoxEventHandler } from "./providers/ultravox-realtime";
export { scheduleCron, scheduleOnce, scheduleInterval } from "./scheduler";
export type { ScheduleHandle, JobCallback } from "./scheduler";
// Provider adapter types (re-exported for advanced users who build custom
// pipelines). The concrete wrapper classes are exported below under the
// namespaced STT/TTS names (Phase 1a of the v0.5.0 API refactor).
export type { SonioxSTTOptions } from "./providers/soniox-stt";
export type { AssemblyAIModel, AssemblyAIEncoding } from "./providers/assemblyai-stt";
export type { CartesiaEncoding } from "./providers/cartesia-stt";
export type { LMNTAudioFormat, LMNTModel, LMNTSampleRate } from "./providers/lmnt-tts";

// New namespaced STT classes — options-object constructor with env fallback.
export { STT as DeepgramSTT } from "./stt/deepgram";
export type { DeepgramSTTOptions } from "./stt/deepgram";
export { STT as WhisperSTT } from "./stt/whisper";
export type { WhisperSTTOptions } from "./stt/whisper";
export { STT as OpenAITranscribeSTT } from "./stt/openai-transcribe";
export type { OpenAITranscribeSTTOptions } from "./stt/openai-transcribe";
export { STT as CartesiaSTT } from "./stt/cartesia";
export type { CartesiaSTTOptions } from "./stt/cartesia";
export { STT as SonioxSTT } from "./stt/soniox";
export { STT as AssemblyAISTT } from "./stt/assemblyai";
export type { AssemblyAISTTOptions } from "./stt/assemblyai";

// New namespaced TTS classes.
export { TTS as ElevenLabsTTS } from "./tts/elevenlabs";
export type { ElevenLabsTTSOptions } from "./tts/elevenlabs";
export { TTS as OpenAITTS } from "./tts/openai";
export type { OpenAITTSOptions } from "./tts/openai";
export { TTS as CartesiaTTS } from "./tts/cartesia";
export type { CartesiaTTSOptions } from "./tts/cartesia";
export { TTS as RimeTTS } from "./tts/rime";
export type { RimeTTSOptions } from "./tts/rime";
export { TTS as LMNTTTS } from "./tts/lmnt";
export type { LMNTTTSOptions } from "./tts/lmnt";

// New namespaced LLM classes (Phase 2 of the v0.5.x API refactor).
export { LLM as OpenAILLM } from "./llm/openai";
export type { OpenAILLMOptions } from "./llm/openai";
export { LLM as AnthropicLLM } from "./llm/anthropic";
export type { AnthropicLLMOptions } from "./llm/anthropic";
export { LLM as GroqLLM } from "./llm/groq";
export type { GroqLLMOptions } from "./llm/groq";
export { LLM as CerebrasLLM } from "./llm/cerebras";
export type { CerebrasLLMOptions } from "./llm/cerebras";
export { LLM as GoogleLLM } from "./llm/google";
export type { GoogleLLMOptions } from "./llm/google";

// Voice Activity Detection (server-side) — Silero ONNX.
export { SileroVAD } from "./providers/silero-vad";
export type { SileroVADOptions, SileroSampleRate } from "./providers/silero-vad";

// Telephony carriers.
export { Carrier as Twilio } from "./carriers/twilio";
export type { TwilioCarrierOptions } from "./carriers/twilio";
export { Carrier as Telnyx } from "./carriers/telnyx";
export type { TelnyxCarrierOptions } from "./carriers/telnyx";

// Realtime / ConvAI engines.
export { Realtime as OpenAIRealtime } from "./engines/openai";
export type { RealtimeOptions as OpenAIRealtimeOptions } from "./engines/openai";
export { ConvAI as ElevenLabsConvAI } from "./engines/elevenlabs";
export type { ConvAIOptions as ElevenLabsConvAIOptions } from "./engines/elevenlabs";

// Tunnel markers.
export { CloudflareTunnel, Ngrok, Static as StaticTunnel } from "./tunnels";

// Public API primitives.
export { Tool, Guardrail, tool, guardrail } from "./public-api";
export type { ToolOptions, GuardrailOptions, ToolHandler } from "./public-api";
export {
  mulawToPcm16,
  pcm16ToMulaw,
  resample8kTo16k,
  resample16kTo8k,
  resample24kTo16k,
  StatefulResampler,
  PcmCarry,
  createResampler16kTo8k,
  createResampler8kTo16k,
  createResampler24kTo16k,
} from "./transcoding";
export type { StatefulResamplerOptions } from "./transcoding";
export { startTunnel } from "./tunnel";
export type { TunnelHandle } from "./tunnel";
export { ChatContext } from "./chat-context";
export type { ChatMessage, ChatRole, OpenAIMessage, AnthropicMessage, AnthropicConversion } from "./chat-context";
export {
  IVRActivity,
  TfidfLoopDetector,
  DTMF_EVENTS,
  formatDtmf,
} from "./services/ivr";
export type {
  DtmfEvent,
  IVRActivityOptions,
  IVRToolDefinition,
  TfidfLoopDetectorOptions,
  LoopCallback,
  SilenceCallback,
} from "./services/ivr";
export {
  BackgroundAudioPlayer,
  BuiltinAudioClip,
  builtinClipPath,
  mixPcm,
  resamplePcm,
  selectSoundFromList,
} from "./services/background-audio";
export type {
  AudioConfig,
  AudioSource,
  BackgroundAudioOptions,
  BuiltinAudioClipName,
  BuiltinPcmSource,
  FilePcmSource,
  RawPcmSource,
} from "./services/background-audio";

// Telephony adapters — direct REST clients mirroring the Python adapters.
export { TwilioAdapter } from "./providers/twilio-adapter";
export type {
  TwilioAdapterOptions,
  ProvisionNumberOptions as TwilioProvisionNumberOptions,
  ProvisionNumberResult as TwilioProvisionNumberResult,
  ConfigureNumberOptions as TwilioConfigureNumberOptions,
  InitiateCallOptions as TwilioInitiateCallOptions,
  InitiateCallResult as TwilioInitiateCallResult,
} from "./providers/twilio-adapter";
export { TelnyxAdapter } from "./providers/telnyx-adapter";
export type {
  ProvisionNumberOptions as TelnyxProvisionNumberOptions,
  ProvisionNumberResult as TelnyxProvisionNumberResult,
  ConfigureNumberOptions as TelnyxConfigureNumberOptions,
  InitiateCallOptions as TelnyxInitiateCallOptions,
  InitiateCallResult as TelnyxInitiateCallResult,
  EndCallOptions as TelnyxEndCallOptions,
} from "./providers/telnyx-adapter";

// Observability — OTel-compatible tracing (optional peer dep).
export {
  initTracing,
  startSpan,
  isTracingEnabled,
  EventBus,
  SPAN_CALL,
  SPAN_STT,
  SPAN_LLM,
  SPAN_TTS,
  SPAN_TOOL,
} from "./observability";
export type {
  Span,
  InitTracingOptions,
  CallEvent,
  PatterEventType,
} from "./observability";

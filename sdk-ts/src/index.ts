export { Patter } from "./client";
export { defineTool } from "./tool-decorator";
export type { DefineToolInput, ParamSpec } from "./tool-decorator";
export type { Logger } from "./logger";
export { getLogger, setLogger } from "./logger";
export type {
  IncomingMessage,
  STTConfig,
  TTSConfig,
  PatterOptions,
  LocalOptions,
  AgentOptions,
  ServeOptions,
  LocalCallOptions,
  ConnectOptions,
  CallOptions,
  MessageHandler,
  CallEventHandler,
  PipelineMessageHandler,
  ToolDefinition,
  CreateAgentOptions,
  Agent,
  PhoneNumber,
  Call,
  Guardrail,
  PipelineHooks,
  HookContext,
} from "./types";
export { SentenceChunker, DEFAULT_MIN_SENTENCE_LEN } from "./sentence-chunker";
export { PipelineHookExecutor } from "./pipeline-hooks";
export { filterMarkdown, filterEmoji, filterForTTS } from "./text-transforms";
export {
  PatterError,
  PatterConnectionError,
  AuthenticationError,
  ProvisionError,
} from "./errors";
export { deepgram, whisper, elevenlabs, openaiTts } from "./providers";
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
export { LLMLoop, OpenAILLMProvider } from "./llm-loop";
export type { LLMProvider, LLMChunk } from "./llm-loop";
export { FallbackLLMProvider, AllProvidersFailedError, PartialStreamError } from "./fallback-provider";
export type { FallbackLLMProviderOptions } from "./fallback-provider";
export { RemoteMessageHandler, isRemoteUrl, isWebSocketUrl } from "./remote-message";
export { TestSession } from "./test-mode";
export { ElevenLabsConvAIAdapter } from "./providers/elevenlabs-convai";
export { OpenAIRealtimeAdapter } from "./providers/openai-realtime";
export { DeepgramSTT } from "./providers/deepgram-stt";
export { SonioxSTT } from "./providers/soniox-stt";
export type { SonioxSTTOptions } from "./providers/soniox-stt";
export { WhisperSTT } from "./providers/whisper-stt";
export { ElevenLabsTTS } from "./providers/elevenlabs-tts";
export { OpenAITTS } from "./providers/openai-tts";
export {
  mulawToPcm16,
  pcm16ToMulaw,
  resample8kTo16k,
  resample16kTo8k,
  resample24kTo16k,
} from "./transcoding";
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

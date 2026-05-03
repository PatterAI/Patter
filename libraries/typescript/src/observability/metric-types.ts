/**
 * Typed metric payload shapes for Patter observability events.
 *
 * These interfaces mirror the Python dataclasses in
 * ``libraries/python/getpatter/observability/metric_types.py`` and are emitted via
 * ``EventBus`` from ``CallMetricsAccumulator``.
 */

/** Provider/model metadata attached to most metric payloads. */
export interface Metadata {
  modelName?: string | null;
  modelProvider?: string | null;
}

// ---- LLM usage ----

/** Token-usage breakdown for a single LLM completion. */
export interface LLMUsage {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  promptCachedTokens?: number;
  cacheCreationTokens?: number;
  cacheReadTokens?: number;
}

// ---- Realtime usage ----

/** Per-modality breakdown of cached tokens reported by Realtime providers. */
export interface CachedTokenDetails {
  audioTokens: number;
  textTokens: number;
  imageTokens: number;
}

/** Realtime input-token breakdown (audio, text, image, cached). */
export interface InputTokenDetails {
  audioTokens: number;
  textTokens: number;
  imageTokens: number;
  cachedTokens: number;
  cachedTokensDetails?: CachedTokenDetails | null;
}

/** Realtime output-token breakdown (text, audio, image). */
export interface OutputTokenDetails {
  textTokens: number;
  audioTokens: number;
  imageTokens: number;
}

/** Aggregate token-and-duration usage for a Realtime session. */
export interface RealtimeUsage {
  sessionDurationSeconds: number;
  tokensPerSecond: number;
  inputTokenDetails: InputTokenDetails;
  outputTokenDetails: OutputTokenDetails;
  metadata?: Metadata | null;
}

// ---- EOU metrics ----

/**
 * End-of-utterance timing breakdown.
 *
 * ``endOfUtteranceDelay``     ms from VAD stop → STT final.
 * ``transcriptionDelay``      ms from VAD stop → turn committed to LLM.
 * ``onUserTurnCompletedDelay`` ms from turn committed → pipeline hook done.
 */
export interface EOUMetrics {
  timestamp: number;
  endOfUtteranceDelay: number;
  transcriptionDelay: number;
  onUserTurnCompletedDelay: number;
  speechId?: string | null;
  metadata?: Metadata | null;
}

// ---- Interruption metrics ----

/**
 * Barge-in / interruption measurement.
 *
 * ``predictionDuration`` is always 0 in the simplified no-ML implementation;
 * it is reserved for a future ML-based overlap classifier.
 */
export interface InterruptionMetrics {
  timestamp: number;
  totalDuration: number;
  predictionDuration: number;
  detectionDelay: number;
  numInterruptions: number;
  numBackchannels: number;
  metadata?: Metadata | null;
}

// ---- TTFB / processing metrics ----

/** Time-to-first-byte for a single processor (STT/LLM/TTS). */
export interface TTFBMetrics {
  timestamp: number;
  processor: string;
  model?: string | null;
  value: number;
}

/** Total processing time for a single processor stage. */
export interface ProcessingMetrics {
  timestamp: number;
  processor: string;
  model?: string | null;
  value: number;
}

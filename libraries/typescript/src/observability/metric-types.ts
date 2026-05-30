/**
 * Typed metric payload shapes for Patter observability events.
 *
 * These interfaces mirror the Python dataclasses in
 * ``libraries/python/getpatter/observability/metric_types.py`` and are emitted via
 * ``EventBus`` from ``CallMetricsAccumulator``.
 */

/** Provider/model metadata attached to most metric payloads. */
export interface Metadata {
  readonly modelName?: string | null;
  readonly modelProvider?: string | null;
}

// ---- LLM usage ----

/** Token-usage breakdown for a single LLM completion. */
export interface LLMUsage {
  readonly promptTokens: number;
  readonly completionTokens: number;
  readonly totalTokens: number;
  readonly promptCachedTokens?: number;
  readonly cacheCreationTokens?: number;
  readonly cacheReadTokens?: number;
}

// ---- Realtime usage ----

/** Per-modality breakdown of cached tokens reported by Realtime providers. */
export interface CachedTokenDetails {
  readonly audioTokens: number;
  readonly textTokens: number;
  readonly imageTokens: number;
}

/** Realtime input-token breakdown (audio, text, image, cached). */
export interface InputTokenDetails {
  readonly audioTokens: number;
  readonly textTokens: number;
  readonly imageTokens: number;
  readonly cachedTokens: number;
  readonly cachedTokensDetails?: CachedTokenDetails | null;
}

/** Realtime output-token breakdown (text, audio, image). */
export interface OutputTokenDetails {
  readonly textTokens: number;
  readonly audioTokens: number;
  readonly imageTokens: number;
}

/** Aggregate token-and-duration usage for a Realtime session. */
export interface RealtimeUsage {
  readonly sessionDurationSeconds: number;
  readonly tokensPerSecond: number;
  readonly inputTokenDetails: InputTokenDetails;
  readonly outputTokenDetails: OutputTokenDetails;
  readonly metadata?: Metadata | null;
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
  readonly timestamp: number;
  readonly endOfUtteranceDelay: number;
  readonly transcriptionDelay: number;
  readonly onUserTurnCompletedDelay: number;
  readonly speechId?: string | null;
  readonly metadata?: Metadata | null;
}

// ---- Interruption metrics ----

/**
 * Barge-in / interruption measurement.
 *
 * ``predictionDuration`` is always 0 in the simplified no-ML implementation;
 * it is reserved for a future ML-based overlap classifier.
 */
export interface InterruptionMetrics {
  readonly timestamp: number;
  readonly totalDuration: number;
  readonly predictionDuration: number;
  readonly detectionDelay: number;
  readonly numInterruptions: number;
  readonly numBackchannels: number;
  readonly metadata?: Metadata | null;
}

// ---- TTFB / processing metrics ----

/** Time-to-first-byte for a single processor (STT/LLM/TTS). */
export interface TTFBMetrics {
  readonly timestamp: number;
  readonly processor: string;
  readonly model?: string | null;
  readonly value: number;
}

/** Total processing time for a single processor stage. */
export interface ProcessingMetrics {
  readonly timestamp: number;
  readonly processor: string;
  readonly model?: string | null;
  readonly value: number;
}

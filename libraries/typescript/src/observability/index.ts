/**
 * Observability entrypoint — re-exports the tracing API.
 *
 * See ``./tracing.ts`` for the implementation.
 */
export {
  initTracing,
  shutdownTracing,
  startSpan,
  withSpan,
  getTracer,
  isTracingEnabled,
  SPAN_CALL,
  SPAN_STT,
  SPAN_LLM,
  SPAN_TTS,
  SPAN_TOOL,
  SPAN_ENDPOINT,
  SPAN_BARGEIN,
  ENV_FLAG,
  SERVICE_NAME,
} from './tracing';
export type { Span, InitTracingOptions } from './tracing';

/**
 * Call lifecycle event — TS mirror of ``getpatter.models.CallEvent``.
 *
 * Kept in the observability namespace because the primary consumers are
 * metrics/tracing sinks (e.g. dashboard ingestion).
 */
export interface CallEvent {
  readonly callId: string;
  readonly caller?: string;
  readonly callee?: string;
  readonly direction?: string;
}

// ---- Event bus ----
export { EventBus } from './event-bus';
export type { PatterEventType } from './event-bus';

// ---- Metric types ----
export type {
  Metadata,
  LLMUsage,
  CachedTokenDetails,
  InputTokenDetails,
  OutputTokenDetails,
  RealtimeUsage,
  EOUMetrics,
  InterruptionMetrics,
  TTFBMetrics,
  ProcessingMetrics,
} from './metric-types';

/**
 * Build the per-call `call_completed` telemetry event.
 *
 * Pure, undefined-guarded, and never throws — called inline on the call-end
 * path, so it must do only O(1) work and never block or throw. Records only
 * coarse, anonymous facts (engine/provider/carrier families, terminal outcome,
 * and the raw latency/duration and total USD cost); no per-call identifier, no PII.
 *
 * `latency_ms` (whole ms) and `duration_seconds` (whole seconds) are sent at full
 * resolution — operational metrics, not the name/cost data that bucketing guards.
 *
 * The CallMetrics object carries snake_case keys (see metrics.ts). Mirrors
 * `libraries/python/getpatter/telemetry/call_metrics.py`.
 */

import type { TelemetryClient } from './client';

type Metricsish = Record<string, unknown>;

function engineFromMode(mode: unknown): string {
  if (mode === 'openai_realtime' || mode === 'openai_realtime_2') return 'realtime';
  if (mode === 'elevenlabs_convai') return 'convai';
  if (mode === 'pipeline') return 'pipeline';
  return 'other';
}

function providerFromMetrics(m: Metricsish): string {
  const mode = m.provider_mode;
  if (mode === 'openai_realtime' || mode === 'openai_realtime_2') return 'openai';
  if (mode === 'elevenlabs_convai') return 'elevenlabs';
  for (const key of ['llm_provider', 'stt_provider', 'tts_provider']) {
    const v = m[key];
    if (typeof v === 'string' && v) return v.toLowerCase();
  }
  return 'other';
}

function providerFromMode(mode: unknown): string {
  // Coarse provider family from the provider mode, for `call_started` (no metrics
  // yet). Pipeline's brain vendor isn't known cheaply at connect, so it collapses
  // to `other` (the value allowlist coerces anything off-list anyway).
  if (mode === 'openai_realtime' || mode === 'openai_realtime_2') return 'openai';
  if (mode === 'elevenlabs_convai') return 'elevenlabs';
  return 'other';
}

function carrierFamily(tp: unknown): string {
  return typeof tp === 'string' && tp ? tp.toLowerCase() : 'none';
}

function direction(value: unknown): string | undefined {
  // Normalise to inbound/outbound; omit if unknown rather than guessing a default
  // that would bias the inbound/outbound split.
  const v = typeof value === 'string' ? value.toLowerCase() : '';
  return v === 'inbound' || v === 'outbound' ? v : undefined;
}

function turnCountBucket(n: number): string {
  if (n <= 0) return '0';
  if (n === 1) return '1';
  if (n <= 3) return '2_3';
  if (n <= 6) return '4_6';
  if (n <= 12) return '7_12';
  return '13_plus';
}

function latencyMs(m: Metricsish): unknown {
  const p95 = m.latency_p95;
  if (p95 && typeof p95 === 'object') {
    return (p95 as Record<string, unknown>).agent_response_ms;
  }
  return undefined;
}

export interface RecordCallStartedOptions {
  readonly providerMode?: string;
  readonly telephonyProvider?: string;
  readonly direction?: unknown;
}

/**
 * Emit a `call_started` event when a call connects (media stream begins). Pairs
 * with `call_completed` for a connect→complete funnel and a failure-rate
 * denominator, and carries the inbound/outbound split. No metrics exist yet at
 * connect, so only coarse engine/provider/carrier/direction are recorded.
 * Swallows everything. Mirrors `record_call_started` in `call_metrics.py`.
 */
export function recordCallStarted(
  telemetry: TelemetryClient | undefined,
  opts: RecordCallStartedOptions,
): void {
  if (!telemetry) return;
  try {
    const dims: Record<string, string> = {
      engine: engineFromMode(opts.providerMode),
      provider: providerFromMode(opts.providerMode),
      carrier: carrierFamily(opts.telephonyProvider),
    };
    const d = direction(opts.direction);
    if (d !== undefined) dims.direction = d;
    telemetry.record('call_started', dims);
  } catch {
    /* swallow — telemetry is never load-bearing */
  }
}

export interface RecordCallCompletedOptions {
  readonly outcome: string;
  readonly metrics?: unknown;
  readonly carrier?: string;
  readonly direction?: unknown;
}

/**
 * Emit a `call_completed` event. Connected calls pass `metrics` +
 * `outcome: "completed"`; non-connected failures pass an `outcome` in
 * {no_answer, busy, failed} and a `carrier` (no metrics). `direction`
 * (inbound/outbound) is recorded when known. Swallows everything.
 */
export function recordCallCompleted(
  telemetry: TelemetryClient | undefined,
  opts: RecordCallCompletedOptions,
): void {
  if (!telemetry) return;
  try {
    const dims: Record<string, string | number> = { outcome: opts.outcome };
    const d = direction(opts.direction);
    if (d !== undefined) dims.direction = d;
    const metrics = opts.metrics;
    if (metrics && typeof metrics === 'object') {
      const m = metrics as Metricsish;
      dims.engine = engineFromMode(m.provider_mode);
      dims.provider = providerFromMetrics(m);
      dims.carrier = carrierFamily(m.telephony_provider);
      if (typeof m.duration_seconds === 'number') {
        dims.duration_seconds = Math.max(0, Math.round(m.duration_seconds));
      }
      const lat = latencyMs(m);
      if (typeof lat === 'number') dims.latency_ms = Math.max(0, Math.round(lat));
      const cost = m.cost;
      if (cost && typeof cost === 'object') {
        const total = (cost as Record<string, unknown>).total;
        if (typeof total === 'number' && Number.isFinite(total)) {
          dims.cost_usd = Math.max(0, Math.round(total * 10000) / 10000);
        }
      }
      if (Array.isArray(m.turns)) {
        dims.turn_count_bucket = turnCountBucket(m.turns.length);
      }
      // A connected call that ended with a terminal error: surface the code and
      // flip the outcome to "error" (the value allowlist coerces unknowns to "other").
      const errorCode = m.error_code;
      if (typeof errorCode === 'string' && errorCode) {
        dims.error_code = errorCode;
        dims.outcome = 'error';
      }
    } else if (opts.carrier !== undefined) {
      dims.carrier = carrierFamily(opts.carrier);
    }
    telemetry.record('call_completed', dims);
  } catch {
    /* swallow — telemetry is never load-bearing */
  }
}

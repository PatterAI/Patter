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
import { installAgeSeconds } from './install-id';

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

/**
 * Coerce a millisecond latency to a whole non-negative int, or `undefined`.
 * `undefined` when the source is absent OR `0` (a stage that did not run — e.g.
 * realtime/convai have no separate STT/TTS span, so those breakdown fields stay
 * `0` and are omitted rather than reported as a false zero).
 */
function wholeMs(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined;
  const ms = Math.max(0, Math.round(value));
  return ms > 0 ? ms : undefined;
}

/**
 * Read the per-stage latency breakdown off the existing p95 accumulator.
 * Read-only — no new audio-path instrumentation. Maps the `LatencyBreakdown`
 * fields onto the wire dims, omitting any stage whose source is absent or `0`
 * (stage did not run). Same source object as `latency_ms` (`m.latency_p95`).
 */
function perStageLatencies(m: Metricsish): Record<string, number> {
  const p95raw = m.latency_p95;
  if (!p95raw || typeof p95raw !== 'object') return {};
  const p95 = p95raw as Record<string, unknown>;
  const mapping: Record<string, unknown> = {
    stt_latency_ms: p95.stt_ms,
    llm_ttft_ms: p95.llm_ttft_ms,
    tts_first_byte_ms: p95.tts_ms,
    eou_latency_ms: p95.endpoint_ms,
  };
  const out: Record<string, number> = {};
  for (const [key, raw] of Object.entries(mapping)) {
    const whole = wholeMs(raw);
    if (whole !== undefined) out[key] = whole;
  }
  return out;
}

// Coarse, deterministic error_code -> error_layer mapping. NEVER the message;
// the layer is inferred only from the structured code. `provider_error` is not
// attributable to a specific stage from the code alone, so it maps to `other`.
// Keep byte-for-byte identical to `_ERROR_LAYER_BY_CODE` in `call_metrics.py`.
const ERROR_LAYER_BY_CODE: Record<string, string> = {
  auth: 'llm',
  rate_limit: 'llm',
  timeout: 'llm',
  provider_error: 'other',
  connection: 'other',
  webhook_verification: 'carrier',
  provision: 'carrier',
  config: 'config',
  input_validation: 'config',
  internal: 'internal',
};

/**
 * Map a terminal `errorCode` to its coarse originating layer. `'none'` on a
 * clean completion (no code); the value allowlist coerces anything off the enum
 * to `'other'`.
 */
function errorLayer(errorCode: string): string {
  if (!errorCode) return 'none';
  return ERROR_LAYER_BY_CODE[errorCode] ?? 'other';
}

/**
 * Map the terminal outcome (+ errorCode) to a coarse disconnect reason. Derived
 * only from already-known state. `hangup_local` / `hangup_remote` are NOT set
 * here because the hanging-up side is not reliably known on this path; clean
 * ends collapse to `completed` and the value allowlist coerces anything off-list
 * to `other`. Keep identical to `_disconnect_reason` in `call_metrics.py`.
 */
function disconnectReason(outcome: string, errorCode: string): string {
  if (outcome === 'no_answer') return 'no_answer';
  if (outcome === 'busy') return 'busy';
  if (outcome === 'error') return errorCode === 'timeout' ? 'timeout' : 'error';
  if (outcome === 'failed') return errorCode === 'timeout' ? 'timeout' : 'error';
  if (outcome === 'completed') return 'completed';
  return 'other';
}

/**
 * Bucket the install age (seconds) into a coarse time-to-first-call band.
 * `unknown` when the age can't be read. Keep boundaries byte-for-byte identical
 * to `_time_to_first_call_bucket` in `call_metrics.py`.
 */
function timeToFirstCallBucket(ageSeconds: number | undefined): string {
  if (ageSeconds === undefined) return 'unknown';
  if (ageSeconds < 3600) return 'lt_1h';
  if (ageSeconds < 86400) return '1h_1d';
  if (ageSeconds < 604800) return '1d_7d';
  return 'gt_7d';
}

export interface RecordCallStartedOptions {
  readonly providerMode?: string;
  readonly telephonyProvider?: string;
  readonly direction?: unknown;
  /** Random per-call correlation id (never the carrier SID), wire key `call_uid`. */
  readonly callUid?: string;
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
    if (opts.callUid) dims.call_uid = opts.callUid;
    // F4: how long after install this call fires (coarse bucket).
    dims.time_to_first_call_bucket = timeToFirstCallBucket(installAgeSeconds());
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
  /** Random per-call correlation id (never the carrier SID), wire key `call_uid`. */
  readonly callUid?: string;
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
    if (opts.callUid) dims.call_uid = opts.callUid;
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
      // F2: per-stage latency, read-only off the same p95 breakdown as
      // latency_ms. Each dim is omitted when its stage didn't run.
      Object.assign(dims, perStageLatencies(m));
      // A connected call that ended with a terminal error: surface the code and
      // flip the outcome to "error" (the value allowlist coerces unknowns to "other").
      const rawCode = m.error_code;
      const errorCode = typeof rawCode === 'string' ? rawCode : '';
      if (errorCode) {
        dims.error_code = errorCode;
        dims.outcome = 'error';
      }
      // F3: coarse error layer + disconnect reason, derived deterministically
      // from the (now-final) outcome and error_code. error_layer is "none" on a
      // clean completion.
      dims.error_layer = errorLayer(errorCode);
      dims.disconnect_reason = disconnectReason(dims.outcome as string, errorCode);
    } else if (opts.carrier !== undefined) {
      dims.carrier = carrierFamily(opts.carrier);
      // F3: non-connected failures (no_answer/busy/failed) carry no metrics, so
      // there is no error_code — map the disconnect reason from the outcome
      // alone; error_layer stays "carrier" only when the carrier itself failed
      // (outcome "failed"), else "none".
      dims.error_layer = opts.outcome === 'failed' ? 'carrier' : 'none';
      dims.disconnect_reason = disconnectReason(opts.outcome, '');
    }
    // F4: how long after install this call fires (coarse bucket) — on both
    // call_started and call_completed for the activation-funnel join.
    dims.time_to_first_call_bucket = timeToFirstCallBucket(installAgeSeconds());
    telemetry.record('call_completed', dims);
  } catch {
    /* swallow — telemetry is never load-bearing */
  }
}

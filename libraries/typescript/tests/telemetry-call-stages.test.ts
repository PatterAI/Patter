/**
 * [unit] Authentic tests for the per-stage / error / time-to-first-call dims.
 *
 * Covers telemetry workstream F2 (per-stage latency), F3 (error_layer +
 * disconnect_reason), and F4 (time_to_first_call_bucket) on the shared
 * `call_started` / `call_completed` emit path.
 *
 * These exercise the REAL builders in `src/telemetry/call-metrics` against
 * REAL CallMetrics-shaped objects (snake_case wire keys, matching `metrics.ts`).
 * Only the outermost boundary — the telemetry client's `record` sink — is a
 * local capture double (no HTTP), so every assertion checks the dimension
 * object the builder actually produced. Nothing here mocks the code under test.
 *
 * Mirror of `libraries/python/tests/test_telemetry_call_stages.py`.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { recordCallCompleted, recordCallStarted } from '../src/telemetry/call-metrics';
import { installAgeSeconds } from '../src/telemetry/install-id';
import type { TelemetryClient } from '../src/telemetry/client';
import type { LatencyBreakdown, CallMetrics } from '../src/metrics';

/**
 * Capture double for the telemetry sink — the only mocked surface. Mirrors
 * `TelemetryClient.record(name, dimensions)` and stores each call so a test can
 * assert the real, fully-built dimension object.
 */
class CaptureTelemetry {
  readonly records: Array<{ name: string; dims: Record<string, unknown> }> = [];
  record(name: string, dimensions?: Record<string, unknown>): void {
    this.records.push({ name, dims: { ...(dimensions ?? {}) } });
  }
}

function asClient(cap: CaptureTelemetry): TelemetryClient {
  // The builders only ever call `.record(...)`; the capture double satisfies
  // that surface. Cast through unknown to avoid pulling the full client type.
  return cap as unknown as TelemetryClient;
}

function latency(partial: Partial<LatencyBreakdown>): LatencyBreakdown {
  return { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0, ...partial };
}

function metricsObj(opts: {
  providerMode: string;
  latency: LatencyBreakdown;
  errorCode?: string;
  llmProvider?: string;
}): Partial<CallMetrics> {
  return {
    call_id: 'ca-test',
    duration_seconds: 12,
    turns: [],
    cost: { stt: 0, tts: 0, llm: 0, telephony: 0, total: 0.06, llm_cached_savings: 0 },
    latency_avg: opts.latency,
    latency_p95: opts.latency,
    provider_mode: opts.providerMode,
    telephony_provider: 'twilio',
    llm_provider: opts.llmProvider ?? '',
    ...(opts.errorCode !== undefined ? { error_code: opts.errorCode } : {}),
  };
}

// --- isolate the persisted-state dir per test (for F4 mtime control) -------
let stateDir: string;
let savedStateDir: string | undefined;

beforeEach(() => {
  savedStateDir = process.env.PATTER_TELEMETRY_STATE_DIR;
  stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-tel-stages-'));
  process.env.PATTER_TELEMETRY_STATE_DIR = stateDir;
});

afterEach(() => {
  if (savedStateDir === undefined) delete process.env.PATTER_TELEMETRY_STATE_DIR;
  else process.env.PATTER_TELEMETRY_STATE_DIR = savedStateDir;
  try {
    fs.rmSync(stateDir, { recursive: true, force: true });
  } catch {
    /* best-effort cleanup */
  }
});

function setInstallMtime(ageSeconds: number): void {
  const p = path.join(stateDir, 'install-id');
  fs.writeFileSync(p, '0'.repeat(32), 'utf8');
  const when = new Date(Date.now() - ageSeconds * 1000);
  fs.utimesSync(p, when, when);
}

// --------------------------------------------------------------------------- //
// F2 — per-stage latency                                                      //
// --------------------------------------------------------------------------- //

describe('[unit] call_completed per-stage latency (F2)', () => {
  it('emits the four per-stage dims as whole ints for a pipeline call', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), {
      outcome: 'completed',
      metrics: metricsObj({
        providerMode: 'pipeline',
        llmProvider: 'cerebras',
        latency: latency({
          stt_ms: 120.4,
          tts_ms: 88.9,
          llm_ttft_ms: 305.5,
          endpoint_ms: 42.2,
          agent_response_ms: 436,
        }),
      }),
    });

    expect(cap.records).toHaveLength(1);
    const { name, dims } = cap.records[0];
    expect(name).toBe('call_completed');
    expect(dims.stt_latency_ms).toBe(120);
    expect(dims.tts_first_byte_ms).toBe(89);
    expect(dims.llm_ttft_ms).toBe(306);
    expect(dims.eou_latency_ms).toBe(42);
    for (const k of ['stt_latency_ms', 'tts_first_byte_ms', 'llm_ttft_ms', 'eou_latency_ms']) {
      expect(Number.isInteger(dims[k])).toBe(true);
    }
  });

  it('omits stt/tts stage dims for a realtime engine (no separate STT/TTS span)', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), {
      outcome: 'completed',
      metrics: metricsObj({
        providerMode: 'openai_realtime',
        latency: latency({ stt_ms: 0, tts_ms: 0, llm_ttft_ms: 210, endpoint_ms: 30 }),
      }),
    });

    const { dims } = cap.records[0];
    expect(dims.stt_latency_ms).toBeUndefined();
    expect(dims.tts_first_byte_ms).toBeUndefined();
    expect(dims.llm_ttft_ms).toBe(210);
    expect(dims.eou_latency_ms).toBe(30);
  });

  it('omits an absent optional stage (undefined ttft/endpoint), never sends 0', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), {
      outcome: 'completed',
      metrics: metricsObj({
        providerMode: 'pipeline',
        latency: latency({ stt_ms: 100, tts_ms: 50 }),
      }),
    });

    const { dims } = cap.records[0];
    expect(dims.stt_latency_ms).toBe(100);
    expect(dims.tts_first_byte_ms).toBe(50);
    expect(dims.llm_ttft_ms).toBeUndefined();
    expect(dims.eou_latency_ms).toBeUndefined();
  });
});

// --------------------------------------------------------------------------- //
// F3 — error_layer + disconnect_reason                                        //
// --------------------------------------------------------------------------- //

describe('[unit] call_completed error_layer + disconnect_reason (F3)', () => {
  it('maps a clean completion to error_layer=none, disconnect_reason=completed', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), {
      outcome: 'completed',
      metrics: metricsObj({ providerMode: 'pipeline', latency: latency({}) }),
    });
    const { dims } = cap.records[0];
    expect(dims.error_layer).toBe('none');
    expect(dims.disconnect_reason).toBe('completed');
    expect(dims.outcome).toBe('completed');
  });

  it('maps an llm auth error to error_layer=llm, disconnect_reason=error', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), {
      outcome: 'completed',
      metrics: metricsObj({ providerMode: 'pipeline', latency: latency({}), errorCode: 'auth' }),
    });
    const { dims } = cap.records[0];
    expect(dims.outcome).toBe('error');
    expect(dims.error_code).toBe('auth');
    expect(dims.error_layer).toBe('llm');
    expect(dims.disconnect_reason).toBe('error');
  });

  it('maps a timeout error to disconnect_reason=timeout', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), {
      outcome: 'completed',
      metrics: metricsObj({ providerMode: 'pipeline', latency: latency({}), errorCode: 'timeout' }),
    });
    const { dims } = cap.records[0];
    expect(dims.error_layer).toBe('llm');
    expect(dims.disconnect_reason).toBe('timeout');
  });

  it('maps webhook_verification to error_layer=carrier', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), {
      outcome: 'completed',
      metrics: metricsObj({
        providerMode: 'pipeline',
        latency: latency({}),
        errorCode: 'webhook_verification',
      }),
    });
    expect(cap.records[0].dims.error_layer).toBe('carrier');
  });

  it('maps provider_error to error_layer=other (not attributable to a stage)', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), {
      outcome: 'completed',
      metrics: metricsObj({
        providerMode: 'pipeline',
        latency: latency({}),
        errorCode: 'provider_error',
      }),
    });
    const { dims } = cap.records[0];
    expect(dims.error_layer).toBe('other');
    expect(dims.disconnect_reason).toBe('error');
  });

  it('maps a no_answer failure to disconnect_reason=no_answer, layer=none', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), { outcome: 'no_answer', carrier: 'twilio' });
    const { dims } = cap.records[0];
    expect(dims.outcome).toBe('no_answer');
    expect(dims.disconnect_reason).toBe('no_answer');
    expect(dims.error_layer).toBe('none');
  });

  it('maps a busy failure to disconnect_reason=busy', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), { outcome: 'busy', carrier: 'telnyx' });
    const { dims } = cap.records[0];
    expect(dims.disconnect_reason).toBe('busy');
    expect(dims.error_layer).toBe('none');
  });

  it('maps a carrier failure (outcome=failed) to disconnect_reason=error, layer=carrier', () => {
    const cap = new CaptureTelemetry();
    recordCallCompleted(asClient(cap), { outcome: 'failed', carrier: 'twilio' });
    const { dims } = cap.records[0];
    expect(dims.disconnect_reason).toBe('error');
    expect(dims.error_layer).toBe('carrier');
  });
});

// --------------------------------------------------------------------------- //
// F4 — time_to_first_call_bucket (both events)                                //
// --------------------------------------------------------------------------- //

describe('[unit] time_to_first_call_bucket (F4)', () => {
  it('attaches lt_1h on both call_started and call_completed for a 30-min install', () => {
    setInstallMtime(30 * 60);

    const started = new CaptureTelemetry();
    recordCallStarted(asClient(started), { providerMode: 'pipeline', telephonyProvider: 'twilio' });

    const completed = new CaptureTelemetry();
    recordCallCompleted(asClient(completed), {
      outcome: 'completed',
      metrics: metricsObj({ providerMode: 'pipeline', latency: latency({}) }),
    });

    expect(started.records[0].dims.time_to_first_call_bucket).toBe('lt_1h');
    expect(completed.records[0].dims.time_to_first_call_bucket).toBe('lt_1h');
  });

  it('buckets at every boundary', () => {
    const cases: Array<[number, string]> = [
      [60, 'lt_1h'],
      [3600 + 1, '1h_1d'],
      [86400 + 1, '1d_7d'],
      [604800 + 1, 'gt_7d'],
    ];
    for (const [age, expected] of cases) {
      setInstallMtime(age);
      const cap = new CaptureTelemetry();
      recordCallStarted(asClient(cap), { providerMode: 'pipeline' });
      expect(cap.records[0].dims.time_to_first_call_bucket).toBe(expected);
    }
  });

  it('falls back to unknown when the install state is unreadable', () => {
    // No install-id file written → statSync throws → bucket is unknown.
    const cap = new CaptureTelemetry();
    recordCallStarted(asClient(cap), { providerMode: 'pipeline' });
    expect(cap.records[0].dims.time_to_first_call_bucket).toBe('unknown');
  });

  it('installAgeSeconds reads the mtime', () => {
    setInstallMtime(7200);
    const age = installAgeSeconds();
    expect(age).not.toBeUndefined();
    expect(age!).toBeGreaterThanOrEqual(7000);
    expect(age!).toBeLessThanOrEqual(7400);
  });
});

describe('[unit] call builders are fire-and-forget', () => {
  it('never throw with an undefined telemetry sink', () => {
    expect(() => recordCallStarted(undefined, { providerMode: 'pipeline' })).not.toThrow();
    expect(() => recordCallCompleted(undefined, { outcome: 'completed' })).not.toThrow();
  });
});

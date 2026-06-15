/**
 * [unit] Authentic tests for the client-side telemetry sampling gate (F4).
 *
 * A real local HTTP collector (node:http) captures what the SDK actually sends
 * over the real global `fetch`. The sampling decision, the per-run SHA-256 hash
 * of `runId`, the `sample_rate` stamping, the payload builder, and the network
 * egress are all REAL — only the CI/test environment detection is neutralised
 * (so the enabled path runs inside vitest) and the network boundary is local.
 * `runId` is the only thing mocked to a fixed value, because the keep/drop
 * decision is deterministic per run and the test must exercise BOTH branches.
 *
 * Mirrors `libraries/python/tests/test_telemetry_sampling.py`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createServer, type Server } from 'node:http';
import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

// Pin the per-run sampling seed. The gate hashes `runId()` (imported by the
// client from this module); overriding it lets one test KEEP and another DROP
// within the same process. Everything else in the module stays real.
let mockRunId = 'run00000000000000000000000000000000';
vi.mock('../src/telemetry/install-id', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/telemetry/install-id')>();
  return { ...actual, runId: () => mockRunId };
});

import { TelemetryClient } from '../src/telemetry/client';
import { sampleRate } from '../src/telemetry/env';

// Keep the persisted install id out of the developer's home during tests.
process.env.PATTER_TELEMETRY_STATE_DIR =
  process.env.PATTER_TELEMETRY_STATE_DIR ?? fs.mkdtempSync(path.join(os.tmpdir(), 'patter-tel-smpl-'));

// Two fixed run ids whose SHA-256-derived ratio straddles 0.5, so at rate=0.5
// one run KEEPS its sampleable call events and the other DROPS them. The
// formula is asserted below, not trusted blind.
const KEEP_RUN_ID = 'run00000000000000000000000000000000'; // ratio ~0.031 -> keep @0.5
const DROP_RUN_ID = 'run00000000000000000000000000000003'; // ratio ~0.742 -> drop @0.5

function ratioFor(runId: string): number {
  const d = createHash('sha256').update(runId, 'utf8').digest('hex').slice(0, 8);
  return parseInt(d, 16) / 0xffffffff;
}

const CI_KEYS = [
  'CI',
  'CONTINUOUS_INTEGRATION',
  'GITHUB_ACTIONS',
  'GITLAB_CI',
  'TRAVIS',
  'CIRCLECI',
  'APPVEYOR',
  'TF_BUILD',
  'TEAMCITY_VERSION',
  'BUILDKITE',
  'DRONE',
  'JENKINS_URL',
  'HUDSON_URL',
  'BAMBOO_BUILDKEY',
  'CODEBUILD_BUILD_ID',
];
const TEST_KEYS = ['VITEST', 'JEST_WORKER_ID'];
const DISABLE_KEYS = [
  'DO_NOT_TRACK',
  'PATTER_TELEMETRY_DISABLED',
  'PATTER_TELEMETRY_DEBUG',
  'PATTER_TELEMETRY_ENDPOINT',
  'PATTER_TELEMETRY_SAMPLE',
];
const ALL_KEYS = [...CI_KEYS, ...TEST_KEYS, ...DISABLE_KEYS, 'NODE_ENV'];

let savedEnv: Record<string, string | undefined> = {};

function snapshotEnv(): void {
  savedEnv = {};
  for (const k of ALL_KEYS) savedEnv[k] = process.env[k];
}
function restoreEnv(): void {
  for (const k of ALL_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
}
/** Clear every detection/disable signal so telemetry resolves to enabled. */
function enableTelemetryEnv(): void {
  for (const k of [...CI_KEYS, ...TEST_KEYS, ...DISABLE_KEYS]) delete process.env[k];
  process.env.NODE_ENV = 'development';
}

class Collector {
  requests: unknown[] = [];
  private server!: Server;

  async start(): Promise<void> {
    this.server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on('data', (c: Buffer) => chunks.push(c));
      req.on('end', () => {
        try {
          this.requests.push(JSON.parse(Buffer.concat(chunks).toString()));
        } catch {
          this.requests.push(null);
        }
        res.statusCode = 204;
        res.end();
      });
    });
    await new Promise<void>((resolve) => this.server.listen(0, '127.0.0.1', () => resolve()));
  }

  get url(): string {
    const addr = this.server.address();
    const port = typeof addr === 'object' && addr ? addr.port : 0;
    return `http://127.0.0.1:${port}/v1/ingest`;
  }

  get events(): Array<Record<string, unknown>> {
    const out: Array<Record<string, unknown>> = [];
    for (const batch of this.requests) {
      if (Array.isArray(batch)) out.push(...(batch as Array<Record<string, unknown>>));
    }
    return out;
  }

  names(): string[] {
    return this.events.map((e) => e.event as string);
  }

  async stop(): Promise<void> {
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }
}

async function waitFor(collector: Collector, n: number, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (collector.events.length < n && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 10));
  }
}

let collector: Collector;

beforeEach(async () => {
  snapshotEnv();
  collector = new Collector();
  await collector.start();
  mockRunId = KEEP_RUN_ID;
});

afterEach(async () => {
  await collector.stop();
  restoreEnv();
});

describe('[unit] sampling seed fixtures', () => {
  it('the two pinned run ids really straddle 0.5', () => {
    expect(ratioFor(KEEP_RUN_ID)).toBeLessThan(0.5);
    expect(ratioFor(DROP_RUN_ID)).toBeGreaterThanOrEqual(0.5);
  });
});

describe('[unit] sampleRate() parsing + clamping + fail-safe', () => {
  it('defaults to 1.0 when unset', () => {
    delete process.env.PATTER_TELEMETRY_SAMPLE;
    expect(sampleRate()).toBe(1.0);
  });

  it('parses valid values', () => {
    const cases: Array<[string, number]> = [
      ['0', 0],
      ['0.0', 0],
      ['0.5', 0.5],
      ['1', 1],
      ['1.0', 1],
      ['  0.25  ', 0.25],
    ];
    for (const [raw, expected] of cases) {
      process.env.PATTER_TELEMETRY_SAMPLE = raw;
      expect(sampleRate()).toBe(expected);
    }
  });

  it('degrades malformed / out-of-range values to 1.0 (fail safe)', () => {
    for (const raw of ['', '   ', 'abc', '0.5x', '-0.1', '-1', '2', '1.5', '10', 'NaN', 'Infinity', '-Infinity']) {
      process.env.PATTER_TELEMETRY_SAMPLE = raw;
      expect(sampleRate()).toBe(1.0);
    }
  });

  it('never throws', () => {
    for (const raw of ['', 'garbage', 'NaN', '1e9999', '+-+']) {
      process.env.PATTER_TELEMETRY_SAMPLE = raw;
      expect(() => sampleRate()).not.toThrow();
    }
  });
});

describe('[unit] sampling gate — rate = 0', () => {
  it('drops call events but keeps activation and error events', async () => {
    enableTelemetryEnv();
    process.env.PATTER_TELEMETRY_SAMPLE = '0';
    mockRunId = KEEP_RUN_ID; // irrelevant at rate 0 (always drops sampleable)
    const client = new TelemetryClient({ sdkVersion: '0.6.8', endpoint: collector.url });

    client.record('call_started', { engine: 'realtime', carrier: 'twilio' }); // dropped
    client.record('call_completed', { outcome: 'completed', carrier: 'twilio' }); // dropped

    // NEVER-sampled events: always delivered, even at rate 0.
    client.record('first_run');
    client.record('config_incomplete', { missing: 'carrier_credentials' });
    client.record('sdk_initialized', { engine: 'realtime' });

    // An ERROR call_completed is force-kept regardless of rate.
    client.record('call_completed', {
      outcome: 'error',
      error_code: 'provider_error',
      carrier: 'twilio',
    });

    await waitFor(collector, 4);
    await client.close();

    expect(collector.names().sort()).toEqual([
      'call_completed', // the error one
      'config_incomplete',
      'first_run',
      'sdk_initialized',
    ]);
    const err = collector.events.find((e) => e.event === 'call_completed');
    expect(err?.outcome).toBe('error');
    expect(err?.error_code).toBe('provider_error');
    expect(err?.sample_rate).toBe(0);
  });

  it('keeps a call_completed flagged only by error_code (no outcome=error)', async () => {
    enableTelemetryEnv();
    process.env.PATTER_TELEMETRY_SAMPLE = '0';
    mockRunId = KEEP_RUN_ID;
    const client = new TelemetryClient({ sdkVersion: '0.6.8', endpoint: collector.url });
    client.record('call_completed', { outcome: 'completed', error_code: 'timeout' });
    client.record('call_started', { engine: 'realtime' }); // dropped
    await waitFor(collector, 1);
    await client.close();

    expect(collector.names()).toEqual(['call_completed']);
    expect(collector.events[0].error_code).toBe('timeout');
  });
});

describe('[unit] sampling gate — rate = 0.5 determinism + stamping', () => {
  it('a KEEP run delivers call events stamped with sample_rate', async () => {
    enableTelemetryEnv();
    process.env.PATTER_TELEMETRY_SAMPLE = '0.5';
    mockRunId = KEEP_RUN_ID; // ratio < 0.5 -> KEEP
    const client = new TelemetryClient({ sdkVersion: '0.6.8', endpoint: collector.url });

    client.record('call_started', { engine: 'realtime', carrier: 'twilio' });
    client.record('call_completed', { outcome: 'completed', carrier: 'twilio' });
    await waitFor(collector, 2);
    await client.close();

    expect(collector.names().sort()).toEqual(['call_completed', 'call_started']);
    for (const e of collector.events) expect(e.sample_rate).toBe(0.5);
  });

  it('a DROP run drops clean call events but keeps first_run and error calls', async () => {
    enableTelemetryEnv();
    process.env.PATTER_TELEMETRY_SAMPLE = '0.5';
    mockRunId = DROP_RUN_ID; // ratio >= 0.5 -> DROP
    const client = new TelemetryClient({ sdkVersion: '0.6.8', endpoint: collector.url });

    client.record('call_started', { engine: 'realtime' }); // dropped
    client.record('call_completed', { outcome: 'completed' }); // dropped
    client.record('first_run'); // kept
    client.record('call_completed', { outcome: 'error', error_code: 'auth' }); // kept
    await waitFor(collector, 2);
    await client.close();

    expect(collector.names().sort()).toEqual(['call_completed', 'first_run']);
    const err = collector.events.find((e) => e.event === 'call_completed');
    expect(err?.outcome).toBe('error');
    expect(err?.sample_rate).toBe(0.5); // force-kept error still carries the rate
  });

  it('the keep/drop decision is stable across repeated record calls (no per-event coin)', async () => {
    enableTelemetryEnv();
    process.env.PATTER_TELEMETRY_SAMPLE = '0.5';
    mockRunId = DROP_RUN_ID; // this run drops sampleable events
    const client = new TelemetryClient({ sdkVersion: '0.6.8', endpoint: collector.url });

    for (let i = 0; i < 20; i++) client.record('call_started', { engine: 'realtime' });
    await new Promise((r) => setTimeout(r, 100));
    await client.close();

    expect(collector.events).toHaveLength(0); // all 20 dropped, consistently

    // A fresh client in the same run (same pinned runId) makes the SAME call.
    const client2 = new TelemetryClient({ sdkVersion: '0.6.8', endpoint: collector.url });
    client2.record('call_started', { engine: 'realtime' });
    await new Promise((r) => setTimeout(r, 100));
    await client2.close();
    expect(collector.events).toHaveLength(0);
  });
});

describe('[unit] sampling gate — no sampling (unset / >= 1 / malformed)', () => {
  it('unset keeps everything and does not stamp sample_rate', async () => {
    enableTelemetryEnv();
    delete process.env.PATTER_TELEMETRY_SAMPLE;
    mockRunId = DROP_RUN_ID; // would drop IF sampling were active
    const client = new TelemetryClient({ sdkVersion: '0.6.8', endpoint: collector.url });

    client.record('call_started', { engine: 'realtime' });
    client.record('call_completed', { outcome: 'completed' });
    await waitFor(collector, 2);
    await client.close();

    expect(collector.names().sort()).toEqual(['call_completed', 'call_started']);
    for (const e of collector.events) expect(e.sample_rate).toBeUndefined();
  });

  it('a malformed value degrades to keep-all (no drops, no stamping)', async () => {
    enableTelemetryEnv();
    process.env.PATTER_TELEMETRY_SAMPLE = 'garbage';
    mockRunId = DROP_RUN_ID;
    const client = new TelemetryClient({ sdkVersion: '0.6.8', endpoint: collector.url });

    client.record('call_started', { engine: 'realtime' });
    client.record('call_completed', { outcome: 'completed' });
    await waitFor(collector, 2);
    await client.close();

    expect(collector.names().sort()).toEqual(['call_completed', 'call_started']);
    for (const e of collector.events) expect(e.sample_rate).toBeUndefined();
  });
});

/**
 * [integration] Authentic tests for the `config_incomplete` activation-blocker
 * signal (F1).
 *
 * The real `Patter` client runs its real credential / engine validation against a
 * real `TelemetryClient`. The only substituted surface is the outbound HTTP
 * boundary: a real local `node:http` collector captures what the SDK actually
 * POSTs over the global `fetch` (the same authentic pattern as `telemetry.test.ts`).
 * Everything from `new Patter(...)` / `patter.agent(...)` inward — the
 * `recordConfigIncomplete` dedupe, the validation, and the `throw` — is real.
 *
 * Each test asserts that the blocking validation BOTH emits exactly one
 * `config_incomplete` event with the correct coarse `missing` enum AND still
 * throws the original error unchanged.
 *
 * Mirror of `libraries/python/tests/test_telemetry_config_incomplete.py`.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createServer, type Server } from 'node:http';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

// Keep the persisted install id out of the developer's home during tests.
process.env.PATTER_TELEMETRY_STATE_DIR =
  process.env.PATTER_TELEMETRY_STATE_DIR ?? fs.mkdtempSync(path.join(os.tmpdir(), 'patter-cfg-'));

import { Patter, Twilio, DeepgramSTT } from '../src/index';

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
];
const ALL_KEYS = [...CI_KEYS, ...TEST_KEYS, ...DISABLE_KEYS, 'NODE_ENV', 'OPENAI_API_KEY'];

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

  configIncomplete(): Array<Record<string, unknown>> {
    return this.events.filter((e) => e.event === 'config_incomplete');
  }

  async stop(): Promise<void> {
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }
}

/** Wait until at least one `config_incomplete` event has reached the collector. */
async function waitForConfigIncomplete(collector: Collector, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (collector.configIncomplete().length < 1 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 10));
  }
}

/** Give the buffered fire-and-forget flush a few ticks to attempt an egress. */
async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 10));
}

const VALID_CARRIER = { accountSid: 'AC_test', authToken: 'tok_test' };

describe('[integration] config_incomplete activation-blocker signal', () => {
  let collector: Collector;

  beforeEach(async () => {
    snapshotEnv();
    enableTelemetryEnv();
    collector = new Collector();
    await collector.start();
    process.env.PATTER_TELEMETRY_ENDPOINT = collector.url;
    delete process.env.OPENAI_API_KEY;
  });

  afterEach(async () => {
    await collector.stop();
    restoreEnv();
  });

  // --- (1) missing carrier credentials (constructor) ------------------------

  it('emits carrier_credentials and still throws when phoneNumber is missing', async () => {
    expect(
      () => new Patter({ carrier: new Twilio(VALID_CARRIER) } as never),
    ).toThrow(/Local mode requires phoneNumber/);

    await waitForConfigIncomplete(collector);
    const events = collector.configIncomplete();
    expect(events.length).toBe(1);
    expect(events[0].missing).toBe('carrier_credentials');
  });

  it('emits carrier_credentials and still throws when carrier is missing', async () => {
    expect(
      () => new Patter({ phoneNumber: '+15550000000' } as never),
    ).toThrow(/Local mode requires a `carrier` instance/);

    await waitForConfigIncomplete(collector);
    const events = collector.configIncomplete();
    expect(events.length).toBe(1);
    expect(events[0].missing).toBe('carrier_credentials');
  });

  // --- (2) missing LLM / engine key (agent) ---------------------------------

  it('emits llm_key and still throws when the OpenAI Realtime key is missing', async () => {
    const phone = new Patter({ carrier: new Twilio(VALID_CARRIER), phoneNumber: '+15550000000' });

    // The OpenAI-key check fires for the explicit openai_realtime provider with no
    // engine marker and no key in env/config (parity with Python's realtime path).
    expect(() =>
      phone.agent({ systemPrompt: 'You are a helpful assistant.', provider: 'openai_realtime' }),
    ).toThrow(/OpenAI Realtime mode requires an OpenAI API key/);

    await waitForConfigIncomplete(collector);
    const events = collector.configIncomplete();
    expect(events.length).toBe(1);
    expect(events[0].missing).toBe('llm_key');
  });

  // --- (3) incomplete engine config (agent) ---------------------------------

  it('emits engine_config and still throws on an invalid provider', async () => {
    const phone = new Patter({ carrier: new Twilio(VALID_CARRIER), phoneNumber: '+15550000000' });

    expect(() =>
      phone.agent({ systemPrompt: 'prompt', provider: 'bogus' as never }),
    ).toThrow(/provider must be one of/);

    await waitForConfigIncomplete(collector);
    const events = collector.configIncomplete();
    expect(events.length).toBe(1);
    expect(events[0].missing).toBe('engine_config');
  });

  it('emits engine_config and still throws on an unknown engine instance', async () => {
    const phone = new Patter({ carrier: new Twilio(VALID_CARRIER), phoneNumber: '+15550000000' });

    // A bare object that is not an OpenAIRealtime/OpenAIRealtime2/ElevenLabsConvAI
    // instance hits the unknown-engine branch.
    expect(() =>
      phone.agent({ systemPrompt: 'prompt', engine: {} as never }),
    ).toThrow(/Unknown engine/);

    await waitForConfigIncomplete(collector);
    const events = collector.configIncomplete();
    expect(events.length).toBe(1);
    expect(events[0].missing).toBe('engine_config');
  });

  // --- emitted at most once per instance ------------------------------------

  it('emits config_incomplete at most once per Patter instance', async () => {
    const phone = new Patter({ carrier: new Twilio(VALID_CARRIER), phoneNumber: '+15550000000' });

    // First failing agent() emits once; subsequent failures on the SAME instance
    // (a retry, a second agent() call) must NOT double-emit.
    expect(() =>
      phone.agent({ systemPrompt: 'one', provider: 'openai_realtime' }),
    ).toThrow();
    expect(() =>
      phone.agent({ systemPrompt: 'two', provider: 'openai_realtime' }),
    ).toThrow();
    expect(() => phone.agent({ systemPrompt: 'three', provider: 'bogus' as never })).toThrow();

    await settle();
    const events = collector.configIncomplete();
    expect(events.length).toBe(1);
    expect(events[0].missing).toBe('llm_key');
  });

  // --- a fully-valid agent never emits the activation-blocker signal ---------

  it('does not emit config_incomplete for a valid Realtime agent', async () => {
    process.env.OPENAI_API_KEY = 'sk-test-not-a-real-key';
    const phone = new Patter({ carrier: new Twilio(VALID_CARRIER), phoneNumber: '+15550000000' });
    phone.agent({ systemPrompt: 'You are a helpful assistant.' });

    await settle();
    expect(collector.configIncomplete().length).toBe(0);
  });

  it('does not emit config_incomplete for a valid pipeline agent with an STT', async () => {
    const phone = new Patter({ carrier: new Twilio(VALID_CARRIER), phoneNumber: '+15550000000' });
    phone.agent({
      systemPrompt: 'You are a helpful assistant.',
      provider: 'pipeline',
      stt: new DeepgramSTT({ apiKey: 'dg-test' }),
    });

    await settle();
    expect(collector.configIncomplete().length).toBe(0);
  });
});

// --- opt-out emits nothing ---------------------------------------------------

describe('[integration] config_incomplete respects opt-out', () => {
  let collector: Collector;

  beforeEach(async () => {
    snapshotEnv();
    enableTelemetryEnv();
    collector = new Collector();
    await collector.start();
    process.env.PATTER_TELEMETRY_ENDPOINT = collector.url;
    delete process.env.OPENAI_API_KEY;
  });

  afterEach(async () => {
    await collector.stop();
    restoreEnv();
  });

  it('emits nothing when telemetry: false', async () => {
    expect(
      () => new Patter({ carrier: new Twilio(VALID_CARRIER), telemetry: false } as never),
    ).toThrow(/Local mode requires phoneNumber/);

    await settle();
    expect(collector.configIncomplete().length).toBe(0);
  });

  it('emits nothing when PATTER_TELEMETRY_DISABLED=1', async () => {
    process.env.PATTER_TELEMETRY_DISABLED = '1';

    expect(
      () => new Patter({ carrier: new Twilio(VALID_CARRIER) } as never),
    ).toThrow(/Local mode requires phoneNumber/);

    await settle();
    expect(collector.configIncomplete().length).toBe(0);
  });
});

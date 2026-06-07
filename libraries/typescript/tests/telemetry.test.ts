/**
 * [integration] Authentic tests for the anonymous telemetry client.
 *
 * A real local HTTP collector (node:http) captures what the SDK actually sends
 * over the real global `fetch`. Only the CI/test environment detection is
 * neutralised (so the enabled path can run inside vitest/CI); the consent logic,
 * buffer, payload builder, redaction, and network egress are all real.
 *
 * Mirror of `libraries/python/tests/test_telemetry.py`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createServer, type Server } from 'node:http';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

// Keep the persisted install id out of the developer's home during tests.
process.env.PATTER_TELEMETRY_STATE_DIR =
  process.env.PATTER_TELEMETRY_STATE_DIR ?? fs.mkdtempSync(path.join(os.tmpdir(), 'patter-tel-'));

import { TelemetryClient, buildEvent, recordCallCompleted } from '../src/telemetry';
import { stackDimensions, modelToken, vendorOf } from '../src/telemetry/stack';
import { DIMENSION_VALUES } from '../src/telemetry/events';
import {
  invokedByAgent,
  inContainer,
  serverless,
  cloud,
  packageManager,
} from '../src/telemetry/environment';
import { EmbeddedServer } from '../src/server';
import { CallMetricsAccumulator } from '../src/metrics';
import { PatterTool } from '../src/integrations/patter-tool';
import type { LocalConfig } from '../src/types';
import type { Patter } from '../src/client';

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
    await new Promise<void>((resolve) =>
      this.server.listen(0, '127.0.0.1', () => resolve()),
    );
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
});

afterEach(async () => {
  await collector.stop();
  restoreEnv();
});

describe('[integration] telemetry — enabled path', () => {
  it('an event reaches the collector when enabled', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    expect(client.enabled).toBe(true);
    client.record('feature_used', { engine: 'realtime', provider: 'openai', carrier: 'twilio' });
    await waitFor(collector, 1);
    await client.close();

    expect(collector.events).toHaveLength(1);
    const event = collector.events[0];
    expect(event.event).toBe('feature_used');
    expect(event.sdk).toBe('typescript');
    expect(event.sdk_version).toBe('0.6.3');
    expect(event.runtime).toBe('node');
    expect(event.schema_version).toBe(4);
    expect(event.engine).toBe('realtime');
    expect(event.carrier).toBe('twilio');
    expect(typeof event.run_id).toBe('string');
  });

  it('drops denylisted dimensions', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    client.record('feature_used', {
      engine: 'pipeline',
      phone_number: '+15551234567',
      transcript: 'secret words',
      api_key: 'sk-secret',
    } as Record<string, string>);
    await waitFor(collector, 1);
    await client.close();

    const event = collector.events[0];
    expect(event.engine).toBe('pipeline');
    for (const forbidden of ['phone_number', 'transcript', 'api_key']) {
      expect(event[forbidden]).toBeUndefined();
    }
    const blob = JSON.stringify(event);
    expect(blob).not.toContain('+1555');
    expect(blob).not.toContain('sk-secret');
  });
});

describe('[integration] telemetry — disabled paths produce zero egress', () => {
  it('disabled by DO_NOT_TRACK', async () => {
    enableTelemetryEnv();
    process.env.DO_NOT_TRACK = '1';
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    expect(client.enabled).toBe(false);
    client.record('feature_used', { engine: 'realtime' });
    await new Promise((r) => setTimeout(r, 100));
    await client.close();
    expect(collector.events).toHaveLength(0);
  });

  it('disabled by the kill switch', async () => {
    enableTelemetryEnv();
    process.env.PATTER_TELEMETRY_DISABLED = '1';
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    expect(client.enabled).toBe(false);
    client.record('feature_used', { engine: 'realtime' });
    await new Promise((r) => setTimeout(r, 100));
    await client.close();
    expect(collector.events).toHaveLength(0);
  });

  it('disabled by the constructor flag', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({
      sdkVersion: '0.6.3',
      flag: false,
      endpoint: collector.url,
    });
    expect(client.enabled).toBe(false);
    client.record('feature_used', { engine: 'realtime' });
    await new Promise((r) => setTimeout(r, 100));
    await client.close();
    expect(collector.events).toHaveLength(0);
  });

  it('disabled in CI', async () => {
    enableTelemetryEnv();
    process.env.CI = 'true';
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    expect(client.enabled).toBe(false);
    client.record('feature_used');
    await new Promise((r) => setTimeout(r, 100));
    await client.close();
    expect(collector.events).toHaveLength(0);
  });
});

describe('[integration] telemetry — fail-safety', () => {
  it('is silent when the collector is offline', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({
      sdkVersion: '0.6.3',
      endpoint: 'http://127.0.0.1:1/v1/ingest',
    });
    expect(client.enabled).toBe(true);
    client.record('feature_used', { engine: 'realtime' });
    await new Promise((r) => setTimeout(r, 100));
    await expect(client.close()).resolves.toBeUndefined();
  });

  it('never throws on an unknown event name', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    expect(() => client.record('not_a_real_event')).not.toThrow();
    await new Promise((r) => setTimeout(r, 100));
    await client.close();
    expect(collector.events).toHaveLength(0);
  });

  it('debug mode prints without sending', async () => {
    enableTelemetryEnv();
    process.env.PATTER_TELEMETRY_DEBUG = '1';
    const writes: string[] = [];
    const spy = vi
      .spyOn(process.stderr, 'write')
      .mockImplementation((chunk: unknown) => {
        writes.push(String(chunk));
        return true;
      });
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    client.record('sdk_initialized', { engine: 'convai' });
    await new Promise((r) => setTimeout(r, 100));
    await client.close();
    spy.mockRestore();

    expect(collector.events).toHaveLength(0);
    const printed = writes.join('');
    expect(printed).toContain('[patter telemetry]');
    expect(printed).toContain('sdk_initialized');
  });
});

describe('[integration] telemetry — call_completed', () => {
  it('emits call_completed from a metrics object (with cost)', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    const metrics = {
      provider_mode: 'pipeline',
      llm_provider: 'openai',
      telephony_provider: 'twilio',
      duration_seconds: 42, // -> 10s_1m
      latency_p95: { agent_response_ms: 2500 }, // -> 2s_5s
      cost: { total: 0.12 }, // forwarded as cost_usd (float, not rounded)
    };
    recordCallCompleted(client, { outcome: 'completed', metrics });
    await waitFor(collector, 1);
    await client.close();

    const e = collector.events[0];
    expect(e.event).toBe('call_completed');
    expect(e.outcome).toBe('completed');
    expect(e.engine).toBe('pipeline');
    expect(e.provider).toBe('openai');
    expect(e.carrier).toBe('twilio');
    // Raw values now (not buckets): whole seconds / whole milliseconds.
    expect(e.duration_seconds).toBe(42);
    expect(e.latency_ms).toBe(2500);
    expect(e.cost_usd).toBe(0.12); // total USD cost, preserved as a float
  });

  it('emits a failed outcome with only outcome + carrier', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    recordCallCompleted(client, { outcome: 'no_answer', carrier: 'twilio' });
    await waitFor(collector, 1);
    await client.close();

    const e = collector.events[0];
    expect(e.event).toBe('call_completed');
    expect(e.outcome).toBe('no_answer');
    expect(e.carrier).toBe('twilio');
    expect(e.latency_ms).toBeUndefined();
    expect(e.duration_seconds).toBeUndefined();
  });

  it('never throws on missing telemetry / metrics', () => {
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    expect(() => recordCallCompleted(undefined, { outcome: 'completed' })).not.toThrow();
    expect(() => recordCallCompleted(client, { outcome: 'completed', metrics: null })).not.toThrow();
  });

  it('an error_code flips the outcome to "error"', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    recordCallCompleted(client, {
      outcome: 'completed',
      metrics: {
        provider_mode: 'pipeline',
        llm_provider: 'openai',
        telephony_provider: 'twilio',
        duration_seconds: 12,
        latency_p95: { agent_response_ms: 900 },
        error_code: 'rate_limit',
      },
    });
    await waitFor(collector, 1);
    await client.close();

    const e = collector.events[0];
    expect(e.outcome).toBe('error');
    expect(e.error_code).toBe('rate_limit');
  });
});

describe('[integration] telemetry — error code + Hermes', () => {
  it('CallMetricsAccumulator.recordError maps codes (never the message)', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'CAx',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
    });
    acc.recordError({ code: 'RATE_LIMIT', message: 'provider 429' });
    expect(acc.endCall().error_code).toBe('rate_limit');
    const acc2 = new CallMetricsAccumulator({
      callId: 'CAy',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
    });
    acc2.recordError(new Error('boom'));
    expect(acc2.endCall().error_code).toBe('other');
    // Node connection errors map to "connection" (parity with Python's
    // ConnectionError branch), not the raw "econnrefused".
    const acc3 = new CallMetricsAccumulator({
      callId: 'CAz',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
    });
    acc3.recordError({ code: 'ECONNREFUSED' });
    expect(acc3.endCall().error_code).toBe('connection');
  });

  it('PatterTool.hermesHandler() emits agent_configured{integration=hermes}', async () => {
    enableTelemetryEnv();
    const client = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    const fakePhone = { telemetry: client } as unknown as Patter;
    const tool = new PatterTool({ phone: fakePhone, agent: { systemPrompt: 'x' } });
    tool.hermesHandler(); // getting the handler signals Hermes integration
    await waitFor(collector, 1);
    await client.close();

    const e = collector.events.find((x) => x.event === 'agent_configured');
    expect(e?.integration).toBe('hermes');
  });
});

describe('[integration] telemetry — server wiring fires call_completed', () => {
  function makeConfig(): LocalConfig {
    return {
      twilioSid: 'ACtest000000000000000000000000000',
      twilioToken: 'tok',
      phoneNumber: '+15550001234',
      webhookUrl: 'abc.ngrok.io',
      telephonyProvider: 'twilio',
      requireSignature: false,
      persistRoot: null,
    } as LocalConfig;
  }
  const makeAgent = () => ({ systemPrompt: 'x', prewarm: false }) as never;

  it('a connected call (wrappedEnd) emits call_completed', async () => {
    enableTelemetryEnv();
    const server = new EmbeddedServer(
      makeConfig(),
      makeAgent(),
      undefined,
      undefined,
      undefined,
      undefined,
      false,
    );
    server.telemetry = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [, , wrappedEnd] = (server as any).wrapLoggingCallbacks({ telephonyProvider: 'twilio' });
    await wrappedEnd({
      call_id: 'CAx',
      metrics: {
        provider_mode: 'pipeline',
        llm_provider: 'openai',
        telephony_provider: 'twilio',
        duration_seconds: 73,
        latency_p95: { agent_response_ms: 1800 },
      },
    });
    await waitFor(collector, 1);
    await server.telemetry.close();

    const e = collector.events.find((x) => x.event === 'call_completed');
    expect(e).toBeDefined();
    expect(e?.outcome).toBe('completed');
    expect(e?.duration_seconds).toBe(73);
    expect(e?.latency_ms).toBe(1800);
  });

  it('a non-connected failure (resolveCompletion) emits call_completed', async () => {
    enableTelemetryEnv();
    const server = new EmbeddedServer(makeConfig(), makeAgent());
    server.telemetry = new TelemetryClient({ sdkVersion: '0.6.3', endpoint: collector.url });
    server.resolveCompletion('CAy', { outcome: 'no_answer', status: 'no-answer' });
    await waitFor(collector, 1);
    await server.telemetry.close();

    const e = collector.events.find((x) => x.event === 'call_completed');
    expect(e).toBeDefined();
    expect(e?.outcome).toBe('no_answer');
    expect(e?.carrier).toBe('twilio');
  });
});

describe('[unit] telemetry — buildEvent', () => {
  it('has required anonymous fields', () => {
    const event = buildEvent('sdk_initialized', {
      sdkVersion: '1.2.3',
      dimensions: { engine: 'realtime' },
    });
    expect(event.event).toBe('sdk_initialized');
    expect(event.sdk).toBe('typescript');
    expect(event.sdk_version).toBe('1.2.3');
    expect(event.runtime).toBe('node');
    expect(['linux', 'darwin', 'windows', 'unknown']).toContain(event.os);
    expect(String(event.runtime_version)).toContain('.');
    expect(typeof event.ci).toBe('boolean');
    expect(event.engine).toBe('realtime');
  });

  it('rejects an unknown event name', () => {
    expect(() => buildEvent('definitely_not_an_event', { sdkVersion: '1.0.0' })).toThrow();
  });

  it('coerces an off-list dimension value to "other"', () => {
    const event = buildEvent('feature_used', {
      sdkVersion: '1.0.0',
      dimensions: { provider: 'AcmeSecretVendorLLM', engine: 'realtime' },
    });
    expect(event.provider).toBe('other'); // coerced
    expect(event.engine).toBe('realtime'); // on-list, preserved
    expect(JSON.stringify(event)).not.toContain('AcmeSecretVendorLLM');
  });

  it('keeps known dimension values', () => {
    const event = buildEvent('feature_used', {
      sdkVersion: '1.0.0',
      dimensions: { provider: 'deepgram', carrier: 'telnyx' },
    });
    expect(event.provider).toBe('deepgram');
    expect(event.carrier).toBe('telnyx');
  });

  it('coerces off-list values on the agent_configured dimensions', () => {
    const event = buildEvent('agent_configured', {
      sdkVersion: '1.0.0',
      dimensions: {
        integration: 'SomeCustomerBrand', // off-list -> other
        integration_kind: 'consult',
        custom_tool_count_bucket: '2_3',
        builtin_tool_count: 1, // numeric passthrough
      },
    });
    expect(event.integration).toBe('other');
    expect(event.integration_kind).toBe('consult');
    expect(event.custom_tool_count_bucket).toBe('2_3');
    expect(event.builtin_tool_count).toBe(1);
    expect(JSON.stringify(event)).not.toContain('SomeCustomerBrand');
  });
});

describe('[unit] stack capture (carrier + STT + TTS + LLM, schema v3)', () => {
  // Build a stand-in provider adapter exposing a static providerKey + model.
  const mk = (providerKey: string, model: string): unknown => {
    class P {
      static readonly providerKey = providerKey;
      model = model;
    }
    return new P();
  };

  it('normalizes known model ids and strips the release date', () => {
    expect(modelToken('deepgram', 'nova-3')).toBe('deepgram-nova-3');
    expect(modelToken('openai', 'gpt-4o')).toBe('openai-gpt-4o');
    expect(modelToken('elevenlabs', 'eleven_flash_v2_5')).toBe('elevenlabs-eleven-flash-v2-5');
    expect(modelToken('anthropic', 'claude-haiku-4-5-20251001')).toBe('anthropic-claude-haiku-4-5');
  });

  it('coerces PII-risky model strings to "{vendor}-other"', () => {
    expect(modelToken('openai', 'ft:gpt-4o:acme-corp:custom:xZ9')).toBe('openai-other');
    expect(modelToken('openai', 'openclaw/agent-x')).toBe('openai-other');
    expect(modelToken('openai', 'my custom model')).toBe('openai-other');
    expect(modelToken('openai', 'x'.repeat(50))).toBe('openai-other');
    expect(modelToken('openai', '')).toBe('openai-other');
  });

  it('maps providerKey aliases to the vendor family', () => {
    expect(vendorOf('cartesia_tts')).toBe('cartesia');
    expect(vendorOf('openai_tts')).toBe('openai');
    expect(vendorOf('elevenlabs_ws')).toBe('elevenlabs');
    expect(vendorOf('deepgram')).toBe('deepgram');
    expect(vendorOf('totally-unknown')).toBe('other');
    expect(vendorOf(null)).toBe('other');
  });

  it('captures the full pipeline stack', () => {
    const dims = stackDimensions(
      mk('deepgram', 'nova-3'),
      mk('elevenlabs', 'eleven_turbo_v2_5'),
      mk('anthropic', 'claude-opus-4-8'),
    );
    expect(dims).toEqual({
      stt_provider: 'deepgram',
      stt_model: 'deepgram-nova-3',
      tts_provider: 'elevenlabs',
      tts_model: 'elevenlabs-eleven-turbo-v2-5',
      llm_provider: 'anthropic',
      llm_model: 'anthropic-claude-opus-4-8',
    });
  });

  it('omits absent layers; buildEvent carries stack and drops forged models', () => {
    const dims = stackDimensions(undefined, undefined, mk('openai', 'gpt-4o'));
    expect(dims).toEqual({ llm_provider: 'openai', llm_model: 'openai-gpt-4o' });

    const ev = buildEvent('feature_used', {
      sdkVersion: '0.6.3',
      dimensions: { ...dims, engine: 'pipeline' },
    });
    expect(ev.llm_model).toBe('openai-gpt-4o');
    const ev2 = buildEvent('feature_used', {
      sdkVersion: '0.6.3',
      dimensions: { stt_provider: 'nsa' },
    });
    expect(ev2.stt_provider).toBe('other');
    const ev3 = buildEvent('feature_used', {
      sdkVersion: '0.6.3',
      dimensions: { llm_model: 'BAD/with:stuff' },
    });
    expect(ev3.llm_model).toBeUndefined();
  });
});

describe('[unit] install id + per-call cost (schema v3)', () => {
  it('buildEvent carries a 32-hex install_id and keeps cost_usd as a float', () => {
    const ev = buildEvent('call_completed', {
      sdkVersion: '0.6.3',
      dimensions: { outcome: 'completed', cost_usd: 0.0123 },
    });
    expect(String(ev.install_id)).toMatch(/^[0-9a-f]{32}$/);
    expect(ev.cost_usd).toBe(0.0123); // float preserved (cost is not rounded to int)
  });

  it('persists a stable anonymous install id to disk', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-iid-'));
    process.env.PATTER_TELEMETRY_STATE_DIR = dir;
    vi.resetModules();
    const mod = await import('../src/telemetry/install-id');
    const id = mod.installId();
    expect(id).toMatch(/^[0-9a-f]{32}$/);
    expect(mod.installId()).toBe(id); // stable within the process
    expect(fs.readFileSync(path.join(dir, 'install-id'), 'utf8').trim()).toBe(id);
  });
});

describe('[unit] deploy-shape + upgrade funnel (schema v4)', () => {
  it('env probes return allowlisted values', () => {
    expect(DIMENSION_VALUES.invoked_by_agent.has(invokedByAgent())).toBe(true);
    expect(DIMENSION_VALUES.serverless.has(serverless())).toBe(true);
    expect(DIMENSION_VALUES.cloud.has(cloud())).toBe(true);
    expect(DIMENSION_VALUES.package_manager.has(packageManager())).toBe(true);
    expect(typeof inContainer()).toBe('boolean');
  });

  it('buildEvent keeps v4 bool/enum/version dims and drops bad ones', () => {
    const ev = buildEvent('agent_configured', {
      sdkVersion: '0.6.4',
      dimensions: {
        noise_reduction: 'far_field',
        turn_detection: 'custom',
        preambles_used: true,
        per_tool_timeouts_set: false,
        llm_fallback_configured: true,
      },
    });
    expect(ev.noise_reduction).toBe('far_field');
    expect(ev.preambles_used).toBe(true);
    expect(ev.per_tool_timeouts_set).toBe(false);

    const ev2 = buildEvent('sdk_initialized', {
      sdkVersion: '0.6.4',
      dimensions: { cloud: 'mars', container: 'nope', previous_sdk_version: '0.6.3' },
    });
    expect(ev2.cloud).toBe('other'); // off-list enum coerced
    expect(ev2.container).toBeUndefined(); // non-bool dropped
    expect(ev2.previous_sdk_version).toBe('0.6.3');
  });

  it('version funnel returns the prior version, then records current', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-ver-'));
    process.env.PATTER_TELEMETRY_STATE_DIR = dir;
    vi.resetModules();
    const mod = await import('../src/telemetry/install-id');
    mod.installId();
    expect(mod.previousVersion('0.6.3')).toBe(''); // first run
    expect(mod.previousVersion('0.6.4')).toBe('0.6.3'); // now sees the prior
    expect(['0', '1_7', '8_30', '30_plus']).toContain(mod.daysSinceInstallBucket());
  });
});

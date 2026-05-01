/**
 * Unit tests for the v0.5.0 instance-based API: the ``Patter`` client accepts
 * carrier, engine, STT/TTS adapter, Tool/Guardrail, and tunnel instances.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import { Patter } from '../../src/client';
import {
  Twilio,
  Telnyx,
  OpenAIRealtime,
  ElevenLabsConvAI,
  DeepgramSTT,
  ElevenLabsTTS,
  Tool,
  Guardrail,
  CloudflareTunnel,
  StaticTunnel,
} from '../../src/index';

// Prevent the actual EmbeddedServer from starting during tests.
vi.mock('../../src/server', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../../src/server')>();
  class MockEmbeddedServer {
    voicemailMessage = '';
    start = vi.fn().mockResolvedValue(undefined);
    stop = vi.fn().mockResolvedValue(undefined);
    constructor(..._args: unknown[]) {}
  }
  return {
    ...orig,
    EmbeddedServer: MockEmbeddedServer,
  };
});

// ---------------------------------------------------------------------------
// Env var snapshot — restore after each test so nothing leaks between cases.
// ---------------------------------------------------------------------------

const TRACKED_ENV_KEYS = [
  'TWILIO_ACCOUNT_SID',
  'TWILIO_AUTH_TOKEN',
  'TELNYX_API_KEY',
  'TELNYX_CONNECTION_ID',
  'TELNYX_PUBLIC_KEY',
  'OPENAI_API_KEY',
  'ELEVENLABS_API_KEY',
  'ELEVENLABS_AGENT_ID',
  'DEEPGRAM_API_KEY',
] as const;

let envSnapshot: Record<string, string | undefined>;

beforeEach(() => {
  envSnapshot = {};
  for (const k of TRACKED_ENV_KEYS) {
    envSnapshot[k] = process.env[k];
    delete process.env[k];
  }
});

afterEach(() => {
  for (const k of TRACKED_ENV_KEYS) {
    const v = envSnapshot[k];
    if (v === undefined) {
      delete process.env[k];
    } else {
      process.env[k] = v;
    }
  }
});

// ---------------------------------------------------------------------------
// Carrier — Twilio / Telnyx
// ---------------------------------------------------------------------------

describe('Patter({ carrier })', () => {
  it('accepts a Twilio carrier instance (local mode)', () => {
    const phone = new Patter({
      carrier: new Twilio({ accountSid: 'AC_test', authToken: 'tok' }),
      phoneNumber: '+15550001234',
    });
    expect(phone).toBeDefined();
  });

  it('accepts a Telnyx carrier instance (local mode)', () => {
    const phone = new Patter({
      carrier: new Telnyx({ apiKey: 'KEY_test', connectionId: 'conn' }),
      phoneNumber: '+15550001234',
    });
    expect(phone).toBeDefined();
  });

  it('throws without a carrier', () => {
    expect(
      () =>
        new Patter({
          phoneNumber: '+15550001234',
        } as never),
    ).toThrow(/carrier/);
  });
});

// ---------------------------------------------------------------------------
// Engine — OpenAIRealtime / ElevenLabsConvAI
// ---------------------------------------------------------------------------

describe('phone.agent({ engine })', () => {
  function makePhone() {
    return new Patter({
      carrier: new Twilio({ accountSid: 'AC', authToken: 'tok' }),
      phoneNumber: '+15550001234',
      webhookUrl: 'abc.example.com',
    });
  }

  it('OpenAIRealtime engine sets provider to openai_realtime', () => {
    const phone = makePhone();
    const agent = phone.agent({
      engine: new OpenAIRealtime({ apiKey: 'sk-x', voice: 'shimmer' }),
      systemPrompt: 'hi',
    });
    expect(agent.provider).toBe('openai_realtime');
    expect(agent.voice).toBe('shimmer');
    expect(agent.model).toBeTruthy();
  });

  it('ElevenLabsConvAI engine sets provider to elevenlabs_convai', () => {
    const phone = makePhone();
    const agent = phone.agent({
      engine: new ElevenLabsConvAI({ apiKey: 'el_x', agentId: 'agt_x' }),
      systemPrompt: 'hi',
    });
    expect(agent.provider).toBe('elevenlabs_convai');
    expect(agent.engine).toBeDefined();
  });

  it('throws when both `engine` and `provider` are passed', () => {
    const phone = makePhone();
    expect(() =>
      phone.agent({
        engine: new OpenAIRealtime({ apiKey: 'sk-x' }),
        provider: 'openai_realtime',
        systemPrompt: 'hi',
      }),
    ).toThrow(/engine.*provider/);
  });
});

// ---------------------------------------------------------------------------
// STT / TTS instance form
// ---------------------------------------------------------------------------

describe('phone.agent({ stt / tts })', () => {
  function makePhone() {
    return new Patter({
      carrier: new Twilio({ accountSid: 'AC', authToken: 'tok' }),
      phoneNumber: '+15550001234',
      webhookUrl: 'abc.example.com',
    });
  }

  it('passes a DeepgramSTT instance through untouched', () => {
    const phone = makePhone();
    const stt = new DeepgramSTT({ apiKey: 'dg_x' });
    const agent = phone.agent({
      provider: 'pipeline',
      stt,
      systemPrompt: '',
    });
    // The instance should be stored as-is (not converted).
    expect(agent.stt).toBe(stt);
  });

  it('passes an ElevenLabsTTS instance through untouched', () => {
    const phone = makePhone();
    const tts = new ElevenLabsTTS({ apiKey: 'el_x' });
    const agent = phone.agent({
      provider: 'pipeline',
      tts,
      systemPrompt: '',
    });
    expect(agent.tts).toBe(tts);
  });
});

// ---------------------------------------------------------------------------
// Tool / Guardrail instances
// ---------------------------------------------------------------------------

describe('phone.agent({ tools / guardrails })', () => {
  function makePhone() {
    return new Patter({
      carrier: new Twilio({ accountSid: 'AC', authToken: 'tok' }),
      phoneNumber: '+15550001234',
      webhookUrl: 'abc.example.com',
    });
  }

  it('accepts a Tool class instance in tools[]', () => {
    const phone = makePhone();
    const tool = new Tool({
      name: 'ping',
      description: 'returns pong',
      handler: async () => 'pong',
    });
    const agent = phone.agent({
      provider: 'openai_realtime',
      systemPrompt: 'hi',
      tools: [tool],
    });
    expect(agent.tools).toHaveLength(1);
    expect(agent.tools?.[0]?.name).toBe('ping');
    expect(typeof agent.tools?.[0]?.handler).toBe('function');
  });

  it('accepts a Guardrail class instance in guardrails[]', () => {
    const phone = makePhone();
    const guard = new Guardrail({ name: 'safety', blockedTerms: ['forbidden'] });
    const agent = phone.agent({
      provider: 'openai_realtime',
      systemPrompt: 'hi',
      guardrails: [guard],
    });
    expect(agent.guardrails).toHaveLength(1);
    expect(agent.guardrails?.[0]?.name).toBe('safety');
    expect(agent.guardrails?.[0]?.blockedTerms).toEqual(['forbidden']);
  });
});

// ---------------------------------------------------------------------------
// Tunnel
// ---------------------------------------------------------------------------

describe('Patter({ tunnel })', () => {
  it('accepts a CloudflareTunnel instance', () => {
    const phone = new Patter({
      carrier: new Twilio({ accountSid: 'AC', authToken: 'tok' }),
      phoneNumber: '+15550001234',
      tunnel: new CloudflareTunnel(),
    });
    expect(phone).toBeDefined();
  });

  it('accepts a StaticTunnel instance and normalizes its hostname into webhookUrl', () => {
    const phone = new Patter({
      carrier: new Twilio({ accountSid: 'AC', authToken: 'tok' }),
      phoneNumber: '+15550001234',
      tunnel: new StaticTunnel({ hostname: 'abc.ngrok.io' }),
    });
    // Peek at the internal config via the localConfig path — use a cast for the test.
    const internal = (phone as unknown as { localConfig: { webhookUrl?: string } }).localConfig;
    expect(internal.webhookUrl).toBe('abc.ngrok.io');
  });

  it('throws when StaticTunnel and webhookUrl are both provided', () => {
    expect(
      () =>
        new Patter({
          carrier: new Twilio({ accountSid: 'AC', authToken: 'tok' }),
          phoneNumber: '+15550001234',
          webhookUrl: 'existing.example.com',
          tunnel: new StaticTunnel({ hostname: 'abc.ngrok.io' }),
        }),
    ).toThrow(/Cannot use both/);
  });

  it('StaticTunnel hostname has http(s):// scheme and trailing slash stripped', () => {
    const phone = new Patter({
      carrier: new Twilio({ accountSid: 'AC', authToken: 'tok' }),
      phoneNumber: '+15550001234',
      tunnel: new StaticTunnel({ hostname: 'https://abc.ngrok.io/' }),
    });
    const internal = (phone as unknown as { localConfig: { webhookUrl?: string } }).localConfig;
    expect(internal.webhookUrl).toBe('abc.ngrok.io');
  });
});

// ---------------------------------------------------------------------------
// 4-line quickstart
// ---------------------------------------------------------------------------

describe('4-line quickstart', () => {
  it('Patter + Twilio + OpenAIRealtime composes cleanly', () => {
    process.env.TWILIO_ACCOUNT_SID = 'AC_env';
    process.env.TWILIO_AUTH_TOKEN = 'tok_env';
    process.env.OPENAI_API_KEY = 'sk-env';

    const phone = new Patter({
      carrier: new Twilio(),
      phoneNumber: '+15550001234',
    });
    const agent = phone.agent({
      engine: new OpenAIRealtime(),
      systemPrompt: 'hi',
    });

    expect(phone).toBeDefined();
    expect(agent.provider).toBe('openai_realtime');
  });
});

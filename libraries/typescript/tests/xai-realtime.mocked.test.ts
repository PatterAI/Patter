import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the 'ws' module so connect() makes no real network connection. The mock
// records constructor args (url + headers) and lets tests drive server frames.
vi.mock('ws', () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const EventEmitter = require('events');
  class MockWebSocket extends EventEmitter {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    static instances: MockWebSocket[] = [];
    readyState = MockWebSocket.OPEN;
    sent: string[] = [];
    url: string;
    options: unknown;
    constructor(url: string, options?: unknown) {
      super();
      this.url = url;
      this.options = options;
      MockWebSocket.instances.push(this);
    }
    send(data: string): void {
      this.sent.push(data);
    }
    ping(): void {
      /* heartbeat no-op */
    }
    close(): void {
      this.readyState = MockWebSocket.CLOSED;
    }
  }
  return { default: MockWebSocket };
});

import {
  XaiRealtimeAdapter,
  XAI_REALTIME_WS_URL,
  XAI_REALTIME_DEFAULT_MODEL,
  XAI_REALTIME_DEFAULT_VOICE,
} from '../src/providers/xai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { XaiRealtime } from '../src/engines/xai';
import { buildAIAdapter, type LocalConfig } from '../src/server';
import type { AgentOptions } from '../src/types';
import { Patter } from '../src/client';
import { Twilio } from '../src/index';
import {
  DEFAULT_PRICING,
  calculateRealtimeCost,
  calculateSttCost,
  calculateTtsCost,
} from '../src/pricing';
import { CallMetricsAccumulator, type CostBreakdown } from '../src/metrics';

type MockWS = {
  readonly sent: string[];
  readonly url: string;
  readonly options: { headers?: Record<string, string> };
  emit: (event: string, ...args: unknown[]) => void;
};

async function getWsCtor(): Promise<{ instances: MockWS[] }> {
  const mod = (await import('ws')).default as unknown as { instances: MockWS[] };
  return mod;
}

const CONFIG: LocalConfig = {
  phoneNumber: '+15555550100',
  webhookUrl: 'https://example.com/voice',
};

/** Read the session body from the first `session.update` sent after `session.created`. */
function sessionUpdateBody(ws: MockWS): Record<string, unknown> {
  const parsed = JSON.parse(ws.sent[0]) as { type: string; session: Record<string, unknown> };
  expect(parsed.type).toBe('session.update');
  return parsed.session;
}

describe('[unit] XaiRealtime engine marker', () => {
  it('throws without an apiKey and no env', () => {
    const prev = process.env.XAI_API_KEY;
    delete process.env.XAI_API_KEY;
    try {
      expect(() => new XaiRealtime()).toThrow(/xAI Realtime requires an apiKey/);
    } finally {
      if (prev !== undefined) process.env.XAI_API_KEY = prev;
    }
  });

  it('reads XAI_API_KEY from the environment and carries the xai_realtime kind', () => {
    const prev = process.env.XAI_API_KEY;
    process.env.XAI_API_KEY = 'xai-env-key';
    try {
      const engine = new XaiRealtime();
      expect(engine.apiKey).toBe('xai-env-key');
      expect(engine.kind).toBe('xai_realtime');
    } finally {
      if (prev === undefined) delete process.env.XAI_API_KEY;
      else process.env.XAI_API_KEY = prev;
    }
  });

  it('captures the xAI-specific knobs on the marker', () => {
    const engine = new XaiRealtime({
      apiKey: 'xai-test-key',
      reasoningEffort: 'none',
      languageHint: 'it',
      keyterms: ['Grok'],
      serverTools: [{ type: 'web_search' }],
    });
    expect(engine.reasoningEffort).toBe('none');
    expect(engine.languageHint).toBe('it');
    expect(engine.keyterms).toEqual(['Grok']);
    expect(engine.serverTools).toEqual([{ type: 'web_search' }]);
  });

  it('validates turnDetection eagerly (rejects bad values)', () => {
    expect(
      () =>
        new XaiRealtime({
          apiKey: 'xai-test-key',
          // @ts-expect-error intentionally invalid for the runtime guard
          turnDetection: { type: 'nope' },
        }),
    ).toThrow(/server_vad|semantic_vad/);
  });
});

describe('[unit] XaiRealtime dispatch', () => {
  it('Patter.agent({ engine: new XaiRealtime() }) selects provider xai_realtime', () => {
    const phone = new Patter({
      carrier: new Twilio({ accountSid: 'AC_test', authToken: 'tok_test' }),
      phoneNumber: '+15550000000',
      webhookUrl: 'abc.ngrok.io',
    });
    const agent = phone.agent({
      engine: new XaiRealtime({ apiKey: 'xai-test-key', voice: 'leo' }),
      systemPrompt: 'You are helpful.',
    });
    expect(agent.provider).toBe('xai_realtime');
    expect(agent.voice).toBe('leo');
  });

  it('buildAIAdapter routes xai_realtime to XaiRealtimeAdapter (GA + base subclass)', () => {
    const agent: AgentOptions = {
      systemPrompt: 'You are helpful.',
      engine: new XaiRealtime({ apiKey: 'xai-test-key' }),
      provider: 'xai_realtime',
    };
    const adapter = buildAIAdapter(CONFIG, agent);
    expect(adapter).toBeInstanceOf(XaiRealtimeAdapter);
    // Subclass of the GA adapter (→ inherits the PCM24↔mulaw8 transcode path)
    // AND the base adapter (→ every `instanceof OpenAIRealtimeAdapter` feature
    // gate in the stream handler fires for xAI with no per-provider branches).
    expect(adapter).toBeInstanceOf(OpenAIRealtime2Adapter);
    expect(adapter).toBeInstanceOf(OpenAIRealtimeAdapter);
  });

  it('throws a clear error when provider is xai_realtime but the engine is missing', () => {
    const agent = { systemPrompt: 'hi', provider: 'xai_realtime' } as unknown as AgentOptions;
    expect(() => buildAIAdapter(CONFIG, agent)).toThrow(/xAI Realtime mode requires/);
  });
});

describe('[mocked] XaiRealtimeAdapter connect', () => {
  beforeEach(async () => {
    const ws = await getWsCtor();
    ws.instances.length = 0;
  });

  it('connects to the xAI endpoint with a Bearer key and grafts the xAI session keys', async () => {
    const adapter = new XaiRealtimeAdapter('xai-test-key', {
      reasoningEffort: 'none',
      languageHint: 'it',
      keyterms: ['Grok', 'xAI'],
      speed: 1.2,
      vadThreshold: 0.6,
      prefixPaddingMs: 250,
      idleTimeoutMs: 5000,
      replace: { 'Acme Mobile': 'Acme Mobull' },
      resumption: true,
      serverTools: [{ type: 'web_search' }],
    });
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];

    // Endpoint + auth + default model.
    expect(ws.url).toBe(`${XAI_REALTIME_WS_URL}?model=grok-voice-latest`);
    expect(ws.options.headers?.Authorization).toBe('Bearer xai-test-key');

    // Handshake: server says session.created -> adapter sends the grafted update.
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    const s = sessionUpdateBody(ws);
    const audio = s.audio as {
      input: { transcription: Record<string, unknown>; turn_detection: Record<string, unknown> };
      output: Record<string, unknown>;
    };
    expect(s.reasoning).toEqual({ effort: 'none' });
    // xAI-valid transcription model (NOT OpenAI's whisper-1), and the
    // OpenAI-only `language` key never leaks into the xAI session.
    expect(audio.input.transcription.model).toBe('grok-transcribe');
    expect(audio.input.transcription.language).toBeUndefined();
    expect(audio.input.transcription.language_hint).toBe('it');
    expect(audio.input.transcription.keyterms).toEqual(['Grok', 'xAI']);
    expect(audio.output.speed).toBe(1.2);
    expect(audio.output.voice).toBe('eve');
    expect(audio.input.turn_detection.threshold).toBe(0.6);
    expect(audio.input.turn_detection.prefix_padding_ms).toBe(250);
    expect(audio.input.turn_detection.idle_timeout_ms).toBe(5000);
    expect(s.replace).toEqual({ 'Acme Mobile': 'Acme Mobull' });
    expect(s.resumption).toEqual({ enabled: true });
    expect(s.tools).toContainEqual({ type: 'web_search' });

    ws.emit('message', JSON.stringify({ type: 'session.updated' }));
    await p;
    adapter.close();
  });

  it('omits every xAI-specific key when unset and uses the eve/grok defaults', async () => {
    const adapter = new XaiRealtimeAdapter('xai-test-key');
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];

    expect(ws.url).toBe(
      `${XAI_REALTIME_WS_URL}?model=${encodeURIComponent(XAI_REALTIME_DEFAULT_MODEL)}`,
    );

    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    const s = sessionUpdateBody(ws);
    const audio = s.audio as {
      input: { transcription: Record<string, unknown>; turn_detection: Record<string, unknown> };
      output: Record<string, unknown>;
    };
    expect(s.reasoning).toBeUndefined();
    // Transcription model still defaults to xAI's grok-transcribe, and no
    // OpenAI-only `language` key leaks even with no config.
    expect(audio.input.transcription.model).toBe('grok-transcribe');
    expect(audio.input.transcription.language).toBeUndefined();
    expect(audio.input.transcription.language_hint).toBeUndefined();
    expect(audio.input.transcription.keyterms).toBeUndefined();
    expect(audio.output.speed).toBeUndefined();
    expect(audio.output.voice).toBe(XAI_REALTIME_DEFAULT_VOICE);
    expect(audio.input.turn_detection.idle_timeout_ms).toBeUndefined();
    // Inherited GA default threshold (0.5) when vadThreshold is unset.
    expect(audio.input.turn_detection.threshold).toBe(0.5);
    expect(s.replace).toBeUndefined();
    expect(s.resumption).toBeUndefined();
    expect(s.tools).toBeUndefined();

    ws.emit('message', JSON.stringify({ type: 'session.updated' }));
    await p;
    adapter.close();
  });

  it('honors a baseUrl override (trailing slash trimmed)', async () => {
    const adapter = new XaiRealtimeAdapter('xai-test-key', { baseUrl: 'wss://example.test/rt/', model: 'grok-voice-fast-1.0' });
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];
    expect(ws.url).toBe('wss://example.test/rt?model=grok-voice-fast-1.0');
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    ws.emit('message', JSON.stringify({ type: 'session.updated' }));
    await p;
    adapter.close();
  });

  it('surfaces a setup error frame as a clear rejection', async () => {
    const adapter = new XaiRealtimeAdapter('xai-test-key', { model: 'bad-model' });
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    ws.emit('message', JSON.stringify({ type: 'error', error: { message: 'model not found' } }));
    await expect(p).rejects.toThrow(/model not found/);
  });

  it('warmup is a no-op and openParkedConnection is unsupported (no OpenAI endpoint hit)', async () => {
    const adapter = new XaiRealtimeAdapter('xai-test-key');
    await expect(adapter.warmup()).resolves.toBeUndefined();
    await expect(adapter.openParkedConnection()).rejects.toThrow(/not supported/);
    const ws = await getWsCtor();
    expect(ws.instances.length).toBe(0);
  });
});

describe('[mocked] openai-realtime-2 realtimeBaseUrl() refactor regression', () => {
  beforeEach(async () => {
    const ws = await getWsCtor();
    ws.instances.length = 0;
  });

  it('OpenAIRealtime2Adapter still connects to the OpenAI GA endpoint (byte-identical URL)', async () => {
    const adapter = new OpenAIRealtime2Adapter('sk-test', 'gpt-realtime-2');
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];
    expect(ws.url).toBe('wss://api.openai.com/v1/realtime?model=gpt-realtime-2');
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    ws.emit('message', JSON.stringify({ type: 'session.updated' }));
    await p;
    adapter.close();
  });
});

describe('[unit] xAI pricing', () => {
  it('bills xai_realtime per MINUTE of session duration (0 when duration unknown)', () => {
    // Arg order (…, provider, durationSeconds) mirrors the Python keyword args.
    expect(calculateRealtimeCost({}, DEFAULT_PRICING, 'grok-voice-latest', 'xai_realtime', 60)).toBeCloseTo(0.05, 6);
    expect(calculateRealtimeCost({}, DEFAULT_PRICING, 'grok-voice-latest', 'xai_realtime', 30)).toBeCloseTo(0.025, 6);
    expect(calculateRealtimeCost({}, DEFAULT_PRICING, 'grok-voice-latest', 'xai_realtime', undefined)).toBe(0);
  });

  it('leaves the OpenAI token path unchanged (durationSeconds ignored)', () => {
    const usage = { output_token_details: { audio_tokens: 1000 } };
    expect(calculateRealtimeCost(usage, DEFAULT_PRICING, 'gpt-realtime-2')).toBeCloseTo(0.064, 6);
    // Passing a duration must NOT change the token-based result.
    expect(calculateRealtimeCost(usage, DEFAULT_PRICING, 'gpt-realtime-2', 'openai_realtime', 9999)).toBeCloseTo(0.064, 6);
  });

  it('prices xAI streaming STT ($0.20/hr) and TTS ($15/1M chars)', () => {
    expect(calculateSttCost('xai', 60, DEFAULT_PRICING)).toBeCloseTo(0.003333, 6);
    expect(calculateTtsCost('xai_tts', 1000, DEFAULT_PRICING)).toBeCloseTo(0.015, 6);
  });

  it('exposes the documented (unmetered) per-message realtime constant', () => {
    expect(DEFAULT_PRICING.xai_realtime.text_input_per_message).toBe(0.004);
  });
});

describe('[unit] xAI realtime metrics wiring', () => {
  // Reach the private cost builder directly with a controlled duration — a real
  // call derives duration from wall-clock, which can't be pinned to 2 min in a
  // unit test. Mirrors the house pattern (openai-realtime-2-session).
  function computeCost(acc: CallMetricsAccumulator, durationSeconds: number): CostBreakdown {
    return (acc as unknown as { _computeCost(d: number): CostBreakdown })._computeCost(
      durationSeconds,
    );
  }

  it('bills the xai_realtime session per minute via the metrics accumulator', () => {
    // 2 min × $0.05/min = $0.10. This exercises the metrics call-site arg order
    // (…, provider, durationSeconds): a future swap makes pricing[<number>]
    // undefined → $0, which this assertion catches loudly.
    const acc = new CallMetricsAccumulator({
      callId: 'c-xai',
      providerMode: 'xai_realtime',
      telephonyProvider: 'twilio',
      realtimeModel: 'grok-voice-latest',
    });
    expect(computeCost(acc, 120).llm).toBeCloseTo(0.1, 6);
  });

  it('ignores a stray OpenAI-shaped usage block for an xai_realtime call', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c-xai-2',
      providerMode: 'xai_realtime',
      telephonyProvider: 'twilio',
      realtimeModel: 'grok-voice-latest',
    });
    // xAI is GA-compatible and could emit a token usage block on response.done;
    // it must NOT inflate the per-minute cost nor surface a bogus cached-savings.
    acc.recordRealtimeUsage({
      input_token_details: {
        audio_tokens: 5000,
        text_tokens: 1000,
        cached_tokens_details: { audio_tokens: 4000, text_tokens: 500 },
      },
      output_token_details: { audio_tokens: 8000, text_tokens: 200 },
    });
    const cost = computeCost(acc, 120);
    expect(cost.llm).toBeCloseTo(0.1, 6); // still just the per-minute charge
    expect(cost.llm_cached_savings).toBe(0);
  });
});

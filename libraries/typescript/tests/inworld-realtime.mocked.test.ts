import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the 'ws' module so connect() makes no real network connection. The mock
// records constructor args (url + headers) and lets tests drive server frames.
vi.mock('ws', () => {
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
  InworldRealtimeAdapter,
  INWORLD_REALTIME_WS_URL,
  INWORLD_REALTIME_DEFAULT_MODEL,
  INWORLD_REALTIME_DEFAULT_VOICE,
} from '../src/providers/inworld-realtime';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { InworldRealtime } from '../src/engines/inworld';
import { buildAIAdapter, type LocalConfig } from '../src/server';
import type { AgentOptions } from '../src/types';
import { Patter } from '../src/client';
import { Twilio } from '../src/index';

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

describe('[unit] InworldRealtime engine marker', () => {
  it('throws without an apiKey and no env', () => {
    const prev = process.env.INWORLD_API_KEY;
    delete process.env.INWORLD_API_KEY;
    try {
      expect(() => new InworldRealtime()).toThrow(/Inworld Realtime requires an apiKey/);
    } finally {
      if (prev !== undefined) process.env.INWORLD_API_KEY = prev;
    }
  });

  it('reads INWORLD_API_KEY from the environment', () => {
    const prev = process.env.INWORLD_API_KEY;
    process.env.INWORLD_API_KEY = 'rt_env_key';
    try {
      const engine = new InworldRealtime();
      expect(engine.apiKey).toBe('rt_env_key');
      expect(engine.kind).toBe('inworld_realtime');
    } finally {
      if (prev === undefined) delete process.env.INWORLD_API_KEY;
      else process.env.INWORLD_API_KEY = prev;
    }
  });

  it('validates turnDetection eagerly (rejects bad values)', () => {
    expect(
      () =>
        new InworldRealtime({
          apiKey: 'k',
          // @ts-expect-error intentionally invalid for the runtime guard
          turnDetection: { type: 'nope' },
        }),
    ).toThrow(/server_vad|semantic_vad/);
  });
});

describe('[unit] InworldRealtime dispatch', () => {
  it('Patter.agent({ engine: new InworldRealtime() }) selects provider inworld_realtime', () => {
    const phone = new Patter({
      carrier: new Twilio({ accountSid: 'AC_test', authToken: 'tok_test' }),
      phoneNumber: '+15550000000',
      webhookUrl: 'abc.ngrok.io',
    });
    const agent = phone.agent({
      engine: new InworldRealtime({ apiKey: 'rt_key', voice: 'Olivia' }),
      systemPrompt: 'You are helpful.',
    });
    expect(agent.provider).toBe('inworld_realtime');
    expect(agent.voice).toBe('Olivia');
  });

  it('buildAIAdapter routes inworld_realtime to InworldRealtimeAdapter (and OpenAIRealtimeAdapter subclass)', () => {
    const agent: AgentOptions = {
      systemPrompt: 'You are helpful.',
      engine: new InworldRealtime({ apiKey: 'rt_key' }),
      provider: 'inworld_realtime',
    };
    const adapter = buildAIAdapter(CONFIG, agent);
    expect(adapter).toBeInstanceOf(InworldRealtimeAdapter);
    // Subclass of the OpenAI adapter, so every `instanceof OpenAIRealtimeAdapter`
    // feature gate in the stream handler (barge-in, sendText, function_call,
    // truncate) fires for Inworld too — no per-provider branches needed.
    expect(adapter).toBeInstanceOf(OpenAIRealtimeAdapter);
  });

  it('throws a clear error when provider is inworld_realtime but the engine is missing', () => {
    const agent = {
      systemPrompt: 'hi',
      provider: 'inworld_realtime',
    } as unknown as AgentOptions;
    expect(() => buildAIAdapter(CONFIG, agent)).toThrow(/Inworld Realtime mode requires/);
  });
});

describe('[mocked] InworldRealtimeAdapter connect', () => {
  beforeEach(async () => {
    const ws = await getWsCtor();
    ws.instances.length = 0;
  });

  it('connects to the Inworld endpoint with a Bearer key and OpenAI-compatible handshake', async () => {
    const adapter = new InworldRealtimeAdapter('rt_key', { model: 'inworld-realtime', voice: 'Ashley' });
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];

    // Endpoint + auth.
    expect(ws.url).toBe(`${INWORLD_REALTIME_WS_URL}?model=inworld-realtime`);
    expect(ws.options.headers?.Authorization).toBe('Bearer rt_key');

    // Handshake: server says session.created -> adapter sends session.update.
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    const update = JSON.parse(ws.sent[0]) as { type: string; session: Record<string, unknown> };
    expect(update.type).toBe('session.update');
    expect(update.session.voice).toBe('Ashley');

    // session.updated -> connect resolves.
    ws.emit('message', JSON.stringify({ type: 'session.updated' }));
    await p;
    adapter.close();
  });

  it('uses the default model + voice when none supplied', async () => {
    const adapter = new InworldRealtimeAdapter('rt_key');
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];
    expect(ws.url).toBe(
      `${INWORLD_REALTIME_WS_URL}?model=${encodeURIComponent(INWORLD_REALTIME_DEFAULT_MODEL)}`,
    );
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    const update = JSON.parse(ws.sent[0]) as { session: { voice: string } };
    expect(update.session.voice).toBe(INWORLD_REALTIME_DEFAULT_VOICE);
    ws.emit('message', JSON.stringify({ type: 'session.updated' }));
    await p;
    adapter.close();
  });

  it('honors a baseUrl override', async () => {
    const adapter = new InworldRealtimeAdapter('rt_key', {
      baseUrl: 'wss://example.test/rt/',
      model: 'm1',
    });
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];
    // Trailing slash trimmed.
    expect(ws.url).toBe('wss://example.test/rt?model=m1');
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    ws.emit('message', JSON.stringify({ type: 'session.updated' }));
    await p;
    adapter.close();
  });

  it('surfaces a setup error frame as a clear rejection', async () => {
    const adapter = new InworldRealtimeAdapter('rt_key', { model: 'bad-model' });
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    ws.emit('message', JSON.stringify({ type: 'error', error: { message: 'model not found' } }));
    await expect(p).rejects.toThrow(/Inworld Realtime setup error: model not found/);
  });

  it('dispatches audio after the session attaches (a call can hear the model)', async () => {
    const adapter = new InworldRealtimeAdapter('rt_key', { model: 'm1' });
    const audio: Buffer[] = [];
    adapter.onEvent((type, data) => {
      if (type === 'audio') audio.push(data as Buffer);
    });
    const p = adapter.connect();
    const ws = (await getWsCtor()).instances[0];
    ws.emit('message', JSON.stringify({ type: 'session.created' }));
    ws.emit('message', JSON.stringify({ type: 'session.updated' }));
    await p;

    const payload = Buffer.from([0xff, 0x7f, 0x00, 0x10]).toString('base64');
    ws.emit('message', JSON.stringify({ type: 'response.audio.delta', delta: payload }));
    await vi.waitFor(() => expect(audio.length).toBe(1));
    expect(Buffer.compare(audio[0], Buffer.from([0xff, 0x7f, 0x00, 0x10]))).toBe(0);
    adapter.close();
  });

  it('warmup is a no-op and openParkedConnection is unsupported (no OpenAI endpoint hit)', async () => {
    const adapter = new InworldRealtimeAdapter('rt_key');
    await expect(adapter.warmup()).resolves.toBeUndefined();
    await expect(adapter.openParkedConnection()).rejects.toThrow(/not supported/);
    // No socket should have been opened by either call.
    const ws = await getWsCtor();
    expect(ws.instances.length).toBe(0);
  });
});

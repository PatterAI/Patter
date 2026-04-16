/**
 * Unit tests for CartesiaSTT (TypeScript).
 *
 * MOCK: no real API calls. These tests mock the `ws` module and emit
 * synthetic JSON frames that imitate Cartesia's STT WebSocket protocol
 * (`transcript` with `is_final`, `flush_done`, `done`, `error`).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('ws', () => {
  class MockWebSocket {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = MockWebSocket.OPEN;
    sent: unknown[] = [];
    pinged = 0;

    private listeners: Record<string, Array<(arg?: unknown) => void>> = {};
    public readonly url: string;
    public readonly opts: unknown;

    constructor(url: string, opts?: unknown) {
      this.url = url;
      this.opts = opts;
      (globalThis as Record<string, unknown>).__lastMockWs = this;
      queueMicrotask(() => this.emit('open'));
    }

    on(event: string, cb: (arg?: unknown) => void): this {
      (this.listeners[event] = this.listeners[event] ?? []).push(cb);
      return this;
    }
    once(event: string, cb: (arg?: unknown) => void): this {
      const wrap = (arg?: unknown) => {
        this.off(event, wrap);
        cb(arg);
      };
      return this.on(event, wrap);
    }
    off(event: string, cb: (arg?: unknown) => void): this {
      const list = this.listeners[event];
      if (!list) return this;
      const idx = list.indexOf(cb);
      if (idx >= 0) list.splice(idx, 1);
      return this;
    }
    emit(event: string, arg?: unknown): void {
      const list = this.listeners[event];
      if (!list) return;
      for (const cb of [...list]) cb(arg);
    }
    send(data: unknown): void {
      this.sent.push(data);
    }
    ping(): void {
      this.pinged++;
    }
    close(): void {
      this.readyState = MockWebSocket.CLOSED;
      this.emit('close');
    }
    pushMessage(payload: unknown): void {
      this.emit('message', Buffer.from(JSON.stringify(payload)));
    }
  }
  return { default: MockWebSocket };
});

import { CartesiaSTT, type Transcript } from '../src/providers/cartesia-stt';

interface MockWsShape {
  readyState: number;
  sent: unknown[];
  url: string;
  pinged: number;
  close: () => void;
  pushMessage: (payload: unknown) => void;
}

function lastMock(): MockWsShape {
  const m = (globalThis as Record<string, unknown>).__lastMockWs as MockWsShape | undefined;
  if (!m) throw new Error('No MockWebSocket constructed yet');
  return m;
}

beforeEach(() => {
  (globalThis as Record<string, unknown>).__lastMockWs = undefined;
});

describe('CartesiaSTT — construction', () => {
  it('throws on empty api key', () => {
    expect(() => new CartesiaSTT('')).toThrow(/apiKey/);
  });

  it('accepts api key', () => {
    const stt = new CartesiaSTT('k');
    expect(stt).toBeDefined();
  });
});

describe('CartesiaSTT — URL building', () => {
  it('builds WS URL with defaults', async () => {
    const stt = new CartesiaSTT('k');
    await stt.connect();
    const url = lastMock().url;
    expect(url.startsWith('wss://api.cartesia.ai/stt/websocket?')).toBe(true);
    expect(url).toContain('model=ink-whisper');
    expect(url).toContain('sample_rate=16000');
    expect(url).toContain('encoding=pcm_s16le');
    expect(url).toContain('cartesia_version=2025-04-16');
    expect(url).toContain('api_key=k');
    expect(url).toContain('language=en');
    stt.close();
  });

  it('rewrites https base_url to wss', async () => {
    const stt = new CartesiaSTT('k', { baseUrl: 'https://alt.example.com' });
    await stt.connect();
    expect(lastMock().url.startsWith('wss://alt.example.com/')).toBe(true);
    stt.close();
  });

  it('rewrites http base_url to ws', async () => {
    const stt = new CartesiaSTT('k', { baseUrl: 'http://localhost:8080' });
    await stt.connect();
    expect(lastMock().url.startsWith('ws://localhost:8080/')).toBe(true);
    stt.close();
  });
});

describe('CartesiaSTT — event handling', () => {
  it('emits final transcript on is_final=true', async () => {
    const stt = new CartesiaSTT('k');
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    lastMock().pushMessage({
      type: 'transcript',
      text: 'hello',
      is_final: true,
      probability: 0.92,
      request_id: 'req-1',
    });
    expect(collected).toHaveLength(1);
    expect(collected[0]).toEqual({ text: 'hello', isFinal: true, confidence: 0.92 });
    expect(stt.requestId).toBe('req-1');
    stt.close();
  });

  it('emits interim transcript on is_final=false', async () => {
    const stt = new CartesiaSTT('k');
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    lastMock().pushMessage({
      type: 'transcript',
      text: 'he',
      is_final: false,
      probability: 0.5,
    });
    expect(collected).toHaveLength(1);
    expect(collected[0].isFinal).toBe(false);
    stt.close();
  });

  it('ignores empty-text non-final transcripts', async () => {
    const stt = new CartesiaSTT('k');
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    lastMock().pushMessage({ type: 'transcript', text: '', is_final: false });
    expect(collected).toHaveLength(0);
    stt.close();
  });

  it('handles error events without crashing', async () => {
    const stt = new CartesiaSTT('k');
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    lastMock().pushMessage({ type: 'error', message: 'boom' });
    expect(collected).toHaveLength(0);
    stt.close();
  });

  it('ignores flush_done and done events', async () => {
    const stt = new CartesiaSTT('k');
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    lastMock().pushMessage({ type: 'flush_done' });
    lastMock().pushMessage({ type: 'done' });
    expect(collected).toHaveLength(0);
    stt.close();
  });
});

describe('CartesiaSTT — audio and close', () => {
  it('sendAudio forwards buffer', async () => {
    const stt = new CartesiaSTT('k');
    await stt.connect();
    stt.sendAudio(Buffer.from([0, 1, 2]));
    expect(lastMock().sent).toHaveLength(1);
    stt.close();
  });

  it('close sends finalize message', async () => {
    const stt = new CartesiaSTT('k');
    await stt.connect();
    stt.close();
    expect(lastMock().sent).toContain('finalize');
  });

  it('sendAudio before connect is a no-op', () => {
    const stt = new CartesiaSTT('k');
    expect(() => stt.sendAudio(Buffer.from([0]))).not.toThrow();
  });
});

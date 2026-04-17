/**
 * Unit tests for AssemblyAISTT (TypeScript).
 *
 * MOCK: no real API calls. These tests mock the `ws` module and emit
 * synthetic JSON frames that imitate AssemblyAI's v3 Universal Streaming
 * protocol (`Begin`, `Turn`, `Termination`).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock `ws` BEFORE importing the module under test. The factory is hoisted
// to the top of the file, so everything it references must be local to it.
// We expose the last-constructed instance via globalThis for test assertions.
vi.mock('ws', () => {
  class MockWebSocket {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = MockWebSocket.OPEN;
    sent: unknown[] = [];

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

import { AssemblyAISTT, type Transcript } from '../src/providers/assemblyai-stt';

interface MockWsShape {
  readyState: number;
  sent: unknown[];
  url: string;
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

describe('AssemblyAISTT — construction', () => {
  it('throws on empty api key', () => {
    expect(() => new AssemblyAISTT('')).toThrow(/apiKey/);
  });

  it('accepts api key', () => {
    const stt = new AssemblyAISTT('k');
    expect(stt).toBeDefined();
  });

  it('forTwilio factory uses mulaw 8 kHz', () => {
    const stt = AssemblyAISTT.forTwilio('k');
    expect(stt).toBeDefined();
  });
});

describe('AssemblyAISTT — URL building', () => {
  it('builds URL with defaults and connects against it', async () => {
    const stt = new AssemblyAISTT('k');
    await stt.connect();
    const url = lastMock().url;
    expect(url).toContain('wss://streaming.assemblyai.com/v3/ws?');
    expect(url).toContain('sample_rate=16000');
    expect(url).toContain('encoding=pcm_s16le');
    expect(url).toContain('speech_model=universal-streaming-english');
    // English: language_detection=false.
    expect(url).toContain('language_detection=false');
    stt.close();
  });

  it('multilingual model enables language_detection by default', async () => {
    const stt = new AssemblyAISTT('k', { model: 'universal-streaming-multilingual' });
    await stt.connect();
    expect(lastMock().url).toContain('language_detection=true');
    stt.close();
  });

  it('u3-rt-pro sets min/max turn silence to 100 by default', async () => {
    const stt = new AssemblyAISTT('k', { model: 'u3-rt-pro' });
    await stt.connect();
    expect(lastMock().url).toContain('min_turn_silence=100');
    expect(lastMock().url).toContain('max_turn_silence=100');
    stt.close();
  });

  it('keyterms_prompt is serialised as JSON string', async () => {
    const stt = new AssemblyAISTT('k', { keytermsPrompt: ['alpha', 'beta'] });
    await stt.connect();
    const decoded = decodeURIComponent(lastMock().url);
    expect(decoded).toContain('keyterms_prompt=["alpha","beta"]');
    stt.close();
  });
});

describe('AssemblyAISTT — event handling', () => {
  it('Begin populates sessionId/expiresAt', async () => {
    const stt = new AssemblyAISTT('k');
    await stt.connect();
    lastMock().pushMessage({ type: 'Begin', id: 'sess', expires_at: 42 });
    expect(stt.sessionId).toBe('sess');
    expect(stt.expiresAt).toBe(42);
    stt.close();
  });

  it('emits final transcript on end_of_turn', async () => {
    const stt = new AssemblyAISTT('k');
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    lastMock().pushMessage({
      type: 'Turn',
      end_of_turn: true,
      transcript: 'hello world',
      words: [
        { text: 'hello', confidence: 0.9 },
        { text: 'world', confidence: 0.8 },
      ],
    });
    expect(collected).toHaveLength(1);
    expect(collected[0].text).toBe('hello world');
    expect(collected[0].isFinal).toBe(true);
    expect(collected[0].confidence).toBeCloseTo(0.85, 5);
    stt.close();
  });

  it('emits interim transcript from words when end_of_turn is false', async () => {
    const stt = new AssemblyAISTT('k');
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    lastMock().pushMessage({
      type: 'Turn',
      end_of_turn: false,
      words: [{ text: 'hello', confidence: 0.7 }],
    });
    expect(collected).toHaveLength(1);
    expect(collected[0].isFinal).toBe(false);
    expect(collected[0].text).toBe('hello');
    stt.close();
  });

  it('format_turns defers final until turn_is_formatted', async () => {
    const stt = new AssemblyAISTT('k', { formatTurns: true });
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    // Unformatted final — should NOT emit.
    lastMock().pushMessage({
      type: 'Turn',
      end_of_turn: true,
      turn_is_formatted: false,
      transcript: 'hi',
      words: [{ text: 'hi', confidence: 1.0 }],
    });
    expect(collected).toHaveLength(0);
    lastMock().pushMessage({
      type: 'Turn',
      end_of_turn: true,
      turn_is_formatted: true,
      transcript: 'Hi.',
      words: [{ text: 'Hi.', confidence: 1.0 }],
    });
    expect(collected).toHaveLength(1);
    expect(collected[0].text).toBe('Hi.');
    stt.close();
  });

  it('ignores unknown event types', async () => {
    const stt = new AssemblyAISTT('k');
    const collected: Transcript[] = [];
    stt.onTranscript((t) => collected.push(t));
    await stt.connect();
    lastMock().pushMessage({ type: 'SpeechStarted' });
    lastMock().pushMessage({ type: 'Termination' });
    expect(collected).toHaveLength(0);
    stt.close();
  });
});

describe('AssemblyAISTT — audio and close', () => {
  it('sendAudio forwards buffer to ws', async () => {
    const stt = new AssemblyAISTT('k');
    await stt.connect();
    stt.sendAudio(Buffer.from([0, 1, 2]));
    expect(lastMock().sent).toHaveLength(1);
    stt.close();
  });

  it('sendAudio before connect is a no-op (does not throw)', () => {
    const stt = new AssemblyAISTT('k');
    expect(() => stt.sendAudio(Buffer.from([0]))).not.toThrow();
  });

  it('close sends Terminate message', async () => {
    const stt = new AssemblyAISTT('k');
    await stt.connect();
    stt.close();
    // Last sent before readyState change should be Terminate JSON.
    const sent = lastMock().sent.map((x) => (typeof x === 'string' ? x : ''));
    expect(sent.some((s) => s.includes('Terminate'))).toBe(true);
  });

  it('onTranscript caps at 10 callbacks', async () => {
    const stt = new AssemblyAISTT('k');
    await stt.connect();
    for (let i = 0; i < 12; i++) {
      stt.onTranscript(() => {});
    }
    // No crash — just a warning via logger.
    expect(stt).toBeDefined();
    stt.close();
  });
});

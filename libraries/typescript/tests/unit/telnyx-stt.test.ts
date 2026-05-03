/**
 * Tests for TelnyxSTT — connect lifecycle, WAV header prepending, message
 * dispatching, sendAudio, onTranscript callbacks, close.
 *
 * MOCK: the `ws` module is mocked so no real Telnyx WebSocket is opened.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TelnyxSTT } from '../../src/providers/telnyx-stt';
import type { Transcript } from '../../src/providers/telnyx-stt';

const mockWsInstances: Array<{
  handlers: Map<string, Array<(...args: unknown[]) => void>>;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  readyState: number;
  once: (event: string, cb: (...args: unknown[]) => void) => void;
}> = [];

vi.mock('ws', () => {
  const OPEN = 1;
  class MockWebSocket {
    static OPEN = OPEN;
    readyState = OPEN;
    handlers = new Map<string, Array<(...args: unknown[]) => void>>();
    send = vi.fn();
    close = vi.fn();

    constructor(_url: string, _opts?: unknown) {
      mockWsInstances.push(this as unknown as (typeof mockWsInstances)[number]);
    }

    on(event: string, cb: (...args: unknown[]) => void) {
      if (!this.handlers.has(event)) this.handlers.set(event, []);
      this.handlers.get(event)!.push(cb);
    }

    once(event: string, cb: (...args: unknown[]) => void) {
      this.on(event, cb);
    }

    emit(event: string, ...args: unknown[]) {
      const cbs = this.handlers.get(event) ?? [];
      for (const cb of cbs) {
        cb(...args);
      }
    }
  }

  return { default: MockWebSocket, __esModule: true };
});

function latestWs() {
  return mockWsInstances[mockWsInstances.length - 1];
}

describe('TelnyxSTT', () => {
  beforeEach(() => {
    mockWsInstances.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('opens WebSocket on connect and resolves on open', async () => {
    const stt = new TelnyxSTT('KEY-test');
    const connectPromise = stt.connect();
    latestWs().emit('open');
    await connectPromise;
    expect(mockWsInstances.length).toBe(1);
  });

  it('sends a WAV header before the first audio chunk', async () => {
    const stt = new TelnyxSTT('KEY-test');
    const p = stt.connect();
    latestWs().emit('open');
    await p;

    const pcm = Buffer.from([1, 2, 3, 4]);
    stt.sendAudio(pcm);
    expect(latestWs().send).toHaveBeenCalledTimes(2);

    // First call: WAV header (44 bytes, RIFF-prefixed)
    const header = latestWs().send.mock.calls[0][0] as Buffer;
    expect(header.length).toBe(44);
    expect(header.subarray(0, 4).toString()).toBe('RIFF');
    expect(header.subarray(8, 12).toString()).toBe('WAVE');

    // Second call: the actual audio
    const sent = latestWs().send.mock.calls[1][0] as Buffer;
    expect(sent.equals(pcm)).toBe(true);
  });

  it('WAV header is only sent once, even across multiple audio chunks', async () => {
    const stt = new TelnyxSTT('KEY-test');
    const p = stt.connect();
    latestWs().emit('open');
    await p;

    stt.sendAudio(Buffer.from([1]));
    stt.sendAudio(Buffer.from([2]));
    stt.sendAudio(Buffer.from([3]));

    // 1 header + 3 chunks
    expect(latestWs().send).toHaveBeenCalledTimes(4);
  });

  it('dispatches transcripts with is_final flag', async () => {
    const stt = new TelnyxSTT('KEY-test');
    const p = stt.connect();
    latestWs().emit('open');
    await p;

    const received: Transcript[] = [];
    stt.onTranscript((t) => received.push(t));

    latestWs().emit('message', Buffer.from(JSON.stringify({
      transcript: 'hello world',
      is_final: true,
      confidence: 0.85,
    })));

    expect(received).toHaveLength(1);
    expect(received[0].text).toBe('hello world');
    expect(received[0].isFinal).toBe(true);
    expect(received[0].confidence).toBe(0.85);
  });

  it('drops messages with empty transcript', async () => {
    const stt = new TelnyxSTT('KEY-test');
    const p = stt.connect();
    latestWs().emit('open');
    await p;

    const received: Transcript[] = [];
    stt.onTranscript((t) => received.push(t));

    latestWs().emit('message', Buffer.from(JSON.stringify({ transcript: '' })));
    latestWs().emit('message', Buffer.from('not-json'));

    expect(received).toHaveLength(0);
  });

  it('close() tears down the WebSocket', async () => {
    const stt = new TelnyxSTT('KEY-test');
    const p = stt.connect();
    latestWs().emit('open');
    await p;

    const ws = latestWs();
    stt.close();
    expect(ws.close).toHaveBeenCalled();
  });

  it('sendAudio is a no-op when not connected', () => {
    const stt = new TelnyxSTT('KEY-test');
    // No connect() — should not throw.
    expect(() => stt.sendAudio(Buffer.from([1]))).not.toThrow();
  });
});

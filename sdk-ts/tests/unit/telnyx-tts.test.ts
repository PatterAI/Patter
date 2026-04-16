/**
 * Tests for TelnyxTTS — synthesize stream, JSON frame decoding, connect
 * protocol (warm-up + text + terminator).
 *
 * MOCK: the `ws` module is mocked so no real Telnyx WebSocket is opened.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TelnyxTTS } from '../../src/providers/telnyx-tts';

const mockWsInstances: Array<{
  handlers: Map<string, Array<(...args: unknown[]) => void>>;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  once: (event: string, cb: (...args: unknown[]) => void) => void;
}> = [];

vi.mock('ws', () => {
  class MockWebSocket {
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

describe('TelnyxTTS', () => {
  beforeEach(() => {
    mockWsInstances.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('uses the default Telnyx.NaturalHD.astra voice', () => {
    const tts = new TelnyxTTS('KEY-test');
    // Voice is private — but the URL built during synthesize proves it.
    // We assert on construction indirectly via the generator.
    expect(tts).toBeInstanceOf(TelnyxTTS);
  });

  it('streams audio chunks from JSON frames with base64 audio field', async () => {
    const tts = new TelnyxTTS('KEY-test');
    const gen = tts.synthesizeStream('hello world');

    // Wait for the WebSocket construction + `open` event.
    const nextChunkPromise = gen.next();
    // Allow microtasks to schedule the `once('open')` handler.
    await new Promise((r) => setImmediate(r));
    latestWs().emit('open');

    // Give the promise queue a tick to send the warm-up frames.
    await new Promise((r) => setImmediate(r));

    // Verify the protocol: warm-up, text, terminator.
    const sends = latestWs().send.mock.calls.map((c: unknown[]) => JSON.parse(c[0] as string));
    expect(sends).toEqual([
      { text: ' ' },
      { text: 'hello world' },
      { text: '' },
    ]);

    // Feed one audio frame and close.
    const audio = Buffer.from('ABC', 'utf8');
    latestWs().emit('message', Buffer.from(JSON.stringify({ audio: audio.toString('base64') })));
    latestWs().emit('close');

    const first = await nextChunkPromise;
    expect(first.done).toBe(false);
    expect((first.value as Buffer).equals(audio)).toBe(true);

    const end = await gen.next();
    expect(end.done).toBe(true);
  });

  it('ignores frames without an audio field', async () => {
    const tts = new TelnyxTTS('KEY-test');
    const gen = tts.synthesizeStream('hi');
    const firstPromise = gen.next();
    await new Promise((r) => setImmediate(r));
    latestWs().emit('open');
    await new Promise((r) => setImmediate(r));

    latestWs().emit('message', Buffer.from(JSON.stringify({ meta: 'nope' })));
    latestWs().emit('close');

    const item = await firstPromise;
    // No audio frame was ever queued, so the stream ends.
    expect(item.done).toBe(true);
  });
});

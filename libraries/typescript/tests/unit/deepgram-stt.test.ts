/**
 * Deep tests for DeepgramSTT — connect lifecycle, message dispatching,
 * sendAudio, onTranscript callbacks, close, and forTwilio factory.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { DeepgramSTT } from '../../src/providers/deepgram-stt';
import type { Transcript } from '../../src/providers/deepgram-stt';

// ---------------------------------------------------------------------------
// Mock the ws module
// ---------------------------------------------------------------------------

const mockWsInstances: Array<{
  handlers: Map<string, Array<(...args: unknown[]) => void>>;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  readyState: number;
  once: (event: string, cb: (...args: unknown[]) => void) => void;
}> = [];

vi.mock('ws', () => {
  const OPEN = 1;
  const CLOSED = 3;

  class MockWebSocket {
    static OPEN = OPEN;
    static CLOSED = CLOSED;
    readyState = OPEN;
    handlers = new Map<string, Array<(...args: unknown[]) => void>>();
    send = vi.fn();
    close = vi.fn();

    constructor(_url: string, _opts?: unknown) {
      mockWsInstances.push(this as unknown as (typeof mockWsInstances)[number]);
    }

    on(event: string, cb: (...args: unknown[]) => void) {
      if (!this.handlers.has(event)) {
        this.handlers.set(event, []);
      }
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

describe('DeepgramSTT (deep)', () => {
  beforeEach(() => {
    mockWsInstances.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --- Factory ---

  describe('forTwilio()', () => {
    it('creates instance configured for mulaw 8kHz', () => {
      const stt = DeepgramSTT.forTwilio('dg-key-123', 'en', 'nova-3');
      expect(stt).toBeInstanceOf(DeepgramSTT);
    });

    it('uses default language and model', () => {
      const stt = DeepgramSTT.forTwilio('dg-key-123');
      expect(stt).toBeInstanceOf(DeepgramSTT);
    });
  });

  // --- connect() ---

  describe('connect()', () => {
    it('resolves on WebSocket open event', async () => {
      const stt = new DeepgramSTT('dg-key-123', 'en', 'nova-3', 'linear16', 16000);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');

      await expect(connectPromise).resolves.toBeUndefined();
    });

    it('rejects on WebSocket error during connect', async () => {
      const stt = new DeepgramSTT('dg-key-123');

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('error', new Error('Connection refused'));

      await expect(connectPromise).rejects.toThrow('Connection refused');
    });

    it('sets up message listener after open', async () => {
      const stt = new DeepgramSTT('dg-key-123');

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      expect(ws.handlers.get('message')).toBeDefined();
    });
  });

  // --- Message dispatching ---

  describe('message handling', () => {
    it('dispatches final transcript to callbacks', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb = vi.fn();
      stt.onTranscript(cb);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      const msg = JSON.stringify({
        type: 'Results',
        is_final: true,
        speech_final: true,
        channel: {
          alternatives: [{ transcript: 'Hello world', confidence: 0.98 }],
        },
      });
      ws.emit('message', Buffer.from(msg));

      expect(cb).toHaveBeenCalledOnce();
      const transcript: Transcript = cb.mock.calls[0][0];
      expect(transcript.text).toBe('Hello world');
      expect(transcript.isFinal).toBe(true);
      expect(transcript.confidence).toBe(0.98);
    });

    it('dispatches interim transcript (not final)', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb = vi.fn();
      stt.onTranscript(cb);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      const msg = JSON.stringify({
        type: 'Results',
        is_final: false,
        speech_final: false,
        channel: {
          alternatives: [{ transcript: 'Hello', confidence: 0.85 }],
        },
      });
      ws.emit('message', Buffer.from(msg));

      expect(cb).toHaveBeenCalledOnce();
      expect(cb.mock.calls[0][0].isFinal).toBe(false);
    });

    it('ignores messages with empty transcript text', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb = vi.fn();
      stt.onTranscript(cb);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'Results',
        is_final: true,
        speech_final: true,
        channel: { alternatives: [{ transcript: '   ', confidence: 0 }] },
      })));

      expect(cb).not.toHaveBeenCalled();
    });

    it('ignores messages with no alternatives', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb = vi.fn();
      stt.onTranscript(cb);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'Results',
        channel: { alternatives: [] },
      })));

      expect(cb).not.toHaveBeenCalled();
    });

    it('surfaces SpeechStarted as a Transcript with eventType=SpeechStarted', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb = vi.fn();
      stt.onTranscript(cb);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({ type: 'SpeechStarted' })));
      expect(cb).toHaveBeenCalledOnce();
      const transcript: Transcript = cb.mock.calls[0][0];
      expect(transcript.eventType).toBe('SpeechStarted');
      expect(transcript.text).toBe('');
      expect(transcript.isFinal).toBe(false);
    });

    it('surfaces UtteranceEnd as a Transcript with eventType=UtteranceEnd', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb = vi.fn();
      stt.onTranscript(cb);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({ type: 'UtteranceEnd' })));
      expect(cb).toHaveBeenCalledOnce();
      const transcript: Transcript = cb.mock.calls[0][0];
      expect(transcript.eventType).toBe('UtteranceEnd');
      expect(transcript.isFinal).toBe(true);
    });

    it('captures requestId from Metadata message', async () => {
      const stt = new DeepgramSTT('dg-key-123');

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'Metadata',
        request_id: 'req-abc-123',
      })));

      expect(stt.requestId).toBe('req-abc-123');
    });

    it('ignores invalid JSON messages', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb = vi.fn();
      stt.onTranscript(cb);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from('not json'));
      expect(cb).not.toHaveBeenCalled();
    });

    it('dispatches to multiple callbacks', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb1 = vi.fn();
      const cb2 = vi.fn();
      stt.onTranscript(cb1);
      stt.onTranscript(cb2);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'Results',
        is_final: true,
        speech_final: true,
        channel: { alternatives: [{ transcript: 'Hi', confidence: 0.9 }] },
      })));

      expect(cb1).toHaveBeenCalledOnce();
      expect(cb2).toHaveBeenCalledOnce();
    });
  });

  // --- onTranscript ---

  describe('onTranscript()', () => {
    it('registers more than 10 callbacks without dropping any (no 10-cap)', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const callbacks = Array.from({ length: 15 }, () => vi.fn());
      for (const cb of callbacks) {
        stt.onTranscript(cb);
      }

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'Results',
        is_final: true,
        speech_final: true,
        channel: { alternatives: [{ transcript: 'hi', confidence: 1 }] },
      })));

      for (const cb of callbacks) {
        expect(cb).toHaveBeenCalledOnce();
      }
    });

    it('offTranscript unsubscribes a previously registered callback', async () => {
      const stt = new DeepgramSTT('dg-key-123');
      const cb = vi.fn();
      stt.onTranscript(cb);
      stt.offTranscript(cb);

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'Results',
        is_final: true,
        speech_final: true,
        channel: { alternatives: [{ transcript: 'hi', confidence: 1 }] },
      })));

      expect(cb).not.toHaveBeenCalled();
    });
  });

  // --- sendAudio ---

  describe('sendAudio()', () => {
    it('sends audio buffer via WebSocket when connected', async () => {
      const stt = new DeepgramSTT('dg-key-123');

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      const audio = Buffer.alloc(320, 0);
      stt.sendAudio(audio);

      expect(ws.send).toHaveBeenCalledWith(audio);
    });

    it('silently skips when not connected', () => {
      const stt = new DeepgramSTT('dg-key-123');
      expect(() => stt.sendAudio(Buffer.from('test'))).not.toThrow();
    });

    it('silently skips when WebSocket is not OPEN', async () => {
      const stt = new DeepgramSTT('dg-key-123');

      const connectPromise = stt.connect();
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.readyState = 3; // CLOSED
      stt.sendAudio(Buffer.from('test'));
      expect(ws.send).not.toHaveBeenCalled();
    });
  });

  // --- close ---

  describe('close()', () => {
    it('sends Finalize immediately and delays CloseStream by ~100ms', async () => {
      vi.useFakeTimers();
      try {
        const stt = new DeepgramSTT('dg-key-123');

        const connectPromise = stt.connect();
        const ws = latestWs();
        ws.emit('open');
        await connectPromise;

        ws.send.mockClear();
        stt.close();

        // Finalize should be sent synchronously; CloseStream must wait.
        expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'Finalize' }));
        expect(ws.send).not.toHaveBeenCalledWith(JSON.stringify({ type: 'CloseStream' }));
        expect(ws.close).not.toHaveBeenCalled();

        // Advance past the drain window (FINALIZE_DRAIN_MS = 100).
        vi.advanceTimersByTime(100);

        expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: 'CloseStream' }));
        expect(ws.close).toHaveBeenCalledOnce();
      } finally {
        vi.useRealTimers();
      }
    });

    it('does not throw when not connected', () => {
      const stt = new DeepgramSTT('dg-key-123');
      expect(() => stt.close()).not.toThrow();
    });

    it('handles send error during close gracefully', async () => {
      vi.useFakeTimers();
      try {
        const stt = new DeepgramSTT('dg-key-123');

        const connectPromise = stt.connect();
        const ws = latestWs();
        ws.emit('open');
        await connectPromise;

        ws.send.mockImplementation(() => { throw new Error('Socket already closed'); });
        expect(() => stt.close()).not.toThrow();
        // CloseStream + ws.close still fire after the drain timer.
        vi.advanceTimersByTime(100);
        expect(ws.close).toHaveBeenCalledOnce();
      } finally {
        vi.useRealTimers();
      }
    });
  });
});

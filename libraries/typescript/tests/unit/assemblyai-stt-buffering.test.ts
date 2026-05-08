/**
 * Unit tests for AssemblyAI STT audio-frame coalescing.
 *
 * Verifies that the SDK batches small Twilio frames (20 ms / 160 bytes
 * mulaw 8 kHz) into 50–1000 ms ws frames before forwarding, so
 * AssemblyAI's v3 streaming endpoint does not emit error 3007 ("audio
 * chunk below minimum").
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  AssemblyAISTT,
  AssemblyAIEncoding,
  AssemblyAISampleRate,
} from '../../src/providers/assemblyai-stt';

// ---------------------------------------------------------------------------
// Mock the ws module — same pattern as deepgram-stt.test.ts
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

async function connectedTwilioStt(): Promise<{
  stt: AssemblyAISTT;
  ws: ReturnType<typeof latestWs>;
}> {
  const stt = AssemblyAISTT.forTwilio('aai-key-test');
  const connectPromise = stt.connect();
  const ws = latestWs();
  ws.emit('open');
  await connectPromise;
  return { stt, ws };
}

describe('[unit] AssemblyAISTT — audio frame coalescing', () => {
  beforeEach(() => {
    mockWsInstances.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('coalesces 10 Twilio 20ms frames into ~3 ws sends (not 10)', async () => {
    const { stt, ws } = await connectedTwilioStt();
    const twilioFrame = Buffer.alloc(160, 0xff); // mulaw 8 kHz / 20 ms

    for (let i = 0; i < 10; i++) {
      stt.sendAudio(twilioFrame);
    }

    // 10 × 20 ms = 200 ms → at 60 ms target → 3 flushed sends + 40 ms buffered
    const binarySends = ws.send.mock.calls.filter(
      (call) => call[0] instanceof Buffer || ArrayBuffer.isView(call[0]),
    );
    expect(binarySends.length).toBe(3);
    for (const call of binarySends) {
      const payload = call[0] as Buffer;
      // 50 ms floor for mulaw 8 kHz = 400 bytes; ceiling 1000 ms = 8000 bytes.
      expect(payload.length).toBeGreaterThanOrEqual(400);
      expect(payload.length).toBeLessThanOrEqual(8000);
    }
  });

  it('does not flush when the buffer is below target', async () => {
    const { stt, ws } = await connectedTwilioStt();
    // 2 frames = 40 ms = 320 bytes < 480 byte threshold.
    stt.sendAudio(Buffer.alloc(160, 0xff));
    stt.sendAudio(Buffer.alloc(160, 0xff));

    const binarySends = ws.send.mock.calls.filter(
      (call) => call[0] instanceof Buffer || ArrayBuffer.isView(call[0]),
    );
    expect(binarySends.length).toBe(0);
  });

  it('silently drops audio before the WebSocket is open (no throw)', () => {
    // Construct without calling connect(): ws is null.
    const stt = AssemblyAISTT.forTwilio('aai-key-test');
    expect(() => stt.sendAudio(Buffer.alloc(160, 0xff))).not.toThrow();
  });

  it('uses 60ms default target (~480 bytes for mulaw 8kHz)', async () => {
    const { stt, ws } = await connectedTwilioStt();
    // Send exactly 480 bytes worth → must produce exactly 1 ws.send.
    stt.sendAudio(Buffer.alloc(480, 0xff));
    const binarySends = ws.send.mock.calls.filter(
      (call) => call[0] instanceof Buffer || ArrayBuffer.isView(call[0]),
    );
    expect(binarySends.length).toBe(1);
    expect((binarySends[0][0] as Buffer).length).toBe(480);
  });

  it('uses ~1920 bytes target for PCM s16le 16kHz', async () => {
    const stt = new AssemblyAISTT('aai-key-test', {
      encoding: AssemblyAIEncoding.PCM_S16LE,
      sampleRate: AssemblyAISampleRate.HZ_16000,
    });
    const connectPromise = stt.connect();
    const ws = latestWs();
    ws.emit('open');
    await connectPromise;

    // 1920 bytes = 60 ms at PCM s16le 16 kHz.
    stt.sendAudio(Buffer.alloc(1920, 0));
    const binarySends = ws.send.mock.calls.filter(
      (call) => call[0] instanceof Buffer || ArrayBuffer.isView(call[0]),
    );
    expect(binarySends.length).toBe(1);
    expect((binarySends[0][0] as Buffer).length).toBe(1920);
  });
});

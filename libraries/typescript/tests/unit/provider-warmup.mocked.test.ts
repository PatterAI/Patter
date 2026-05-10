/**
 * Unit tests for the concrete provider WebSocket / HTTP warmup overrides.
 *
 * Covers the per-provider `warmup()` overrides on top of the no-op default
 * declared on `STTAdapter` / `TTSAdapter`. Each test checks two invariants:
 *
 *   1. `warmup()` completes without throwing (best-effort contract).
 *   2. When a provider opens a connection, it does NOT request any
 *      synthesis or send any audio frames — billing-during-warmup must
 *      remain zero per the per-provider docstrings.
 *
 * Tests use authentic real code paths — only the network boundary is
 * mocked. See `.claude/rules/authentic-tests.md`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ----------------------------------------------------------------------
// Shared FakeWebSocket — mirrors the pattern used by other ws-mocking
// tests in the suite. Defined inside `vi.mock` so vitest's hoisting
// doesn't pull in an outer import.
// ----------------------------------------------------------------------
vi.mock('ws', () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { EventEmitter } = require('events');
  class FakeWebSocket extends EventEmitter {
    static OPEN = 1;
    static CONNECTING = 0;
    static CLOSED = 3;
    readyState: number = FakeWebSocket.CONNECTING;
    sent: unknown[] = [];
    closeCalled = 0;
    /**
     * Pre-canned recv responses. Each `send()` call (or each open) pops one
     * off. Tests that need the fake to talk back set this before the
     * fake is constructed via FakeWebSocket.nextResponses.
     */
    static nextResponses: string[] = [];
    constructor(public url: string, public opts?: unknown) {
      super();
      (FakeWebSocket as unknown as { instances: FakeWebSocket[] }).instances.push(this);
      const queued = FakeWebSocket.nextResponses.slice();
      FakeWebSocket.nextResponses = [];
      setImmediate(() => {
        this.readyState = FakeWebSocket.OPEN;
        this.emit('open');
        // Emit each queued message on its OWN macrotask so consumer code
        // that attaches a fresh `ws.on('message', ...)` between frames
        // (typical of multi-step setup paths) still observes every frame.
        const drainOne = (idx: number): void => {
          if (idx >= queued.length) return;
          setImmediate(() => {
            this.emit('message', Buffer.from(queued[idx]));
            drainOne(idx + 1);
          });
        };
        drainOne(0);
      });
    }
    send(data: unknown): void {
      this.sent.push(data);
    }
    close(): void {
      this.closeCalled += 1;
      this.readyState = FakeWebSocket.CLOSED;
      this.emit('close', 1000, Buffer.from(''));
    }
    off(event: string, fn: (...args: unknown[]) => void): this {
      this.removeListener(event, fn);
      return this;
    }
  }
  (FakeWebSocket as unknown as { instances: FakeWebSocket[] }).instances = [];
  return { default: FakeWebSocket };
});

// Imports — must come AFTER the vi.mock above.
import { DeepgramSTT } from '../../src/providers/deepgram-stt';
import { CartesiaSTT } from '../../src/providers/cartesia-stt';
import { AssemblyAISTT } from '../../src/providers/assemblyai-stt';
import { ElevenLabsWebSocketTTS } from '../../src/providers/elevenlabs-ws-tts';
import { CartesiaTTS } from '../../src/providers/cartesia-tts';
import { InworldTTS } from '../../src/providers/inworld-tts';
import { OpenAIRealtimeAdapter } from '../../src/providers/openai-realtime';
import { setLogger, getLogger, type Logger } from '../../src/logger';
import WebSocketDefault from 'ws';

interface FakeWSInstance {
  url: string;
  sent: unknown[];
  closeCalled: number;
  readyState: number;
  emit: (event: string, ...args: unknown[]) => void;
  removeAllListeners: () => void;
}

interface FakeWSStatic {
  instances: FakeWSInstance[];
  nextResponses: string[];
  OPEN: number;
}

const FakeWS = WebSocketDefault as unknown as FakeWSStatic;

beforeEach(() => {
  FakeWS.instances.length = 0;
  FakeWS.nextResponses = [];
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ----------------------------------------------------------------------
// Deepgram STT WS warmup
// ----------------------------------------------------------------------

describe('[mocked] DeepgramSTT.warmup', () => {
  it('opens the WS, idles, closes — no audio frames sent', async () => {
    const stt = new DeepgramSTT('dg-key');
    await stt.warmup();
    expect(FakeWS.instances).toHaveLength(1);
    const ws = FakeWS.instances[0];
    expect(ws.closeCalled).toBeGreaterThan(0);
    // No audio (Buffer) frames sent during warmup.
    for (const sent of ws.sent) {
      expect(Buffer.isBuffer(sent)).toBe(false);
    }
  });

  it('targets the Deepgram listen endpoint', async () => {
    const stt = new DeepgramSTT('dg-key');
    await stt.warmup();
    expect(FakeWS.instances[0].url).toContain('api.deepgram.com/v1/listen');
  });

  it('swallows connect errors (does not throw)', async () => {
    // Force the first emitted event to be `error` instead of `open`.
    const FakeWSCtor = WebSocketDefault as unknown as new (...a: unknown[]) => FakeWSInstance & {
      emit: (event: string, ...args: unknown[]) => void;
    };
    const origDescriptor = Object.getOwnPropertyDescriptor(FakeWSCtor, 'OPEN');
    void origDescriptor;
    const stt = new DeepgramSTT('dg-key');
    // Patch fetch path doesn't apply — this provider goes through ws.
    // Instead, set the next constructed FakeWS to immediately emit error.
    // Easiest: monkey-patch the static so the next instance fires `error`
    // instead of `open`.
    const origInstancesPush = FakeWS.instances.push.bind(FakeWS.instances);
    FakeWS.instances.push = ((inst: FakeWSInstance) => {
      const r = origInstancesPush(inst);
      setImmediate(() => inst.emit('error', new Error('DNS down')));
      return r;
    }) as typeof FakeWS.instances.push;
    try {
      // Must not throw.
      await stt.warmup();
    } finally {
      FakeWS.instances.push = origInstancesPush;
    }
  });
});

// ----------------------------------------------------------------------
// Cartesia STT WS warmup
// ----------------------------------------------------------------------

describe('[mocked] CartesiaSTT.warmup', () => {
  it('opens the WS, idles, closes — no audio frames sent', async () => {
    const stt = new CartesiaSTT('cart-key');
    await stt.warmup();
    expect(FakeWS.instances).toHaveLength(1);
    const ws = FakeWS.instances[0];
    expect(ws.closeCalled).toBeGreaterThan(0);
    for (const sent of ws.sent) {
      expect(Buffer.isBuffer(sent)).toBe(false);
    }
  });

  it('targets the Cartesia STT websocket endpoint', async () => {
    const stt = new CartesiaSTT('cart-key');
    await stt.warmup();
    expect(FakeWS.instances[0].url).toContain('/stt/websocket');
  });

  it('handshake error does not leak the API key into logs (regression)', async () => {
    // Cartesia auth uses ?api_key=... in the URL. A 401/403 from the
    // server during the WS upgrade surfaces via the `error` event with
    // a message that may include the URL. The warmup catch handler
    // must extract only the HTTP status, never the full message.
    const secretKey = 'ck_secret_THIS_MUST_NEVER_LEAK';

    const captured: { level: string; message: string }[] = [];
    const originalLogger = getLogger();
    const captureLogger: Logger = {
      info: (msg) => captured.push({ level: 'info', message: msg }),
      warn: (msg) => captured.push({ level: 'warn', message: msg }),
      error: (msg) => captured.push({ level: 'error', message: msg }),
      debug: (msg) => captured.push({ level: 'debug', message: msg }),
    };
    setLogger(captureLogger);

    // Force the next FakeWS instance to fire `error` with a payload
    // shaped like a `ws` handshake failure — `statusCode` set to 401
    // and a message that includes the secret URL (matching the real
    // `ws` behaviour pre-fix).
    const origInstancesPush = FakeWS.instances.push.bind(FakeWS.instances);
    FakeWS.instances.push = ((inst: FakeWSInstance) => {
      const r = origInstancesPush(inst);
      setImmediate(() => {
        const err = new Error(
          `Unexpected server response: 401 (url=${inst.url})`,
        ) as Error & { statusCode?: number };
        err.statusCode = 401;
        inst.emit('error', err);
      });
      return r;
    }) as typeof FakeWS.instances.push;

    try {
      const stt = new CartesiaSTT(secretKey);
      await stt.warmup(); // must not throw
    } finally {
      FakeWS.instances.push = origInstancesPush;
      setLogger(originalLogger);
    }

    // The API key must not appear in any captured log message.
    for (const log of captured) {
      expect(log.message).not.toContain(secretKey);
      expect(log.message).not.toContain('api_key=');
    }
    // We should still log SOMETHING — namely the HTTP status — so
    // operators know the warmup failed and why.
    expect(
      captured.some((l) => l.message.includes('401')),
    ).toBe(true);
  });
});

// ----------------------------------------------------------------------
// AssemblyAI STT WS warmup
// ----------------------------------------------------------------------

describe('[mocked] AssemblyAISTT.warmup', () => {
  it('opens the WS, idles, sends Terminate (no audio), closes', async () => {
    const stt = new AssemblyAISTT('aai-key');
    await stt.warmup();
    expect(FakeWS.instances).toHaveLength(1);
    const ws = FakeWS.instances[0];
    expect(ws.closeCalled).toBeGreaterThan(0);
    // No audio frames during warmup.
    for (const sent of ws.sent) {
      expect(Buffer.isBuffer(sent)).toBe(false);
    }
    // Terminate is fine — control message, not audio. If the warmup
    // sent any string frame at all, it must be a Terminate.
    for (const sent of ws.sent) {
      const parsed = JSON.parse(String(sent));
      expect(parsed.type).toBe('Terminate');
    }
  });

  it('targets the AssemblyAI v3 ws endpoint', async () => {
    const stt = new AssemblyAISTT('aai-key');
    await stt.warmup();
    expect(FakeWS.instances[0].url).toContain('/v3/ws');
  });

  it('handshake error does not leak the API key into logs (regression)', async () => {
    // AssemblyAI auth supports ?token=... in the URL when
    // useQueryToken is set. A 401/403 from the server surfaces via
    // the `error` event with a message that may include the URL.
    const secretKey = 'aai_secret_THIS_MUST_NEVER_LEAK';

    const captured: { level: string; message: string }[] = [];
    const originalLogger = getLogger();
    const captureLogger: Logger = {
      info: (msg) => captured.push({ level: 'info', message: msg }),
      warn: (msg) => captured.push({ level: 'warn', message: msg }),
      error: (msg) => captured.push({ level: 'error', message: msg }),
      debug: (msg) => captured.push({ level: 'debug', message: msg }),
    };
    setLogger(captureLogger);

    const origInstancesPush = FakeWS.instances.push.bind(FakeWS.instances);
    FakeWS.instances.push = ((inst: FakeWSInstance) => {
      const r = origInstancesPush(inst);
      setImmediate(() => {
        const err = new Error(
          `Unexpected server response: 401 (url=${inst.url})`,
        ) as Error & { statusCode?: number };
        err.statusCode = 401;
        inst.emit('error', err);
      });
      return r;
    }) as typeof FakeWS.instances.push;

    try {
      const stt = new AssemblyAISTT(secretKey, { useQueryToken: true });
      await stt.warmup(); // must not throw
    } finally {
      FakeWS.instances.push = origInstancesPush;
      setLogger(originalLogger);
    }

    for (const log of captured) {
      expect(log.message).not.toContain(secretKey);
      expect(log.message).not.toContain('token=');
    }
    expect(
      captured.some((l) => l.message.includes('401')),
    ).toBe(true);
  });
});

// ----------------------------------------------------------------------
// ElevenLabs WS TTS warmup
// ----------------------------------------------------------------------

describe('[mocked] ElevenLabsWebSocketTTS.warmup', () => {
  it('opens the WS, sends keepalive, idles, closes — no synthesis commit', async () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'el-key' });
    await tts.warmup();
    expect(FakeWS.instances).toHaveLength(1);
    const ws = FakeWS.instances[0];
    expect(ws.closeCalled).toBeGreaterThan(0);
    // Every send must be a keepalive ({"text": " "}) — no flush:true and
    // no real text (which would commit a synthesis and bill characters).
    for (const sent of ws.sent) {
      const msg = JSON.parse(String(sent));
      expect(msg.flush).not.toBe(true);
      expect(String(msg.text || '').trim()).toBe('');
    }
  });

  it('targets the ElevenLabs stream-input endpoint', async () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'el-key' });
    await tts.warmup();
    expect(FakeWS.instances[0].url).toContain('/stream-input');
  });

  it('warmup BOS bytes are byte-identical to synthesizeStream BOS bytes (regression)', async () => {
    // Configure with non-default voice_settings + auto_mode=false +
    // chunk_length_schedule so the BOS frame carries every optional field.
    const tts = new ElevenLabsWebSocketTTS({
      apiKey: 'el-key',
      voiceSettings: { stability: 0.7, similarity_boost: 0.8 },
      autoMode: false,
      chunkLengthSchedule: [120, 160, 250, 290],
    });

    // --- Capture warmup BOS ---
    await tts.warmup();
    const warmupWs = FakeWS.instances[0];
    const warmupBos = warmupWs.sent[0] as string;
    expect(typeof warmupBos).toBe('string');

    // Reset for the synthesize run.
    FakeWS.instances.length = 0;

    // --- Capture synthesize BOS ---
    // Pre-canned response: an `isFinal` frame so the generator exits fast
    // without yielding any real audio bytes.
    FakeWS.nextResponses = [JSON.stringify({ isFinal: true })];
    const gen = tts.synthesizeStream('hello');
    // Drain the generator until it exits.
    for await (const _chunk of gen) {
      void _chunk;
    }
    const synthWs = FakeWS.instances[0];
    const synthBos = synthWs.sent[0] as string;
    expect(typeof synthBos).toBe('string');

    // BOS frames must match byte-for-byte so ElevenLabs picks the same
    // per-session worker for warm and live.
    expect(Buffer.from(warmupBos, 'utf8').equals(Buffer.from(synthBos, 'utf8'))).toBe(true);

    // And specifically: must NOT include flush:true.
    const parsed = JSON.parse(warmupBos);
    expect(parsed.flush).not.toBe(true);
    expect(String(parsed.text || '').trim()).toBe('');
  });
});

// ----------------------------------------------------------------------
// Cartesia TTS HTTP warmup
// ----------------------------------------------------------------------

describe('[mocked] CartesiaTTS.warmup', () => {
  it('issues a GET against /voices, never POST /tts/bytes', async () => {
    const calls: { url: string; method: string }[] = [];
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          url: typeof input === 'string' ? input : input.toString(),
          method: init?.method ?? 'GET',
        });
        return new Response('', { status: 200 });
      },
    );
    try {
      const tts = new CartesiaTTS('ct-key');
      await tts.warmup();
    } finally {
      fetchSpy.mockRestore();
    }
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/voices');
    expect(calls[0].method).toBe('GET');
  });

  it('swallows fetch errors (does not throw)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      throw new Error('DNS down');
    });
    try {
      const tts = new CartesiaTTS('ct-key');
      // Must not throw.
      await tts.warmup();
    } finally {
      fetchSpy.mockRestore();
    }
  });
});

// ----------------------------------------------------------------------
// Inworld TTS HTTP warmup
// ----------------------------------------------------------------------

describe('[mocked] InworldTTS.warmup', () => {
  it('issues GET /tts/v1/voices — 2xx, not HEAD/POST against the POST-only streaming endpoint', async () => {
    // Earlier revisions used HEAD against the streaming endpoint,
    // which returned 405. New path uses the documented voices
    // metadata GET so the response is 2xx and no 405s are spammed.
    const calls: { url: string; method: string; status: number }[] = [];
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = init?.method ?? 'GET';
        const response = new Response('', { status: 200 });
        calls.push({ url, method, status: response.status });
        return response;
      },
    );
    try {
      const tts = new InworldTTS('inworld-token');
      await tts.warmup();
    } finally {
      fetchSpy.mockRestore();
    }
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toContain('/tts/v1/voices');
    // Must NOT target the POST-only streaming endpoint.
    expect(calls[0].url).not.toContain('voice:stream');
    // Status must be 2xx (the fake responds with 200) — no 405 spam.
    expect(calls[0].status).toBeGreaterThanOrEqual(200);
    expect(calls[0].status).toBeLessThan(300);
  });

  it('swallows fetch errors (does not throw)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      throw new Error('DNS down');
    });
    try {
      const tts = new InworldTTS('inworld-token');
      await tts.warmup();
    } finally {
      fetchSpy.mockRestore();
    }
  });
});

// ----------------------------------------------------------------------
// OpenAI Realtime warmup (session.update — billing-safe, no response.create)
// ----------------------------------------------------------------------

describe('[mocked] OpenAIRealtimeAdapter.warmup', () => {
  it('sends session.update only — never response.create — and waits for session.updated', async () => {
    // Pre-canned server frames: session.created → session.updated.
    FakeWS.nextResponses = [
      JSON.stringify({ type: 'session.created' }),
      JSON.stringify({ type: 'session.updated' }),
    ];

    const adapter = new OpenAIRealtimeAdapter(
      'sk-test',
      'gpt-realtime-mini',
      'alloy',
      'You are a test assistant.',
    );
    await adapter.warmup();

    expect(FakeWS.instances).toHaveLength(1);
    const ws = FakeWS.instances[0];
    expect(ws.closeCalled).toBeGreaterThan(0);

    const sentMessages = (ws.sent as unknown[]).map((s) => JSON.parse(String(s)));
    // Must NOT send response.create — that field is not in the OpenAI
    // Realtime schema and is billing-unsafe.
    expect(sentMessages.find((m) => m.type === 'response.create')).toBeUndefined();
    // Must NOT send audio.
    expect(sentMessages.find((m) => m.type === 'input_audio_buffer.append')).toBeUndefined();
    // Must send exactly one session.update with the production fields.
    const updates = sentMessages.filter((m) => m.type === 'session.update');
    expect(updates).toHaveLength(1);
    const session = updates[0].session;
    for (const required of [
      'input_audio_format',
      'output_audio_format',
      'voice',
      'instructions',
      'turn_detection',
      'input_audio_transcription',
    ]) {
      expect(session).toHaveProperty(required);
    }
    expect(session.voice).toBe('alloy');
    expect(session.instructions).toBe('You are a test assistant.');
  });

  it('does not send response.create on the wire (regression)', async () => {
    FakeWS.nextResponses = [
      JSON.stringify({ type: 'session.created' }),
      JSON.stringify({ type: 'session.updated' }),
    ];
    const adapter = new OpenAIRealtimeAdapter('sk-test');
    await adapter.warmup();
    const ws = FakeWS.instances[0];
    for (const raw of ws.sent as unknown[]) {
      expect(String(raw)).not.toContain('response.create');
    }
  });

  it('targets the OpenAI Realtime endpoint', async () => {
    FakeWS.nextResponses = [JSON.stringify({ type: 'session.created' })];
    const adapter = new OpenAIRealtimeAdapter('sk-test');
    await adapter.warmup();
    expect(FakeWS.instances[0].url).toContain('api.openai.com/v1/realtime');
  });
});

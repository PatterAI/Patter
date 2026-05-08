/**
 * Mocked tests for the Speechmatics STT adapter.
 *
 * Mocks the outer `ws` boundary only — every transcript-rendering /
 * config-validation / lifecycle path inside `SpeechmaticsSTT` runs the real
 * code under test. Mirrors the Python `tests/test_providers.py` Speechmatics
 * coverage (`@pytest.mark.mocked`).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Mock `ws` — only the WebSocket boundary, not the adapter under test.
// `vi.mock` is hoisted to the top of the file, so the factory cannot close
// over module-scope variables. Instead the mock stashes its state on the
// constructor itself, which the test reads via `MockSocket.instances`.
// ---------------------------------------------------------------------------
vi.mock('ws', async () => {
  const { EventEmitter } = await import('events');
  class MockSocket extends EventEmitter {
    static OPEN = 1;
    static CLOSED = 3;
    static CONNECTING = 0;
    static instances: MockSocket[] = [];
    readyState: number = MockSocket.CONNECTING;
    sent: unknown[] = [];
    url: string;
    options: Record<string, unknown>;

    constructor(url: string, options?: Record<string, unknown>) {
      super();
      this.url = url;
      this.options = options ?? {};
      MockSocket.instances.push(this);
      // Defer the open event until the next tick so callers can attach
      // listeners before it fires (matches real `ws` behaviour).
      process.nextTick(() => {
        this.readyState = MockSocket.OPEN;
        this.emit('open');
      });
    }

    send(data: unknown): void {
      this.sent.push(data);
    }

    close(): void {
      this.readyState = MockSocket.CLOSED;
      this.emit('close');
    }
  }
  return { default: MockSocket };
});

// Pull the mocked module so we can read `MockSocket.instances` from tests.
import WebSocket from 'ws';
interface MockSocketCtor {
  instances: MockSocketLike[];
  OPEN: number;
  CLOSED: number;
}
interface MockSocketLike {
  readyState: number;
  sent: unknown[];
  url: string;
  options: Record<string, unknown>;
  emit(event: string, ...args: unknown[]): boolean;
}
const MockSocket = WebSocket as unknown as MockSocketCtor;

// ---------------------------------------------------------------------------
// Import after the mock is registered so `WebSocket` resolves to MockSocket.
// ---------------------------------------------------------------------------
import {
  SpeechmaticsSTT,
  TurnDetectionMode,
  SpeechmaticsSampleRate,
} from '../src/providers/speechmatics-stt';
import { STT as SpeechmaticsSttPipeline } from '../src/stt/speechmatics';

beforeEach(() => {
  MockSocket.instances.length = 0;
});

describe('[mocked] SpeechmaticsSTT — provider adapter', () => {
  it('rejects construction without an api key', () => {
    expect(() => new SpeechmaticsSTT('')).toThrow(/apiKey is required/);
  });

  it('validates endOfUtteranceSilenceTrigger range', () => {
    expect(
      () => new SpeechmaticsSTT('sm_test', { endOfUtteranceSilenceTrigger: 5 }),
    ).toThrow(/endOfUtteranceSilenceTrigger/);
    expect(
      () => new SpeechmaticsSTT('sm_test', { endOfUtteranceSilenceTrigger: 0 }),
    ).toThrow(/endOfUtteranceSilenceTrigger/);
  });

  it('validates endOfUtteranceMaxDelay > endOfUtteranceSilenceTrigger', () => {
    expect(
      () =>
        new SpeechmaticsSTT('sm_test', {
          endOfUtteranceSilenceTrigger: 1,
          endOfUtteranceMaxDelay: 0.5,
        }),
    ).toThrow(/greater than endOfUtteranceSilenceTrigger/);
  });

  it('validates maxDelay range', () => {
    expect(() => new SpeechmaticsSTT('sm_test', { maxDelay: 0.1 })).toThrow(
      /maxDelay/,
    );
    expect(() => new SpeechmaticsSTT('sm_test', { maxDelay: 10 })).toThrow(
      /maxDelay/,
    );
  });

  it('accepts valid construction with explicit options', () => {
    const stt = new SpeechmaticsSTT('sm_test', {
      language: 'it',
      turnDetectionMode: TurnDetectionMode.SMART_TURN,
      sampleRate: SpeechmaticsSampleRate.HZ_8000,
      enableDiarization: true,
      includePartials: false,
      maxDelay: 1.5,
      endOfUtteranceSilenceTrigger: 0.5,
      endOfUtteranceMaxDelay: 1.5,
    });
    expect(stt).toBeDefined();
  });

  it('connect opens a websocket with Bearer auth + sends StartRecognition', async () => {
    const stt = new SpeechmaticsSTT('sm_secret', { language: 'es' });
    await stt.connect();

    expect(MockSocket.instances).toHaveLength(1);
    const sock = MockSocket.instances[0];
    expect(sock.url).toBe('wss://eu.rt.speechmatics.com/v2');
    expect(sock.options.headers).toEqual({ Authorization: 'Bearer sm_secret' });

    // First frame on the wire MUST be the StartRecognition payload.
    expect(sock.sent).toHaveLength(1);
    const start = JSON.parse(sock.sent[0] as string);
    expect(start.message).toBe('StartRecognition');
    expect(start.audio_format).toEqual({
      type: 'raw',
      encoding: 'pcm_s16le',
      sample_rate: 16000,
    });
    expect(start.transcription_config.language).toBe('es');
    expect(start.transcription_config.enable_partials).toBe(true);
    expect(start.transcription_config.diarization).toBe('none');
    expect(start.transcription_config.conversation_config.end_of_utterance_mode).toBe(
      'adaptive',
    );
  });

  it('honours custom baseUrl override', async () => {
    const stt = new SpeechmaticsSTT('sm_test', {
      baseUrl: 'wss://us.rt.speechmatics.com/v2',
    });
    await stt.connect();
    expect(MockSocket.instances[0].url).toBe('wss://us.rt.speechmatics.com/v2');
  });

  it('emits a non-final transcript on AddPartialTranscript', async () => {
    const stt = new SpeechmaticsSTT('sm_test');
    const seen: { text: string; isFinal: boolean; confidence: number }[] = [];
    stt.onTranscript((t) => seen.push({ text: t.text, isFinal: t.isFinal, confidence: t.confidence }));
    await stt.connect();

    MockSocket.instances[0].emit(
      'message',
      Buffer.from(
        JSON.stringify({
          message: 'AddPartialTranscript',
          metadata: { transcript: 'hello there' },
          results: [{ alternatives: [{ content: 'hello', confidence: 0.8 }] }],
        }),
      ),
    );

    expect(seen).toEqual([{ text: 'hello there', isFinal: false, confidence: 0.8 }]);
  });

  it('emits a final transcript on AddTranscript and averages confidences', async () => {
    const stt = new SpeechmaticsSTT('sm_test');
    const seen: { text: string; isFinal: boolean; confidence: number }[] = [];
    stt.onTranscript((t) => seen.push({ text: t.text, isFinal: t.isFinal, confidence: t.confidence }));
    await stt.connect();

    MockSocket.instances[0].emit(
      'message',
      Buffer.from(
        JSON.stringify({
          message: 'AddTranscript',
          metadata: { transcript: 'how are you' },
          results: [
            { alternatives: [{ content: 'how', confidence: 0.9 }] },
            { alternatives: [{ content: 'are', confidence: 0.8 }] },
            { alternatives: [{ content: 'you', confidence: 0.7 }] },
          ],
        }),
      ),
    );

    expect(seen).toHaveLength(1);
    expect(seen[0].text).toBe('how are you');
    expect(seen[0].isFinal).toBe(true);
    expect(seen[0].confidence).toBeCloseTo(0.8, 5);
  });

  it('falls back to confidence=1.0 when no per-token confidences arrive', async () => {
    const stt = new SpeechmaticsSTT('sm_test');
    const seen: { text: string; isFinal: boolean; confidence: number }[] = [];
    stt.onTranscript((t) => seen.push({ text: t.text, isFinal: t.isFinal, confidence: t.confidence }));
    await stt.connect();

    MockSocket.instances[0].emit(
      'message',
      Buffer.from(
        JSON.stringify({
          message: 'AddTranscript',
          metadata: { transcript: 'no confidences here' },
          results: [],
        }),
      ),
    );

    expect(seen).toEqual([
      { text: 'no confidences here', isFinal: true, confidence: 1.0 },
    ]);
  });

  it('drops empty transcripts so silence frames do not noise listeners', async () => {
    const stt = new SpeechmaticsSTT('sm_test');
    const seen: unknown[] = [];
    stt.onTranscript((t) => seen.push(t));
    await stt.connect();

    MockSocket.instances[0].emit(
      'message',
      Buffer.from(
        JSON.stringify({
          message: 'AddTranscript',
          metadata: { transcript: '   ' },
          results: [],
        }),
      ),
    );

    expect(seen).toHaveLength(0);
  });

  it('skips lifecycle messages (RecognitionStarted, AudioAdded, EndOfUtterance, EndOfTranscript, Info)', async () => {
    const stt = new SpeechmaticsSTT('sm_test');
    const seen: unknown[] = [];
    stt.onTranscript((t) => seen.push(t));
    await stt.connect();

    for (const event of [
      'RecognitionStarted',
      'AudioAdded',
      'EndOfUtterance',
      'EndOfTranscript',
      'Info',
    ]) {
      MockSocket.instances[0].emit('message', Buffer.from(JSON.stringify({ message: event })));
    }

    expect(seen).toHaveLength(0);
  });

  it('forwards Error frames to error listeners', async () => {
    const stt = new SpeechmaticsSTT('sm_test');
    const errors: Error[] = [];
    stt.onError((err) => errors.push(err));
    await stt.connect();

    MockSocket.instances[0].emit(
      'message',
      Buffer.from(JSON.stringify({ message: 'Error', reason: 'boom' })),
    );

    expect(errors).toHaveLength(1);
    expect(errors[0].message).toContain('boom');
  });

  it('sendAudio drops empty frames and increments the seq counter for non-empty', async () => {
    const stt = new SpeechmaticsSTT('sm_test');
    await stt.connect();
    const sock = MockSocket.instances[0];

    // Empty frame should NOT be forwarded.
    stt.sendAudio(Buffer.alloc(0));
    expect(sock.sent).toHaveLength(1); // still just StartRecognition

    // Real frame should be forwarded as a binary buffer.
    const audio = Buffer.from([1, 2, 3, 4]);
    stt.sendAudio(audio);
    expect(sock.sent).toHaveLength(2);
    expect(sock.sent[1]).toBe(audio);
  });

  it('close() sends EndOfStream with the last_seq_no and tears down the socket', async () => {
    const stt = new SpeechmaticsSTT('sm_test');
    await stt.connect();
    const sock = MockSocket.instances[0];

    stt.sendAudio(Buffer.from([1, 2, 3]));
    stt.sendAudio(Buffer.from([4, 5, 6]));
    stt.close();

    // [StartRecognition, audio1, audio2, EndOfStream]
    expect(sock.sent).toHaveLength(4);
    const eos = JSON.parse(sock.sent[3] as string);
    expect(eos).toEqual({ message: 'EndOfStream', last_seq_no: 2 });
    expect(sock.readyState).toBe(MockSocket.CLOSED);
  });

  it('close() is idempotent', () => {
    const stt = new SpeechmaticsSTT('sm_test');
    expect(() => stt.close()).not.toThrow();
    expect(() => stt.close()).not.toThrow();
  });

  it('sendAudio is a no-op before connect()', () => {
    const stt = new SpeechmaticsSTT('sm_test');
    expect(() => stt.sendAudio(Buffer.from([1, 2, 3]))).not.toThrow();
    expect(MockSocket.instances).toHaveLength(0);
  });
});

describe('[mocked] SpeechmaticsSTT — pipeline-mode wrapper', () => {
  it('exposes a stable providerKey', () => {
    expect(SpeechmaticsSttPipeline.providerKey).toBe('speechmatics');
  });

  it('accepts an explicit apiKey', () => {
    const stt = new SpeechmaticsSttPipeline({ apiKey: 'sm_explicit' });
    expect(stt).toBeDefined();
  });

  it('reads SPEECHMATICS_API_KEY from the environment', () => {
    const original = process.env.SPEECHMATICS_API_KEY;
    process.env.SPEECHMATICS_API_KEY = 'sm_env';
    try {
      const stt = new SpeechmaticsSttPipeline();
      expect(stt).toBeDefined();
    } finally {
      if (original === undefined) delete process.env.SPEECHMATICS_API_KEY;
      else process.env.SPEECHMATICS_API_KEY = original;
    }
  });

  it('throws a clear error when no key is available', () => {
    const original = process.env.SPEECHMATICS_API_KEY;
    delete process.env.SPEECHMATICS_API_KEY;
    try {
      expect(() => new SpeechmaticsSttPipeline()).toThrow(/SPEECHMATICS_API_KEY/);
    } finally {
      if (original !== undefined) process.env.SPEECHMATICS_API_KEY = original;
    }
  });
});

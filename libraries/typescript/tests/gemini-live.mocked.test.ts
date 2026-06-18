import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GeminiLiveAdapter, GEMINI_LIVE_3_1_FLASH_PREVIEW } from '../src/providers/gemini-live.js';

// [mocked] GeminiLiveAdapter — mock the @google/genai boundary only.

function makeFakeSession(overrides: Record<string, unknown> = {}) {
  const audioCaptures: unknown[] = [];
  // Mirrors the real @google/genai contract: calling session.close()
  // terminates the underlying WebSocket, which ends the receive() async
  // iterator. Without this, the fake's receive() would block forever and
  // adapter.close() (which awaits the receive loop) would hang.
  let closeSignal: () => void = () => {};
  const closed = new Promise<void>((resolve) => {
    closeSignal = resolve;
  });
  return {
    audioCaptures,
    session: {
      sendRealtimeInput: vi.fn((args: unknown) => { audioCaptures.push(args); }),
      sendClientContent: vi.fn(),
      sendToolResponse: vi.fn(),
      close: vi.fn(() => { closeSignal(); }),
      receive: async function* () {
        // Yield one no-op message immediately so pumpReceive resolves _ready
        yield {};
        // Then stay open until close() is called (simulates a live session
        // whose receive iterator only ends when the socket is torn down).
        await closed;
      },
      ...overrides,
    },
  };
}

function makeGenAIMock(session: unknown, capturedApiVersions: string[] = []) {
  return {
    GoogleGenAI: vi.fn().mockImplementation((opts: { httpOptions?: { apiVersion?: string } }) => {
      if (opts.httpOptions?.apiVersion) {
        capturedApiVersions.push(opts.httpOptions.apiVersion);
      }
      return {
        live: {
          connect: vi.fn().mockResolvedValue(session),
        },
      };
    }),
  };
}

describe('[mocked] GeminiLiveAdapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('sendAudio sends the audio: field (not deprecated media:)', async () => {
    const fake = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session));

    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
    });
    await adapter.connect();

    // 64 mulaw bytes (8 ms @ 8 kHz) — enough for the upsampler to emit.
    const mulaw = Buffer.alloc(64, 0x7f);
    adapter.sendAudio(mulaw);

    expect(fake.session.sendRealtimeInput).toHaveBeenCalledOnce();
    const arg = fake.session.sendRealtimeInput.mock.calls[0][0] as Record<string, unknown>;
    expect(arg).toHaveProperty('audio');
    expect(arg).not.toHaveProperty('media');
    expect((arg.audio as Record<string, unknown>).mimeType).toBe('audio/pcm;rate=16000');
  });

  it('sendAudio transcodes carrier mulaw-8k to PCM16 @ 16k before sending', async () => {
    const fake = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session));

    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
    });
    await adapter.connect();

    // N mulaw bytes -> N PCM16 samples @ 8 kHz -> ~2N samples @ 16 kHz
    // -> ~4N bytes of PCM16. The 8->16k upsampler defers the final sample
    // until flush, so expect 2*(N-1) samples = 4*(N-1) bytes for one chunk.
    const N = 160; // 20 ms of mulaw @ 8 kHz
    const mulaw = Buffer.alloc(N, 0x55);
    adapter.sendAudio(mulaw);

    expect(fake.session.sendRealtimeInput).toHaveBeenCalledOnce();
    const arg = fake.session.sendRealtimeInput.mock.calls[0][0] as Record<string, unknown>;
    const audio = arg.audio as { data: string; mimeType: string };
    expect(audio.mimeType).toBe('audio/pcm;rate=16000');

    const sentBytes = Buffer.from(audio.data, 'base64');
    // PCM16 @ 16 kHz is 4x the byte count of the mulaw-8k source (1 byte
    // mulaw -> 2 bytes PCM16 @ 8k -> 2x samples @ 16k -> 4 bytes), minus the
    // one sample the upsampler defers across the chunk boundary.
    expect(sentBytes.length).toBe(4 * (N - 1));
    // The bytes are NOT the raw mulaw (which would be 160 bytes): the
    // transcode must have run. This is the discriminating assertion — if the
    // mulaw->PCM->upsample transcode is removed and raw bytes are sent, the
    // length collapses to N (160) and mimeType lies, failing both checks.
    expect(sentBytes.length).not.toBe(N);
    expect(Buffer.compare(sentBytes.subarray(0, N), mulaw)).not.toBe(0);
  });

  it('receive path transcodes PCM-24k to mulaw-8k 20ms (<=160-byte) frames', async () => {
    // 480 PCM16 samples @ 24 kHz = 20 ms of audio = 960 bytes.
    // After 24k->16k->8k that becomes ~160 samples @ 8 kHz = 160 mulaw bytes
    // = exactly one 20 ms carrier frame (within resampler boundary tolerance).
    const SAMPLES_24K = 480;
    const pcm24 = Buffer.alloc(SAMPLES_24K * 2);
    for (let i = 0; i < SAMPLES_24K; i++) {
      // Low-frequency sine so the anti-alias filter has real content.
      pcm24.writeInt16LE(Math.round(8000 * Math.sin((2 * Math.PI * 200 * i) / 24000)), i * 2);
    }

    const fake = makeFakeSession({
      receive: async function* () {
        yield {}; // resolves _ready
        yield {
          serverContent: {
            modelTurn: {
              parts: [{ inlineData: { data: pcm24.toString('base64') } }],
            },
          },
        };
        // Return after delivering the part — ends the receive loop cleanly so
        // adapter.close() (which awaits the loop) does not hang.
      },
    });
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    const audioFrames: Buffer[] = [];
    adapter.onEvent((type, data) => {
      if (type === 'audio') audioFrames.push(data as Buffer);
    });
    await adapter.connect();

    // Allow the receive loop to process the queued PCM-24k part.
    await vi.waitFor(() => {
      expect(audioFrames.length).toBeGreaterThan(0);
    });

    // Every emitted frame is a mulaw 20 ms slice: at most 160 bytes.
    for (const frame of audioFrames) {
      expect(frame.length).toBeLessThanOrEqual(160);
      expect(frame.length).toBeGreaterThan(0);
    }

    // Duration is preserved: total mulaw bytes ~= 160 (20 ms @ 8 kHz mulaw),
    // within a small resampler-warmup tolerance. Critically the total is far
    // below the 960 raw PCM-24k bytes — proof the downsample+encode ran.
    // If the transcode were removed and raw PCM-24k were emitted, the total
    // would be ~960 bytes and individual frames would exceed 160, failing
    // the per-frame assertion above.
    const totalMulaw = audioFrames.reduce((sum, f) => sum + f.length, 0);
    expect(totalMulaw).toBeGreaterThan(120);
    expect(totalMulaw).toBeLessThan(200);

    await adapter.close();
  });

  it('uses v1alpha for native-audio models and no custom version for 3.1-flash-live', async () => {
    const versionsNativeAudio: string[] = [];
    const fake1 = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(fake1.session, versionsNativeAudio));

    const adapterNative = new GeminiLiveAdapter('test-key', {
      model: 'gemini-2.5-flash-native-audio-preview-09-2025',
    });
    await adapterNative.connect();
    expect(versionsNativeAudio).toContain('v1alpha');
    await adapterNative.close();

    vi.resetModules();

    const versionsNew: string[] = [];
    const fake2 = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(fake2.session, versionsNew));

    const adapterNew = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
    });
    await adapterNew.connect();
    // Should NOT pass a custom apiVersion for non-native-audio models
    expect(versionsNew).toHaveLength(0);
    await adapterNew.close();
  });

  it('explicit apiVersion option is forwarded regardless of model name', async () => {
    const versions: string[] = [];
    const fake = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session, versions));

    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
      apiVersion: 'v1beta',
    });
    await adapter.connect();
    expect(versions).toContain('v1beta');
  });

  it('connect() resolves only after the receive loop starts', async () => {
    let receiveStarted = false;
    const fake = makeFakeSession({
      receive: async function* () {
        receiveStarted = true;
        yield {};
        await new Promise(() => {});
      },
    });
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    await adapter.connect();

    expect(receiveStarted).toBe(true);
  });

  it('GEMINI_LIVE_3_1_FLASH_PREVIEW constant has expected value', () => {
    expect(GEMINI_LIVE_3_1_FLASH_PREVIEW).toBe('gemini-3.1-flash-live-preview');
  });

  it('pricing.ts includes gemini-3.1-flash-live-preview', async () => {
    // Dynamic import to avoid circular reference in the test file.
    // The real exported symbol is `llmPricing` (per-provider, per-model
    // nested rate card) — Gemini Live models live under the `google` key.
    const { llmPricing } = await import('../src/pricing.js');
    expect(llmPricing.google['gemini-3.1-flash-live-preview']).toEqual({ input: 0.30, output: 2.50 });
  });
});

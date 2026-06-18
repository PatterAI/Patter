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

    const pcm = Buffer.from([0x00, 0x01, 0x02]);
    adapter.sendAudio(pcm);

    expect(fake.session.sendRealtimeInput).toHaveBeenCalledOnce();
    const arg = fake.session.sendRealtimeInput.mock.calls[0][0] as Record<string, unknown>;
    expect(arg).toHaveProperty('audio');
    expect(arg).not.toHaveProperty('media');
    expect((arg.audio as Record<string, unknown>).mimeType).toBe('audio/pcm;rate=16000');
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

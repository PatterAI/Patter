import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GeminiLiveAdapter, GEMINI_LIVE_3_1_FLASH_PREVIEW } from '../src/providers/gemini-live.js';

// [mocked] GeminiLiveAdapter — mock the @google/genai boundary only.
//
// The real JS @google/genai SDK delivers server messages through the
// `callbacks.onmessage` handler passed to `live.connect()` and signals an open
// socket via `callbacks.onopen` — the returned Session has NO async-iterable
// `receive()` (that is the Python SDK's shape). These mocks mirror that exact
// contract: connect() captures the callbacks, fires onopen so connect()
// unblocks, and tests push server messages by invoking onmessage themselves.

interface LiveCbs {
  onopen?: () => void;
  onmessage?: (e: unknown) => void;
  onerror?: (e: unknown) => void;
  onclose?: (e: unknown) => void;
}

/** Holder so a test can reach the callbacks connect() was given. */
interface CbHolder {
  cbs: LiveCbs | null;
}

function makeFakeSession() {
  const audioCaptures: unknown[] = [];
  return {
    audioCaptures,
    session: {
      sendRealtimeInput: vi.fn((args: unknown) => { audioCaptures.push(args); }),
      sendClientContent: vi.fn(),
      sendToolResponse: vi.fn(),
      close: vi.fn(),
      // Deliberately NO receive(): the real Session does not expose one.
    },
  };
}

/**
 * Mock of @google/genai. connect() captures the callbacks into `holder` and
 * fires onopen (mirroring the real SDK opening the socket) so the adapter's
 * connect() ready-gate resolves.
 */
function makeGenAIMock(
  session: unknown,
  capturedApiVersions: string[] = [],
  holder: CbHolder = { cbs: null },
) {
  return {
    GoogleGenAI: vi.fn().mockImplementation((opts: { httpOptions?: { apiVersion?: string } }) => {
      if (opts.httpOptions?.apiVersion) {
        capturedApiVersions.push(opts.httpOptions.apiVersion);
      }
      return {
        live: {
          connect: vi.fn(async (args: { callbacks?: LiveCbs }) => {
            holder.cbs = args.callbacks ?? null;
            // Mirror the real handshake: socket opens, THEN the server sends
            // setupComplete. connect()'s ready-gate must key off setupComplete
            // (not onopen) so the caller never sends a turn before the session
            // is configured — see the prod-silence regression below.
            args.callbacks?.onopen?.();
            args.callbacks?.onmessage?.({ setupComplete: {} });
            return session;
          }),
        },
      };
    }),
  };
}

/** 20 ms of PCM-16-LE @ 24 kHz (a low sine so the anti-alias filter has content). */
function pcm24Tone(samples = 480): Buffer {
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) {
    buf.writeInt16LE(Math.round(8000 * Math.sin((2 * Math.PI * 200 * i) / 24000)), i * 2);
  }
  return buf;
}

describe('[mocked] GeminiLiveAdapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('connect() registers callbacks and resolves once onopen fires', async () => {
    const fake = makeFakeSession();
    const holder: CbHolder = { cbs: null };
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session, [], holder));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    await adapter.connect(); // resolves only because makeGenAIMock fired onopen

    expect(holder.cbs).not.toBeNull();
    expect(typeof holder.cbs!.onmessage).toBe('function');
    expect(typeof holder.cbs!.onopen).toBe('function');
    await adapter.close();
  });

  it('connect() waits for setupComplete, not merely onopen (prod silence fix)', async () => {
    // REGRESSION (prod 2026-06-18): resolving the ready-gate on onopen let the
    // StreamHandler send the firstMessage before Gemini processed setup, so the
    // server silently dropped it and the agent never spoke. connect() must
    // block until setupComplete. Verified empirically: a user text turn sent
    // pre-setup yields 0 audio bytes; sent post-setup yields a full reply.
    let cbs: LiveCbs | null = null;
    const session = {
      sendRealtimeInput: vi.fn(),
      sendClientContent: vi.fn(),
      sendToolResponse: vi.fn(),
      close: vi.fn(),
    };
    vi.doMock('@google/genai', () => ({
      GoogleGenAI: vi.fn().mockImplementation(() => ({
        live: {
          connect: vi.fn(async (args: { callbacks?: LiveCbs }) => {
            cbs = args.callbacks ?? null;
            args.callbacks?.onopen?.(); // socket open — but NOT yet configured
            return session;
          }),
        },
      })),
    }));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    let resolved = false;
    const p = adapter.connect().then(() => { resolved = true; });

    // onopen has fired; setupComplete has NOT. connect() must still be pending.
    await new Promise((r) => setTimeout(r, 50));
    expect(resolved).toBe(false);
    expect(cbs).not.toBeNull();

    // Server signals setup done -> connect() resolves.
    cbs!.onmessage!({ setupComplete: {} });
    await p;
    expect(resolved).toBe(true);
    await adapter.close();
  });

  it('sendAudio sends the audio: field (not deprecated media:)', async () => {
    const fake = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    await adapter.connect();

    const mulaw = Buffer.alloc(64, 0x7f);
    adapter.sendAudio(mulaw);

    expect(fake.session.sendRealtimeInput).toHaveBeenCalledOnce();
    const arg = fake.session.sendRealtimeInput.mock.calls[0][0] as Record<string, unknown>;
    expect(arg).toHaveProperty('audio');
    expect(arg).not.toHaveProperty('media');
    expect((arg.audio as Record<string, unknown>).mimeType).toBe('audio/pcm;rate=16000');
    await adapter.close();
  });

  it('sendAudio transcodes carrier mulaw-8k to PCM16 @ 16k before sending', async () => {
    const fake = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    await adapter.connect();

    const N = 160; // 20 ms of mulaw @ 8 kHz
    const mulaw = Buffer.alloc(N, 0x55);
    adapter.sendAudio(mulaw);

    expect(fake.session.sendRealtimeInput).toHaveBeenCalledOnce();
    const arg = fake.session.sendRealtimeInput.mock.calls[0][0] as Record<string, unknown>;
    const audio = arg.audio as { data: string; mimeType: string };
    expect(audio.mimeType).toBe('audio/pcm;rate=16000');

    const sentBytes = Buffer.from(audio.data, 'base64');
    // 1 byte mulaw -> 2 bytes PCM16 @ 8k -> 2x samples @ 16k -> 4 bytes, minus
    // the one sample the upsampler defers across the chunk boundary.
    expect(sentBytes.length).toBe(4 * (N - 1));
    // Discriminating: if the transcode were skipped and raw mulaw sent, length
    // would collapse to N (160) and the bytes would equal the source.
    expect(sentBytes.length).not.toBe(N);
    expect(Buffer.compare(sentBytes.subarray(0, N), mulaw)).not.toBe(0);
    await adapter.close();
  });

  it('dispatches audio via callbacks.onmessage, transcoded to mulaw-8k 20ms frames', async () => {
    // REGRESSION (prod incident 2026-06-18): the agent was silent ~20s because
    // the adapter relied on a non-existent session.receive() and registered no
    // callbacks, so onmessage was never wired. This drives the REAL contract.
    const fake = makeFakeSession();
    const holder: CbHolder = { cbs: null };
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session, [], holder));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    const audioFrames: Buffer[] = [];
    adapter.onEvent((type, data) => {
      if (type === 'audio') audioFrames.push(data as Buffer);
    });
    await adapter.connect();

    // Deliver a server message exactly as the JS SDK would — via onmessage.
    holder.cbs!.onmessage!({
      serverContent: { modelTurn: { parts: [{ inlineData: { data: pcm24Tone().toString('base64') } }] } },
    });

    await vi.waitFor(() => {
      expect(audioFrames.length).toBeGreaterThan(0);
    });
    // Every emitted frame is a mulaw 20 ms slice: at most 160 bytes.
    for (const frame of audioFrames) {
      expect(frame.length).toBeLessThanOrEqual(160);
      expect(frame.length).toBeGreaterThan(0);
    }
    // Total ~160 mulaw bytes (20 ms @ 8 kHz), far below the 960 raw PCM-24k
    // bytes — proof the downsample+encode ran. Raw passthrough would be ~960
    // and individual frames would exceed 160, failing the per-frame check.
    const totalMulaw = audioFrames.reduce((sum, f) => sum + f.length, 0);
    expect(totalMulaw).toBeGreaterThan(120);
    expect(totalMulaw).toBeLessThan(200);
    await adapter.close();
  });

  it('dispatches function_call via onmessage with original tool name preserved', async () => {
    const fake = makeFakeSession();
    const holder: CbHolder = { cbs: null };
    vi.doMock('@google/genai', () => makeGenAIMock(fake.session, [], holder));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    const calls: Array<{ call_id: string; name: string; arguments: string }> = [];
    adapter.onEvent((type, data) => {
      if (type === 'function_call') calls.push(data as { call_id: string; name: string; arguments: string });
    });
    await adapter.connect();

    holder.cbs!.onmessage!({
      toolCall: { functionCalls: [{ id: 'call_1', name: 'web_search', args: { query: 'patter' } }] },
    });

    await vi.waitFor(() => {
      expect(calls.length).toBe(1);
    });
    expect(calls[0]).toMatchObject({ call_id: 'call_1', name: 'web_search' });
    expect(JSON.parse(calls[0].arguments)).toEqual({ query: 'patter' });
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
    await adapter.close();
  });

  it('GEMINI_LIVE_3_1_FLASH_PREVIEW constant has expected value', () => {
    expect(GEMINI_LIVE_3_1_FLASH_PREVIEW).toBe('gemini-3.1-flash-live-preview');
  });

  it('pricing.ts includes gemini-3.1-flash-live-preview', async () => {
    // The real exported symbol is `llmPricing` (per-provider, per-model nested
    // rate card) — Gemini Live models live under the `google` key.
    const { llmPricing } = await import('../src/pricing.js');
    expect(llmPricing.google['gemini-3.1-flash-live-preview']).toEqual({ input: 0.30, output: 2.50 });
  });
});

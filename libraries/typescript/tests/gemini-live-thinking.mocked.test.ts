import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  GeminiLiveAdapter,
  GEMINI_LIVE_3_1_FLASH_PREVIEW,
  geminiRequiresV1Alpha,
} from '../src/providers/gemini-live';

// [mocked] GeminiLiveAdapter — Phase-2 fixes: thinking-leak suppression,
// gemini-3.1-flash-live-preview apiVersion selection, and actionable connect
// errors. Mocks the @google/genai boundary only (the real JS SDK delivers
// server messages through `callbacks.onmessage` and has no async `receive()`).

interface LiveCbs {
  onopen?: () => void;
  onmessage?: (e: unknown) => void;
  onerror?: (e: unknown) => void;
  onclose?: (e: unknown) => void;
}

interface CbHolder {
  cbs: LiveCbs | null;
}

interface ConnectCapture {
  configs: Array<Record<string, unknown>>;
  apiVersions: Array<string | undefined>;
}

function makeFakeSession() {
  return {
    sendRealtimeInput: vi.fn(),
    sendClientContent: vi.fn(),
    sendToolResponse: vi.fn(),
    close: vi.fn(),
  };
}

/**
 * Mock of @google/genai.
 *
 * `sendSetupComplete` (default true) mirrors the real handshake: socket opens,
 * THEN the server sends `setupComplete`. Pass false to simulate a model that
 * never completes setup (drives the "session ready timeout" path).
 */
function makeGenAIMock(
  session: unknown,
  capture: ConnectCapture,
  holder: CbHolder = { cbs: null },
  sendSetupComplete = true,
) {
  return {
    GoogleGenAI: vi.fn().mockImplementation((opts: { httpOptions?: { apiVersion?: string } }) => {
      capture.apiVersions.push(opts.httpOptions?.apiVersion);
      return {
        live: {
          connect: vi.fn(async (args: { config?: Record<string, unknown>; callbacks?: LiveCbs }) => {
            if (args.config) capture.configs.push(args.config);
            holder.cbs = args.callbacks ?? null;
            args.callbacks?.onopen?.();
            if (sendSetupComplete) args.callbacks?.onmessage?.({ setupComplete: {} });
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

function newCapture(): ConnectCapture {
  return { configs: [], apiVersions: [] };
}

describe('[mocked] GeminiLiveAdapter — thinking suppression', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('disables thinking by default for voice (thinkingBudget: 0)', async () => {
    const capture = newCapture();
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    await adapter.connect();

    expect(capture.configs).toHaveLength(1);
    const cfg = capture.configs[0] as { thinkingConfig?: { thinkingBudget?: number } };
    expect(cfg.thinkingConfig).toEqual({ thinkingBudget: 0 });
    await adapter.close();
  });

  it('thinking: true re-enables dynamic thinking (thinkingBudget: -1)', async () => {
    const capture = newCapture();
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture));

    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
      thinking: true,
    });
    await adapter.connect();

    const cfg = capture.configs[0] as { thinkingConfig?: { thinkingBudget?: number } };
    expect(cfg.thinkingConfig).toEqual({ thinkingBudget: -1 });
    // It is explicitly NOT the voice-default off value.
    expect(cfg.thinkingConfig?.thinkingBudget).not.toBe(0);
    await adapter.close();
  });

  it('explicit thinkingBudget wins over the thinking flag', async () => {
    const capture = newCapture();
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture));

    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
      thinking: false,
      thinkingBudget: 512,
    });
    await adapter.connect();

    const cfg = capture.configs[0] as { thinkingConfig?: { thinkingBudget?: number } };
    expect(cfg.thinkingConfig).toEqual({ thinkingBudget: 512 });
    await adapter.close();
  });

  it('NEVER emits a thought part — drops it from BOTH transcript and audio', async () => {
    // The model can still return a `thought: true` part even with thinking
    // disabled. The adapter must defensively suppress it on BOTH legs.
    const captureA = newCapture();
    const holderA: CbHolder = { cbs: null };
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), captureA, holderA));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    const transcripts: string[] = [];
    const audio: Buffer[] = [];
    adapter.onEvent((type, data) => {
      if (type === 'transcript_output') transcripts.push(data as string);
      if (type === 'audio') audio.push(data as Buffer);
    });
    await adapter.connect();

    // One server message carrying BOTH a thought part (audio + text) and a
    // normal part (audio + text) — exactly the leak shape from the live call.
    holderA.cbs!.onmessage!({
      serverContent: {
        modelTurn: {
          parts: [
            { thought: true, text: 'Initiating Call Protocol — SECRET REASONING' },
            { thought: true, inlineData: { data: pcm24Tone().toString('base64') } },
            { text: 'Buongiorno, come posso aiutarla?' },
            { inlineData: { data: pcm24Tone().toString('base64') } },
          ],
        },
      },
    });

    await vi.waitFor(() => {
      expect(audio.length).toBeGreaterThan(0);
    });

    // Only the real reply text — never the chain-of-thought.
    expect(transcripts).toEqual(['Buongiorno, come posso aiutarla?']);
    expect(transcripts.join(' ')).not.toContain('SECRET REASONING');
    const withThoughtBytes = audio.reduce((n, f) => n + f.length, 0);
    await adapter.close();

    // Baseline: the SAME message minus the thought parts must yield the SAME
    // audio byte count — proving the thought audio was dropped, not merged.
    vi.resetModules();
    const captureB = newCapture();
    const holderB: CbHolder = { cbs: null };
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), captureB, holderB));
    const adapter2 = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    const audio2: Buffer[] = [];
    adapter2.onEvent((type, data) => {
      if (type === 'audio') audio2.push(data as Buffer);
    });
    await adapter2.connect();
    holderB.cbs!.onmessage!({
      serverContent: {
        modelTurn: {
          parts: [
            { text: 'Buongiorno, come posso aiutarla?' },
            { inlineData: { data: pcm24Tone().toString('base64') } },
          ],
        },
      },
    });
    await vi.waitFor(() => {
      expect(audio2.length).toBeGreaterThan(0);
    });
    const baselineBytes = audio2.reduce((n, f) => n + f.length, 0);
    expect(withThoughtBytes).toBe(baselineBytes);
    await adapter2.close();
  });
});

describe('[mocked] GeminiLiveAdapter — apiVersion selection (issue: 3.1 session ready timeout)', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('geminiRequiresV1Alpha matches native-audio AND the 3.1 flash-live-preview family', () => {
    expect(geminiRequiresV1Alpha('gemini-2.5-flash-native-audio-preview-09-2025')).toBe(true);
    expect(geminiRequiresV1Alpha(GEMINI_LIVE_3_1_FLASH_PREVIEW)).toBe(true);
    // Retired half-cascade live models stayed on v1beta — must NOT match.
    expect(geminiRequiresV1Alpha('gemini-2.0-flash-live-001')).toBe(false);
    expect(geminiRequiresV1Alpha('gemini-2.5-flash')).toBe(false);
  });

  it('selects v1alpha for native-audio models', async () => {
    const capture = newCapture();
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture));
    const adapter = new GeminiLiveAdapter('test-key', {
      model: 'gemini-2.5-flash-native-audio-preview-09-2025',
    });
    await adapter.connect();
    expect(capture.apiVersions).toContain('v1alpha');
    await adapter.close();
  });

  it('selects v1alpha for gemini-3.1-flash-live-preview (the fix)', async () => {
    // BEFORE: the id lacked the "native-audio" substring, so it fell to the
    // SDK default (v1beta), the server never sent setupComplete, and connect()
    // died with "session ready timeout". It must now resolve to v1alpha.
    const capture = newCapture();
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture));
    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    await adapter.connect();
    expect(capture.apiVersions).toEqual(['v1alpha']);
    await adapter.close();
  });

  it('explicit apiVersion override wins over auto-detection', async () => {
    const capture = newCapture();
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture));
    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
      apiVersion: 'v1beta',
    });
    await adapter.connect();
    expect(capture.apiVersions).toEqual(['v1beta']);
    await adapter.close();
  });
});

describe('[mocked] GeminiLiveAdapter — actionable connect errors', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('surfaces an actionable error when the socket errors before setupComplete', async () => {
    const holder: CbHolder = { cbs: null };
    const capture = newCapture();
    // Never send setupComplete; the test fires onerror itself.
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture, holder, false));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    const p = adapter.connect();
    // Wait until the SDK mock has handed us the callbacks, then fail the socket.
    await vi.waitFor(() => expect(holder.cbs).not.toBeNull());
    holder.cbs!.onerror!(new Error('1007 invalid model'));

    await expect(p).rejects.toThrow(/Gemini Live failed to start a session for model/);
  });

  it('actionable error names the model, apiVersion, and the three root causes', async () => {
    const holder: CbHolder = { cbs: null };
    const capture = newCapture();
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture, holder, false));

    const adapter = new GeminiLiveAdapter('test-key', { model: GEMINI_LIVE_3_1_FLASH_PREVIEW });
    const p = adapter.connect();
    await vi.waitFor(() => expect(holder.cbs).not.toBeNull());
    holder.cbs!.onerror!(new Error('boom'));

    const err = await p.catch((e: Error) => e);
    const msg = (err as Error).message;
    expect(msg).toContain(GEMINI_LIVE_3_1_FLASH_PREVIEW);
    expect(msg).toContain('v1alpha'); // resolved apiVersion for the 3.1 model
    expect(msg).toContain('apiVersion');
    expect(msg).toContain('API key');
    expect(msg).toContain('boom');
  });

  it('a session-ready timeout surfaces the actionable error (not a bare timeout)', async () => {
    const holder: CbHolder = { cbs: null };
    const capture = newCapture();
    // onopen fires but setupComplete never arrives -> the ready gate trips.
    // Use the internal short connectTimeoutMs knob so the test is fast.
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture, holder, false));

    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
      connectTimeoutMs: 40,
    });
    const err = await adapter.connect().catch((e: Error) => e);
    const msg = (err as Error).message;
    expect(msg).toMatch(/session ready timeout/);
    expect(msg).toMatch(/Likely causes/);
    expect(msg).toContain(GEMINI_LIVE_3_1_FLASH_PREVIEW);
    expect(msg).toContain('v1alpha');
  });
});

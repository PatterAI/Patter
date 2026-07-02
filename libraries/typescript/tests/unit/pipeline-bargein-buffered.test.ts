/**
 * [unit] Barge-in while the carrier still plays buffered audio.
 *
 * The pipeline pushes TTS audio to the carrier as fast as the provider
 * synthesizes it; the carrier buffers and plays at realtime. With an
 * agent-runtime LLM (Hermes / OpenClaw) the whole — often long — reply
 * arrives at once, so the SDK finishes *pushing* tens of seconds before the
 * caller finishes *hearing*. The handler must keep ``isSpeaking=true`` (with
 * ``tailGraceActive=false``) for that whole audible backlog so a barge-in
 * still takes the cancel path (``sendClear`` drops the carrier buffer)
 * instead of being mis-read as a calm next turn — previously the fixed 1.5 s
 * grace expired mid-reply and "the agent detected the barge-in but kept
 * talking".
 *
 * Parity with Python tests/unit/test_pipeline_bargein_buffered.py.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { PipelineHookExecutor } from '../../src/pipeline-hooks';
import type { WebSocket as WSWebSocket } from 'ws';

function makeMockBridge(overrides?: Partial<TelephonyBridge>): TelephonyBridge {
  return {
    label: 'TestBridge',
    telephonyProvider: 'twilio',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(),
    close: vi.fn(),
    on: vi.fn(),
    once: vi.fn(),
    readyState: 1,
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makeDeps(overrides?: Partial<StreamHandlerDeps>): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-oai-key' },
    agent: { systemPrompt: 'Test agent', provider: 'pipeline' },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(null),
    sanitizeVariables: vi.fn(() => ({})),
    resolveVariables: vi.fn((tpl: string) => tpl),
    ...overrides,
  } as StreamHandlerDeps;
}

function makeHandler(deps = makeDeps()): StreamHandler {
  return new StreamHandler(deps, makeMockWs(), '+15551111111', '+15552222222');
}

describe('[unit] trackOutboundPlayback — cursor math', () => {
  it('advances the cursor by chunk duration for PCM16 @ 16 kHz', () => {
    const h = makeHandler() as any;
    const before = Date.now();
    h.trackOutboundPlayback(3200); // 100 ms at 32 bytes/ms
    expect(h.playbackBufferedUntil).toBeGreaterThanOrEqual(before + 95);
    expect(h.playbackBufferedUntil).toBeLessThanOrEqual(before + 150);
  });

  it('uses 8 bytes/ms when the TTS emits carrier-native μ-law 8 kHz', () => {
    const h = makeHandler() as any;
    h.ttsOutputFormatNativeForCarrier = true; // Twilio bridge in makeDeps
    const before = Date.now();
    h.trackOutboundPlayback(800); // 100 ms at 8 bytes/ms
    expect(h.playbackBufferedUntil).toBeGreaterThanOrEqual(before + 95);
    expect(h.playbackBufferedUntil).toBeLessThanOrEqual(before + 150);
  });

  it('keeps 32 bytes/ms for Telnyx native pcm_16000', () => {
    const h = makeHandler(
      makeDeps({ bridge: makeMockBridge({ telephonyProvider: 'telnyx' }) }),
    ) as any;
    h.ttsOutputFormatNativeForCarrier = true;
    const before = Date.now();
    h.trackOutboundPlayback(3200); // still 100 ms at 32 bytes/ms
    expect(h.playbackBufferedUntil).toBeGreaterThanOrEqual(before + 95);
    expect(h.playbackBufferedUntil).toBeLessThanOrEqual(before + 150);
  });

  it('accumulates back-to-back chunks', () => {
    const h = makeHandler() as any;
    const before = Date.now();
    h.trackOutboundPlayback(3200);
    h.trackOutboundPlayback(3200);
    expect(h.playbackBufferedUntil).toBeGreaterThanOrEqual(before + 195);
    expect(h.playbackBufferedUntil).toBeLessThanOrEqual(before + 250);
  });

  it('rebases to now after an idle gap', () => {
    const h = makeHandler() as any;
    h.playbackBufferedUntil = Date.now() - 10_000;
    const before = Date.now();
    h.trackOutboundPlayback(3200);
    expect(h.playbackBufferedUntil).toBeGreaterThanOrEqual(before + 95);
    expect(h.playbackBufferedUntil).toBeLessThanOrEqual(before + 150);
  });

  it('treats an empty chunk as a no-op', () => {
    const h = makeHandler() as any;
    h.trackOutboundPlayback(0);
    expect(h.playbackBufferedUntil).toBe(0);
  });
});

describe('[unit] endSpeakingWithGrace — two-phase wait', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubEnv('PATTER_TTS_TAIL_GRACE_MS', '50');
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  it('holds isSpeaking (NOT tail grace) while the carrier backlog plays', () => {
    const h = makeHandler() as any;
    h.isSpeaking = true;
    h.playbackBufferedUntil = Date.now() + 500;

    h.endSpeakingWithGrace();
    vi.advanceTimersByTime(100); // well inside the backlog window

    expect(h.isSpeaking).toBe(true);
    expect(h.tailGraceActive).toBe(false);
    h.clearGraceTimer();
  });

  it('drains the backlog, then tail grace, then flips to idle', () => {
    const h = makeHandler() as any;
    h.isSpeaking = true;
    h.playbackBufferedUntil = Date.now() + 150;

    h.endSpeakingWithGrace();
    vi.advanceTimersByTime(160); // backlog drained
    expect(h.isSpeaking).toBe(true);
    expect(h.tailGraceActive).toBe(true);

    vi.advanceTimersByTime(60); // grace elapsed
    expect(h.isSpeaking).toBe(false);
    expect(h.tailGraceActive).toBe(false);
  });

  it('starts tail grace immediately when there is no backlog (legacy path)', () => {
    const h = makeHandler() as any;
    h.isSpeaking = true;
    expect(h.playbackBufferedUntil).toBe(0);

    h.endSpeakingWithGrace();
    expect(h.tailGraceActive).toBe(true);

    vi.advanceTimersByTime(60);
    expect(h.isSpeaking).toBe(false);
  });
});

describe('[unit] barge-in during the buffered backlog — Hermes/OpenClaw regression', () => {
  beforeEach(() => {
    vi.stubEnv('PATTER_TTS_TAIL_GRACE_MS', '50');
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('a transcript during the backlog runs the FULL cancel path', () => {
    const deps = makeDeps();
    const h = makeHandler(deps) as any;
    h.isSpeaking = true;
    // Turn finished pushing; carrier still has seconds of audio queued.
    h.playbackBufferedUntil = Date.now() + 5_000;
    h.endSpeakingWithGrace();

    expect(h.isSpeaking).toBe(true); // backlog holds the floor
    expect(h.tailGraceActive).toBe(false);

    const interrupted = h.handleBargeIn({ text: 'aspetta', isFinal: false });

    expect(interrupted).toBe(true);
    expect(deps.bridge.sendClear).toHaveBeenCalledTimes(1);
    expect(h.isSpeaking).toBe(false);
    expect(h.playbackBufferedUntil).toBe(0);
    h.clearGraceTimer();
  });

  it('cancelSpeaking resets the playback cursor', () => {
    const h = makeHandler() as any;
    h.isSpeaking = true;
    h.playbackBufferedUntil = Date.now() + 5_000;

    h.cancelSpeaking();

    expect(h.playbackBufferedUntil).toBe(0);
  });

  it('synthesizeSentence advances the cursor for every pushed chunk', async () => {
    const deps = makeDeps();
    const h = makeHandler(deps) as any;
    h.isSpeaking = true;
    h.tts = {
      synthesizeStream: async function* () {
        yield Buffer.alloc(6400); // 200 ms of PCM16 @ 16 kHz
      },
    };

    const before = Date.now();
    await h.synthesizeSentence(
      'ciao',
      new PipelineHookExecutor(undefined),
      h.buildHookContext(),
      { value: false },
    );

    expect(deps.bridge.sendAudio).toHaveBeenCalled();
    expect(h.playbackBufferedUntil).toBeGreaterThanOrEqual(before + 195);
    expect(h.playbackBufferedUntil).toBeLessThanOrEqual(before + 300);
  });
});

describe('[unit] heardResponsePrefix — what did the caller actually listen to?', () => {
  it('(a) a fully played turn returns the full text, everything heard', () => {
    const h = makeHandler() as any;
    h.turnSpokenSegments = [
      { text: 'Frase uno.', startMs: 0, durMs: 2000 },
      { text: 'Frase due.', startMs: 2000, durMs: 0 },
    ];
    h.turnPlaybackTotalMs = 4000;
    h.playbackBufferedUntil = 0; // long drained

    const heard = h.heardResponsePrefix();

    expect(heard.text).toBe('Frase uno. Frase due.');
    expect(heard.heardEverything).toBe(true);
  });

  it('(b) a mid-sentence interruption splits on a word boundary', () => {
    const h = makeHandler() as any;
    h.turnSpokenSegments = [
      { text: 'Sentence one here.', startMs: 0, durMs: 2000 }, // 0..2 s
      { text: 'The second sentence goes here.', startMs: 2000, durMs: 2000 }, // 2..4 s
      { text: 'Third and final sentence.', startMs: 4000, durMs: 0 }, // 4..6 s
    ];
    h.turnPlaybackTotalMs = 6000;
    // 3.0 s played → sentence one full, sentence two half (word boundary).
    h.playbackBufferedUntil = Date.now() + 3000;

    const heard = h.heardResponsePrefix();

    expect(heard.text).toBe('Sentence one here. The second sentence');
    expect(heard.heardEverything).toBe(false);
  });

  it('a sentence whose start sits at the play head is not yet heard', () => {
    const h = makeHandler() as any;
    h.turnSpokenSegments = [
      { text: 'Frase uno.', startMs: 0, durMs: 2000 },
      { text: 'Frase due.', startMs: 2000, durMs: 2000 },
      { text: 'Frase tre.', startMs: 4000, durMs: 0 },
    ];
    h.turnPlaybackTotalMs = 6000;
    h.playbackBufferedUntil = Date.now() + 4000; // heard = 2000 ms

    const heard = h.heardResponsePrefix();

    expect(heard.text).toBe('Frase uno.');
    expect(heard.heardEverything).toBe(false);
  });

  it('returns null when no segments were tracked', () => {
    const h = makeHandler() as any;
    expect(h.heardResponsePrefix()).toBeNull();
    expect(h.heardResponseSplit()).toBeNull();
  });

  it('(f) the unsaid remainder is the exact complement of the heard prefix', () => {
    const h = makeHandler() as any;
    h.turnSpokenSegments = [
      { text: 'Sentence one here.', startMs: 0, durMs: 2000 },
      { text: 'The second sentence goes here.', startMs: 2000, durMs: 2000 },
      { text: 'Third and final sentence.', startMs: 4000, durMs: 0 },
    ];
    h.turnPlaybackTotalMs = 6000;
    h.playbackBufferedUntil = Date.now() + 3000; // heard = 3000 ms

    const heard = h.heardResponsePrefix().text as string;
    const unsaid = h.heardResponseSplit().unsaid as string;
    const full =
      'Sentence one here. The second sentence goes here. Third and final sentence.';

    expect(heard).toBe('Sentence one here. The second sentence');
    expect(unsaid).toBe('goes here. Third and final sentence.');
    expect([heard, unsaid].filter(Boolean).join(' ')).toBe(full);
  });

  it('(c) a Twilio seg mark echo beats the lagging byte estimate', async () => {
    const h = makeHandler() as any;
    h.turnSpokenSegments = [
      { text: 'Uno.', startMs: 0, durMs: 2000 },
      { text: 'Due.', startMs: 2000, durMs: 2000 },
      { text: 'Tre.', startMs: 4000, durMs: 0 },
    ];
    h.turnPlaybackTotalMs = 6000;
    // Byte estimate lags badly: 5.5 s still "buffered" → ~0.5 s "played".
    h.playbackBufferedUntil = Date.now() + 5500;

    expect(h.heardResponsePrefix().text).toBe(''); // nothing confirmed yet

    // The carrier echoes seg_2 (end of the second sentence at 4000 ms).
    h.segMarkEndMs = new Map([
      ['seg_1', 2000],
      ['seg_2', 4000],
    ]);
    await h.onMark('seg_2');
    expect(h.markConfirmedMs).toBe(4000);

    const heard = h.heardResponsePrefix();
    expect(heard.text).toBe('Uno. Due.');
    expect(heard.heardEverything).toBe(false);
  });

  it('synthesizeSentence records a heard-prefix segment + emits a seg mark', async () => {
    const deps = makeDeps();
    const h = makeHandler(deps) as any;
    h.isSpeaking = true;
    h.tts = {
      synthesizeStream: async function* () {
        yield Buffer.alloc(6400); // 200 ms of PCM16 @ 16 kHz
      },
    };

    const hookExecutor = new PipelineHookExecutor(undefined);
    await h.synthesizeSentence('Frase uno.', hookExecutor, h.buildHookContext(), {
      value: false,
    });
    await h.synthesizeSentence('Frase due.', hookExecutor, h.buildHookContext(), {
      value: true,
    });

    // (text, startMs, durMs): "Frase uno." is finalized to its own 200 ms
    // playout when "Frase due." stamps; the last sentence keeps durMs=0.
    expect(h.turnSpokenSegments).toEqual([
      { text: 'Frase uno.', startMs: 0, durMs: 200 },
      { text: 'Frase due.', startMs: 200, durMs: 0 },
    ]);
    // A per-sentence carrier mark was emitted (Twilio) with the end position.
    const markNames = (deps.bridge.sendMark as any).mock.calls.map((c: unknown[]) => c[1]);
    expect(markNames).toContain('seg_1');
    expect(h.segMarkEndMs.get('seg_1')).toBeCloseTo(200, 1);
  });

  it('(e) a seg mark leaves the fm_ first-message flow-control FIFO untouched', async () => {
    const h = makeHandler() as any;
    let resolved = false;
    h.pendingMarks = [
      {
        name: 'fm_1',
        resolve: () => {
          resolved = true;
        },
        promise: Promise.resolve(),
      },
    ];
    h.segMarkEndMs = new Map([['seg_1', 2000]]);

    await h.onMark('seg_1');
    expect(h.markConfirmedMs).toBe(2000);
    expect(h.pendingMarks).toHaveLength(1);
    expect(resolved).toBe(false);

    await h.onMark('fm_1');
    expect(resolved).toBe(true);
    expect(h.pendingMarks).toHaveLength(0);
  });

  it('filler audio advances the clock without adding a segment', async () => {
    const deps = makeDeps();
    const h = makeHandler(deps) as any;
    h.isSpeaking = true;
    h.tts = {
      synthesizeStream: async function* () {
        yield Buffer.alloc(6400);
      },
    };

    await h.synthesizeSentence(
      'One moment.',
      new PipelineHookExecutor(undefined),
      h.buildHookContext(),
      { value: false },
      false, // recordSegment=false — filler / error fallback
    );

    expect(h.turnSpokenSegments).toEqual([]);
    expect(h.turnPlaybackTotalMs).toBe(200);
  });
});

describe('[unit] post-complete barge-in — history rewritten to the heard prefix', () => {
  const FULL = 'Frase uno. Frase due. Frase tre.';

  function completedTurnHandler(): { h: any; deps: StreamHandlerDeps } {
    const deps = makeDeps();
    const h = makeHandler(deps) as any;
    h.isSpeaking = true;
    h.history.push({ role: 'assistant', text: FULL, timestamp: Date.now() });
    h.turnSpokenSegments = [
      { text: 'Frase uno.', startMs: 0, durMs: 2000 }, // 0..2 s
      { text: 'Frase due.', startMs: 2000, durMs: 2000 }, // 2..4 s
      { text: 'Frase tre.', startMs: 4000, durMs: 0 }, // 4..6 s
    ];
    h.turnPlaybackTotalMs = 6000;
    // 1.8 s still buffered → heard ≈ 4.2 s: sentences one and two in full,
    // sentence three barely begun (rounds to no words).
    h.playbackBufferedUntil = Date.now() + 1800;
    return { h, deps };
  }

  it('a barge-in during the buffered tail truncates the recorded reply', () => {
    const { h, deps } = completedTurnHandler();

    h.runBargeInCancel('aspetta');

    const last = h.history.entries[h.history.entries.length - 1];
    expect(last.text).toBe('Frase uno. Frase due. [interrupted by caller]');
    expect(deps.bridge.sendClear).toHaveBeenCalledTimes(1);
  });

  it('no backlog → no rewrite', () => {
    const { h } = completedTurnHandler();
    h.playbackBufferedUntil = 0; // everything already played

    h.runBargeInCancel('ok');

    const last = h.history.entries[h.history.entries.length - 1];
    expect(last.text).toBe(FULL);
  });

  it('a turn still in flight is owned by the streaming marker — no rewrite', () => {
    const { h } = completedTurnHandler();
    h.dispatchTask = new Promise(() => {}); // in flight, never settles

    h.runBargeInCancel('aspetta');

    const last = h.history.entries[h.history.entries.length - 1];
    expect(last.text).toBe(FULL);
    h.dispatchTask = null;
  });
});

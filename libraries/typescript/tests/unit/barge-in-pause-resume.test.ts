/**
 * Pause-and-resume false-interruption handling (``bargeInMode: 'pause_resume'``).
 *
 * Mirrors the Python ``test_barge_in_pause_resume.py`` so the cross-SDK
 * behaviour stays in lockstep. LiveKit-style three-way state machine on top
 * of the existing barge-in machinery:
 *
 * - PAUSE — VAD ``speech_start`` while the agent speaks gates the send loops
 *   on ``outputPaused`` and clears the carrier buffer. Nothing is cancelled:
 *   the LLM stream keeps buffering sentences (bounded) and the TTS stream
 *   keeps queueing audio into per-sentence retention entries (bounded).
 * - KILL — a committed final transcript (non-echo / non-hallucination /
 *   non-duplicate) within ``bargeInConfirmMs`` runs the full cancel path
 *   (``runBargeInCancel``) and discards the paused buffers.
 * - RESUME — the window expires with no confirming transcript: the
 *   cleared-but-unheard tail is re-sent from the retained audio at SENTENCE
 *   granularity (first sentence not fully played) and the event is recorded
 *   as a false interruption (``recordOverlapEnd(false)`` — the backchannel
 *   path, never an interruption count).
 *
 * Fixtures mirror ``barge-in-two-stage.test.ts``: a real StreamHandler with
 * the telephony bridge / WS mocked at the external boundary, private state
 * armed via casts — the same surface the audio/STT loops drive.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { PipelineHookExecutor } from '../../src/pipeline-hooks';
import type { AgentOptions } from '../../src/types';
import type { WebSocket as WSWebSocket } from 'ws';

function makeMockBridge(): TelephonyBridge {
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
  } as unknown as TelephonyBridge;
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

function makeDeps(agentOverrides: Partial<AgentOptions> = {}): StreamHandlerDeps {
  return {
    config: {},
    agent: {
      systemPrompt: 'Test agent',
      provider: 'pipeline',
      bargeInMode: 'pause_resume',
      ...agentOverrides,
    } as AgentOptions,
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(null),
    sanitizeVariables: vi.fn((raw: Record<string, unknown>) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl: string) => tpl),
  } as unknown as StreamHandlerDeps;
}

interface RetainedEntry {
  text: string;
  chunks: Buffer[];
  sent: number;
}

interface MetricsAccLike {
  recordOverlapStart: () => void;
  recordOverlapEnd: (wasInterruption: boolean) => void;
  recordBargeinDetected: () => void;
  recordTurnInterrupted: () => void;
  recordTtsStopped: () => void;
  recordTtsFirstByte: () => void;
  anchorUserSpeechStart: () => void;
}

interface Priv {
  isSpeaking: boolean;
  speakingStartedAt: number | null;
  firstAudioSentAt: number | null;
  tailGraceActive: boolean;
  graceTimer: ReturnType<typeof setTimeout> | null;
  llmAbort: AbortController | null;
  metricsAcc: MetricsAccLike;
  bargeInMode: 'cancel' | 'pause_resume';
  bargeInPendingSince: number | null;
  bargeInPendingTimer: ReturnType<typeof setTimeout> | null;
  outputPaused: boolean;
  pausedSentences: string[];
  turnSentenceAudio: RetainedEntry[];
  pauseRetainedBytes: number;
  pauseResumeOverflowed: boolean;
  pauseResumeIndex: number;
  turnOutputDone: boolean;
  turnPlaybackTotalMs: number;
  turnSpokenSegments: Array<{ text: string; startMs: number }>;
  playbackBufferedUntil: number;
  dispatchTask: Promise<void> | null;
  lastCommitText: string;
  lastCommitAt: number;
  ttsOutputFormatNativeForCarrier: boolean;
  tts: unknown;
  llmLoop: unknown;
  history: { entries: Array<{ role: string; text: string; timestamp: number }> };
  startPauseResume(): void;
  shouldPauseForBargeIn(): boolean;
  bufferSentenceIfPaused(s: string): boolean;
  releasePausedSentences(): string[];
  beginRetainedSentence(t: string): RetainedEntry | null;
  retainPauseChunk(e: RetainedEntry, c: Buffer): boolean;
  pauseRetainedCapBytes(): number;
  clearPendingBargeIn(): void;
  clearGraceTimer(): void;
  handleBargeIn(t: { text?: string; isFinal?: boolean; speechFinal?: boolean }): boolean;
  synthesizeSentence(
    s: string,
    he: PipelineHookExecutor,
    ctx: unknown,
    f: { value: boolean },
    recordSegment?: boolean,
  ): Promise<void>;
  buildHookContext(): unknown;
  runPipelineLlm(
    t: string,
    he: PipelineHookExecutor,
    ctx: unknown,
    hist: Array<{ role: string; text: string }>,
  ): Promise<{ text: string; interrupted: boolean }>;
}

function priv(h: StreamHandler): Priv {
  return h as unknown as Priv;
}

const PAUSE_MAX_BUFFERED_SENTENCES = 32;

/** Handler in pause_resume mode with the speaking state armed (gates open). */
function makeHandler(agentOverrides: Partial<AgentOptions> = {}): {
  h: StreamHandler;
  p: Priv;
  deps: StreamHandlerDeps;
} {
  const deps = makeDeps(agentOverrides);
  const h = new StreamHandler(deps, makeMockWs(), '+15551110000', '+15552220000');
  h.setStreamSid('MZ-pause-resume');
  const p = priv(h);
  p.isSpeaking = true;
  p.speakingStartedAt = Date.now() - 1500; // past the canBargeIn gate
  p.firstAudioSentAt = Date.now() - 1500;
  p.llmAbort = new AbortController();
  // Post-complete default, like the Python fixture (getattr default True):
  // resume re-arms the grace machinery unless a turn body is in flight.
  p.turnOutputDone = true;
  return { h, p, deps };
}

/** Three sentences pushed; sentence one fully played, sentence two
 * mid-playback, sentence three still entirely in the carrier buffer. */
function seedPartiallyHeardTurn(p: Priv): void {
  p.turnSpokenSegments = [
    { text: 'Frase uno.', startMs: 0 },
    { text: 'Frase due.', startMs: 2000 },
    { text: 'Frase tre.', startMs: 4000 },
  ];
  p.turnPlaybackTotalMs = 6000;
  // 3.5 s still buffered → heard = 2.5 s: inside sentence two.
  p.playbackBufferedUntil = Date.now() + 3500;
  p.turnSentenceAudio = [
    { text: 'Frase uno.', chunks: [Buffer.alloc(320, 1)], sent: 1 },
    { text: 'Frase due.', chunks: [Buffer.alloc(320, 2), Buffer.alloc(320, 3)], sent: 2 },
    { text: 'Frase tre.', chunks: [Buffer.alloc(320, 4)], sent: 1 },
  ];
  p.pauseRetainedBytes = 4 * 320;
}

// ---------------------------------------------------------------------------
// PAUSE — speech_start gates output without cancelling anything
// ---------------------------------------------------------------------------

describe('pause_resume — PAUSE on speech_start', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('pause clears the carrier but cancels nothing', () => {
    const { p, deps } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);
    const overlapStart = vi.spyOn(p.metricsAcc, 'recordOverlapStart');
    const bargeinDetected = vi.spyOn(p.metricsAcc, 'recordBargeinDetected');
    const turnInterrupted = vi.spyOn(p.metricsAcc, 'recordTurnInterrupted');

    p.startPauseResume();

    expect(p.outputPaused).toBe(true);
    expect(p.isSpeaking).toBe(true); // pause must NOT take the floor
    expect(p.llmAbort!.signal.aborted).toBe(false); // LLM stream stays alive
    expect(deps.bridge.sendClear).toHaveBeenCalledTimes(1);
    expect(overlapStart).toHaveBeenCalledTimes(1);
    expect(bargeinDetected).not.toHaveBeenCalled();
    expect(turnInterrupted).not.toHaveBeenCalled();
    expect(p.bargeInPendingSince).not.toBeNull();
    expect(p.bargeInPendingTimer).not.toBeNull();
    p.clearPendingBargeIn();
  });

  it('pause freezes the playback cursor at the heard offset', () => {
    const { p } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);

    p.startPauseResume();

    // heard = total (6000) - backlog (3500) = 2500 ms; the cleared backlog
    // is void so the cursor snaps to "nothing pending".
    expect(Math.abs(p.turnPlaybackTotalMs - 2500)).toBeLessThanOrEqual(100);
    expect(p.playbackBufferedUntil).toBe(0);
    // Resume offset: sentence two (start 2000, end 4000 > heard 2500) is
    // the first not FULLY played → index 1.
    expect(p.pauseResumeIndex).toBe(1);
    p.clearPendingBargeIn();
  });

  it('pause is idempotent', () => {
    const { p, deps } = makeHandler({ bargeInConfirmMs: 10_000 });
    const overlapStart = vi.spyOn(p.metricsAcc, 'recordOverlapStart');
    p.startPauseResume();
    const firstSince = p.bargeInPendingSince;
    const firstTimer = p.bargeInPendingTimer;

    p.startPauseResume();

    expect(p.bargeInPendingSince).toBe(firstSince);
    expect(p.bargeInPendingTimer).toBe(firstTimer);
    expect(overlapStart).toHaveBeenCalledTimes(1);
    expect(deps.bridge.sendClear).toHaveBeenCalledTimes(1);
    p.clearPendingBargeIn();
  });

  it('shouldPauseForBargeIn requires the mode AND resumable state', () => {
    // Legacy mode — never pause.
    {
      const { p } = makeHandler({ bargeInMode: 'cancel' });
      expect(p.shouldPauseForBargeIn()).toBe(false);
    }
    // pause_resume but no dispatch in flight and no retained audio (e.g.
    // the firstMessage paced sender) → fall back to immediate cancel.
    {
      const { p } = makeHandler();
      expect(p.shouldPauseForBargeIn()).toBe(false);
      p.turnSentenceAudio = [{ text: 'x', chunks: [Buffer.from('a')], sent: 1 }];
      expect(p.shouldPauseForBargeIn()).toBe(true);
    }
    // Dispatch in flight → pause path available.
    {
      const { p } = makeHandler();
      p.dispatchTask = Promise.resolve();
      expect(p.shouldPauseForBargeIn()).toBe(true);
      p.dispatchTask = null;
    }
    // After a retained-audio overflow the rest of the turn is legacy.
    {
      const { p } = makeHandler();
      p.pauseResumeOverflowed = true;
      p.turnSentenceAudio = [{ text: 'x', chunks: [Buffer.from('a')], sent: 1 }];
      expect(p.shouldPauseForBargeIn()).toBe(false);
    }
  });

  it('defaults: bargeInMode unset → cancel; unknown value → cancel', () => {
    const depsDefault = makeDeps({ bargeInMode: undefined });
    const hDefault = new StreamHandler(depsDefault, makeMockWs(), '+1', '+2');
    expect(priv(hDefault).bargeInMode).toBe('cancel');
    // Config off → retention never opens, the old send path is untouched.
    expect(priv(hDefault).beginRetainedSentence('x')).toBeNull();

    const depsBad = makeDeps({ bargeInMode: 'resume_maybe' as unknown as 'cancel' });
    const hBad = new StreamHandler(depsBad, makeMockWs(), '+1', '+2');
    expect(priv(hBad).bargeInMode).toBe('cancel');
  });
});

// ---------------------------------------------------------------------------
// While paused — synthesis queues, sentences buffer
// ---------------------------------------------------------------------------

describe('pause_resume — buffering while paused', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('synthesizeSentence queues audio while paused (TTS stream stays alive)', async () => {
    const { p, deps } = makeHandler();
    const ttsFirstByte = vi.spyOn(p.metricsAcc, 'recordTtsFirstByte');
    p.tts = {
      synthesizeStream: async function* () {
        yield Buffer.alloc(640, 0);
        yield Buffer.alloc(640, 1);
      },
    };
    p.outputPaused = true;
    const firstChunkPending = { value: false };

    await p.synthesizeSentence(
      'Frase uno.',
      new PipelineHookExecutor(undefined),
      p.buildHookContext(),
      firstChunkPending,
    );

    // Chunks went to the retention entry, never to the carrier.
    expect(deps.bridge.sendAudio).not.toHaveBeenCalled();
    expect(p.turnSentenceAudio).toHaveLength(1);
    const entry = p.turnSentenceAudio[0];
    expect(entry.text).toBe('Frase uno.');
    expect(entry.chunks).toEqual([Buffer.alloc(640, 0), Buffer.alloc(640, 1)]);
    expect(entry.sent).toBe(0);
    // Nothing was sent → no heard-prefix segment, no cursor motion, and the
    // "first TTS byte" signal waits for the first post-resume chunk.
    expect(p.turnSpokenSegments).toEqual([]);
    expect(p.turnPlaybackTotalMs).toBe(0);
    expect(firstChunkPending.value).toBe(false);
    expect(ttsFirstByte).not.toHaveBeenCalled();
  });

  it('sentences buffer as text while paused and release in order', () => {
    const { p } = makeHandler();
    p.outputPaused = true;

    expect(p.bufferSentenceIfPaused('Frase uno.')).toBe(true);
    expect(p.bufferSentenceIfPaused('Frase due.')).toBe(true);
    expect(p.pausedSentences).toEqual(['Frase uno.', 'Frase due.']);
    expect(p.isSpeaking).toBe(true);

    p.outputPaused = false;
    expect(p.bufferSentenceIfPaused('Frase tre.')).toBe(false);
    expect(p.releasePausedSentences()).toEqual(['Frase uno.', 'Frase due.']);
    expect(p.pausedSentences).toEqual([]);
  });

  it('sentence-buffer overflow degrades to a full cancel', () => {
    const { p } = makeHandler();
    const turnInterrupted = vi.spyOn(p.metricsAcc, 'recordTurnInterrupted');
    p.outputPaused = true;
    p.pausedSentences = Array.from({ length: PAUSE_MAX_BUFFERED_SENTENCES }, () => 's');

    const handled = p.bufferSentenceIfPaused('one too many');

    expect(handled).toBe(true);
    expect(p.isSpeaking).toBe(false); // overflow must run the full cancel path
    expect(p.llmAbort!.signal.aborted).toBe(true);
    expect(p.outputPaused).toBe(false);
    expect(turnInterrupted).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// RESUME — timeout with no confirming transcript re-sends the unheard tail
// ---------------------------------------------------------------------------

describe('pause_resume — RESUME after the confirm window expires', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('resume re-sends the unheard tail from retained audio', () => {
    const { p, deps } = makeHandler({ bargeInConfirmMs: 30 });
    seedPartiallyHeardTurn(p);

    p.startPauseResume();
    expect(p.outputPaused).toBe(true);

    vi.advanceTimersByTime(40); // past the confirm window → resume fires

    expect(p.outputPaused).toBe(false);
    expect(p.isSpeaking).toBe(true); // resume keeps the floor
    // Resume offset was sentence index 1 → "Frase due." (replayed from its
    // start: sentence granularity) and "Frase tre." are re-sent: 2 + 1
    // chunks. "Frase uno." (fully heard) is NOT re-sent.
    expect(deps.bridge.sendAudio).toHaveBeenCalledTimes(3);
    expect(p.turnSentenceAudio[0].sent).toBe(1); // untouched prefix
    expect(p.turnSentenceAudio[1].sent).toBe(2);
    expect(p.turnSentenceAudio[2].sent).toBe(1);
    // The replayed sentences were re-stamped on the (frozen-then-resumed)
    // timeline; "Frase uno." kept its original stamp.
    expect(p.turnSpokenSegments.map((s) => s.text)).toEqual([
      'Frase uno.',
      'Frase due.',
      'Frase tre.',
    ]);
    expect(Math.abs(p.turnSpokenSegments[1].startMs - 2500)).toBeLessThanOrEqual(100);
    // The re-sent tail advanced the playback cursor again.
    expect(p.turnPlaybackTotalMs).toBeGreaterThan(2500);
    expect(p.playbackBufferedUntil).toBeGreaterThan(0);
    p.clearGraceTimer();
  });

  it('resume records a FALSE interruption, not an interruption', () => {
    const { h, p } = makeHandler({ bargeInConfirmMs: 30 });
    seedPartiallyHeardTurn(p);
    const overlapEnd = vi.spyOn(p.metricsAcc, 'recordOverlapEnd');
    const bargeinDetected = vi.spyOn(p.metricsAcc, 'recordBargeinDetected');
    const turnInterrupted = vi.spyOn(p.metricsAcc, 'recordTurnInterrupted');
    const ttsStopped = vi.spyOn(p.metricsAcc, 'recordTtsStopped');
    const anchor = vi.spyOn(p.metricsAcc, 'anchorUserSpeechStart');
    const events: Array<{ resumedSentences: number }> = [];
    h.addObserver<{ resumedSentences: number }>((payload) => {
      events.push(payload);
    }, 'false_interruption');

    p.startPauseResume();
    vi.advanceTimersByTime(40);

    // Backchannel path: overlap closed with wasInterruption=false and the
    // interruption-side records never fired.
    expect(overlapEnd).toHaveBeenCalledTimes(1);
    expect(overlapEnd).toHaveBeenCalledWith(false);
    expect(bargeinDetected).not.toHaveBeenCalled();
    expect(turnInterrupted).not.toHaveBeenCalled();
    expect(ttsStopped).not.toHaveBeenCalled();
    expect(anchor).toHaveBeenCalled();
    expect(p.bargeInPendingSince).toBeNull();
    expect(p.bargeInPendingTimer).toBeNull();
    // Observability: the false_interruption event carries the resumed count.
    expect(events).toEqual([{ resumedSentences: 2 }]);
    p.clearGraceTimer();
  });

  it('resume with nothing unheard just unpauses', () => {
    const { p, deps } = makeHandler({ bargeInConfirmMs: 30 });
    p.turnSpokenSegments = [{ text: 'Frase uno.', startMs: 0 }];
    p.turnPlaybackTotalMs = 2000;
    p.playbackBufferedUntil = 0; // fully drained
    p.turnSentenceAudio = [
      { text: 'Frase uno.', chunks: [Buffer.alloc(320, 1)], sent: 1 },
    ];
    p.pauseRetainedBytes = 320;

    p.startPauseResume();
    expect(p.pauseResumeIndex).toBe(1);
    vi.advanceTimersByTime(40);

    expect(p.outputPaused).toBe(false);
    expect(deps.bridge.sendAudio).not.toHaveBeenCalled();
    expect(p.turnSpokenSegments.map((s) => s.text)).toEqual(['Frase uno.']);
    p.clearGraceTimer();
  });

  it('resume re-arms the grace machinery for a post-complete turn', () => {
    const { p } = makeHandler({ bargeInConfirmMs: 30 });
    seedPartiallyHeardTurn(p);
    p.dispatchTask = null; // post-complete: no turn in flight
    p.turnOutputDone = true;

    p.startPauseResume();
    vi.advanceTimersByTime(40);
    try {
      // Resume must reschedule endSpeakingWithGrace for the re-sent backlog.
      expect(p.graceTimer).not.toBeNull();
      expect(p.isSpeaking).toBe(true);
    } finally {
      p.clearGraceTimer();
    }
  });
});

// ---------------------------------------------------------------------------
// KILL — a committed final transcript within the window runs the cancel path
// ---------------------------------------------------------------------------

describe('pause_resume — KILL on a confirming transcript', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('a committed final transcript kills the paused turn', () => {
    const { p } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);
    const overlapStart = vi.spyOn(p.metricsAcc, 'recordOverlapStart');
    const overlapEnd = vi.spyOn(p.metricsAcc, 'recordOverlapEnd');
    const bargeinDetected = vi.spyOn(p.metricsAcc, 'recordBargeinDetected');
    const turnInterrupted = vi.spyOn(p.metricsAcc, 'recordTurnInterrupted');
    p.startPauseResume();

    const confirmed = p.handleBargeIn({
      text: 'please stop talking now',
      isFinal: true,
      speechFinal: true,
    });

    expect(confirmed).toBe(true);
    expect(p.isSpeaking).toBe(false);
    expect(p.llmAbort!.signal.aborted).toBe(true);
    expect(p.outputPaused).toBe(false);
    // Paused buffers discarded — no replay may survive the kill.
    expect(p.turnSentenceAudio).toEqual([]);
    expect(p.pausedSentences).toEqual([]);
    expect(p.bargeInPendingSince).toBeNull();
    expect(p.bargeInPendingTimer).toBeNull();
    // Interruption metrics: overlap window anchored at PAUSE time was
    // preserved (started once) and closed as a real interruption.
    expect(overlapStart).toHaveBeenCalledTimes(1);
    expect(overlapEnd).toHaveBeenCalledTimes(1);
    expect(overlapEnd).toHaveBeenCalledWith(true);
    expect(bargeinDetected).toHaveBeenCalledTimes(1);
    expect(turnInterrupted).toHaveBeenCalledTimes(1);
  });

  it('a post-complete kill truncates history to the FROZEN heard prefix', () => {
    // The cursor was frozen at pause time, so the history rewrite must
    // still see the heard prefix (not skip on "no backlog").
    const { p } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);
    const full = 'Frase uno. Frase due. Frase tre.';
    p.history.entries.push({ role: 'assistant', text: full, timestamp: Date.now() });

    p.startPauseResume();
    p.handleBargeIn({ text: 'aspetta un attimo', isFinal: true, speechFinal: true });

    expect(p.history.entries[p.history.entries.length - 1].text).toBe(
      'Frase uno. Frase due. [interrupted by caller]',
    );
  });

  it('an interim transcript does NOT kill while paused', () => {
    const { p } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);
    p.startPauseResume();

    const confirmed = p.handleBargeIn({
      text: 'please stop',
      isFinal: false,
      speechFinal: false,
    });

    expect(confirmed).toBe(false);
    expect(p.isSpeaking).toBe(true);
    expect(p.outputPaused).toBe(true);
    expect(p.llmAbort!.signal.aborted).toBe(false);
    p.clearPendingBargeIn();
  });

  it('a known STT hallucination final does NOT kill while paused', () => {
    const { p } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);
    p.startPauseResume();

    // "Thanks for watching!" is in HALLUCINATIONS (Whisper caption bias)
    // — a paused turn must not be killed by it.
    const confirmed = p.handleBargeIn({
      text: 'Thanks for watching!',
      isFinal: true,
      speechFinal: true,
    });

    expect(confirmed).toBe(false);
    expect(p.isSpeaking).toBe(true);
    expect(p.outputPaused).toBe(true);
    p.clearPendingBargeIn();
  });

  it('a duplicate of the last committed utterance does NOT kill while paused', () => {
    const { p } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);
    p.lastCommitText = 'che ore sono';
    p.lastCommitAt = Date.now() - 300;
    p.startPauseResume();

    const confirmed = p.handleBargeIn({
      text: 'che ore sono',
      isFinal: true,
      speechFinal: true,
    });

    expect(confirmed).toBe(false);
    expect(p.isSpeaking).toBe(true);
    expect(p.outputPaused).toBe(true);
    p.clearPendingBargeIn();
  });

  it('legacy cancel mode is untouched by the final-only gate', () => {
    // In cancel mode (never paused) interim transcripts still cancel
    // immediately — byte-identical legacy contract.
    const { p } = makeHandler({ bargeInMode: 'cancel' });

    const confirmed = p.handleBargeIn({
      text: 'okay wait',
      isFinal: false,
      speechFinal: false,
    });

    expect(confirmed).toBe(true);
    expect(p.isSpeaking).toBe(false);
    expect(p.llmAbort!.signal.aborted).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Buffer overflow — degrade rather than grow without bound
// ---------------------------------------------------------------------------

describe('pause_resume — retained-audio overflow', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('overflow while paused degrades to a full cancel', () => {
    const { p } = makeHandler({ bargeInConfirmMs: 10_000 });
    const turnInterrupted = vi.spyOn(p.metricsAcc, 'recordTurnInterrupted');
    p.startPauseResume();
    const entry = p.beginRetainedSentence('Frase uno.');
    expect(entry).not.toBeNull();
    p.pauseRetainedBytes = p.pauseRetainedCapBytes(); // at the cap

    const retained = p.retainPauseChunk(entry!, Buffer.alloc(640, 0));

    expect(retained).toBe(false);
    expect(p.isSpeaking).toBe(false); // paused overflow must kill the turn
    expect(p.llmAbort!.signal.aborted).toBe(true);
    expect(p.outputPaused).toBe(false);
    expect(turnInterrupted).toHaveBeenCalledTimes(1);
  });

  it('overflow while speaking releases retention for the turn (no cancel)', () => {
    const { p } = makeHandler({ bargeInConfirmMs: 10_000 });
    const entry = p.beginRetainedSentence('Frase uno.');
    expect(entry).not.toBeNull();
    p.pauseRetainedBytes = p.pauseRetainedCapBytes();

    const retained = p.retainPauseChunk(entry!, Buffer.alloc(640, 0));

    expect(retained).toBe(false);
    expect(p.isSpeaking).toBe(true); // an un-paused overflow must NOT cancel
    expect(p.pauseResumeOverflowed).toBe(true);
    expect(entry!.chunks).toEqual([]);
    expect(p.pauseRetainedBytes).toBe(0);
    // With retention released, speech_start falls back to legacy cancel.
    expect(p.shouldPauseForBargeIn()).toBe(false);
  });

  it('the retained-audio cap scales with the wire format', () => {
    const { p } = makeHandler();
    expect(p.pauseRetainedCapBytes()).toBe(15 * 1000 * 32); // PCM16 @ 16 kHz
    p.ttsOutputFormatNativeForCarrier = true; // Twilio native μ-law 8 kHz
    expect(p.pauseRetainedCapBytes()).toBe(15 * 1000 * 8);
  });
});

// ---------------------------------------------------------------------------
// Streaming-loop integration — pause mid-stream, then resume or kill
// ---------------------------------------------------------------------------

/** Real handler whose runPipelineLlm runs the genuine loop; only the paid
 * edges (LLM stream / TTS / carrier) are stubbed. */
function makeStreamingHandler(
  confirmMs: number,
  tokens: () => AsyncGenerator<string>,
): { h: StreamHandler; p: Priv; deps: StreamHandlerDeps } {
  const made = makeHandler({ bargeInConfirmMs: confirmMs });
  const { p } = made;
  // runPipelineLlm owns beginSpeaking itself — undo the armed fixture state.
  p.isSpeaking = false;
  p.turnOutputDone = false;
  p.tts = {
    synthesizeStream: async function* () {
      yield Buffer.alloc(3200, 0); // 100 ms PCM16 @ 16 kHz per sentence
    },
  };
  p.llmLoop = {
    run: (..._args: unknown[]) => tokens(),
    setEventBus: vi.fn(),
  };
  return made;
}

describe('pause_resume — streaming-loop integration', () => {
  beforeEach(() => {
    vi.useRealTimers();
    process.env.PATTER_TTS_TAIL_GRACE_MS = '0';
  });
  afterEach(() => {
    delete process.env.PATTER_TTS_TAIL_GRACE_MS;
    vi.restoreAllMocks();
  });

  it('pause mid-stream then resume speaks the buffered sentences in order', async () => {
    let pausePoint: (() => void) | null = null;
    const made = makeStreamingHandler(50, async function* () {
      yield 'Frase uno. ';
      // Cough: VAD speech_start lands after sentence one went out.
      pausePoint!();
      yield 'Frase due. '; // buffered as text while paused
      yield 'Frase tre.';
    });
    const { p, deps } = made;
    pausePoint = () => p.startPauseResume();

    const result = await p.runPipelineLlm(
      'user text',
      new PipelineHookExecutor(undefined),
      p.buildHookContext(),
      [],
    );

    expect(result.interrupted).toBe(false);
    expect(result.text).toBe('Frase uno. Frase due. Frase tre.');
    // Sentence one was sent live; the resume replayed it (it had not
    // finished playing when the pause cleared the carrier) and then the
    // buffered sentences flowed through the normal synth path.
    expect(
      (deps.bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBeGreaterThanOrEqual(3);
    expect(p.outputPaused).toBe(false);
    expect(p.turnSpokenSegments.map((s) => s.text)).toEqual([
      'Frase uno.',
      'Frase due.',
      'Frase tre.',
    ]);
  });

  it('pause mid-stream then kill marks the turn interrupted', async () => {
    let killPoint: (() => void) | null = null;
    const made = makeStreamingHandler(10_000, async function* () {
      yield 'Frase uno. ';
      priv(made.h).startPauseResume();
      yield 'Frase due. ';
      // Confirming final transcript arrives while paused.
      killPoint!();
      yield 'Frase tre.';
    });
    const { p } = made;
    killPoint = () => {
      // Backdate the first-audio stamp so the anti-flicker gate (which only
      // exists to reject same-instant phantoms) is open, as it would be on
      // a real call by transcript time.
      p.firstAudioSentAt = Date.now() - 1000;
      p.handleBargeIn({
        text: 'no aspetta fermati',
        isFinal: true,
        speechFinal: true,
      });
    };

    const result = await p.runPipelineLlm(
      'user text',
      new PipelineHookExecutor(undefined),
      p.buildHookContext(),
      [],
    );

    // The ``[interrupted by caller]`` heard-prefix marker is applied by
    // dispatchTurn from this flag (covered by the truncation test above) —
    // the loop's contract is the interrupted signal plus clean state.
    expect(result.interrupted).toBe(true);
    expect(result.text).toBe('Frase uno. Frase due. ');
    expect(p.isSpeaking).toBe(false);
    expect(p.outputPaused).toBe(false);
    expect(p.turnSentenceAudio).toEqual([]);
    expect(p.pausedSentences).toEqual([]);
  });

  it('a stream ending while paused waits for the decision, then resumes', async () => {
    // The turn must not end while the pause decision is outstanding — the
    // flush-phase wait holds it until the resume timer fires, then the
    // buffered sentence is spoken.
    const made = makeStreamingHandler(50, async function* () {
      yield 'Frase uno. ';
      priv(made.h).startPauseResume();
      yield 'Frase due.';
      // Stream ends here, still paused.
    });
    const { p, deps } = made;

    const result = await p.runPipelineLlm(
      'user text',
      new PipelineHookExecutor(undefined),
      p.buildHookContext(),
      [],
    );

    expect(result.interrupted).toBe(false);
    expect(result.text).toBe('Frase uno. Frase due.');
    expect(p.outputPaused).toBe(false);
    expect(p.turnSpokenSegments.map((s) => s.text)).toEqual([
      'Frase uno.',
      'Frase due.',
    ]);
    expect(
      (deps.bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBeGreaterThanOrEqual(2);
  });
});

// ---------------------------------------------------------------------------
// Teardown — stop / WS close mid-pause leaks nothing
// ---------------------------------------------------------------------------

describe('pause_resume — teardown mid-pause', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('handleStop cancels the resume timer and discards the buffers', async () => {
    const { h, p, deps } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);
    const overlapEnd = vi.spyOn(p.metricsAcc, 'recordOverlapEnd');

    p.startPauseResume();
    expect(p.bargeInPendingTimer).not.toBeNull();
    overlapEnd.mockClear();
    (deps.bridge.sendAudio as ReturnType<typeof vi.fn>).mockClear();

    await h.handleStop();
    vi.advanceTimersByTime(20_000);

    expect(p.outputPaused).toBe(false);
    expect(p.turnSentenceAudio).toEqual([]);
    expect(p.bargeInPendingTimer).toBeNull();
    // No stale resume fired after teardown: nothing re-sent, no
    // overlap_end on a finalised metrics object.
    expect(deps.bridge.sendAudio).not.toHaveBeenCalled();
    expect(overlapEnd).not.toHaveBeenCalled();
  });

  it('handleWsClose cancels the resume timer and discards the buffers', async () => {
    const { h, p, deps } = makeHandler({ bargeInConfirmMs: 10_000 });
    seedPartiallyHeardTurn(p);
    const overlapEnd = vi.spyOn(p.metricsAcc, 'recordOverlapEnd');

    p.startPauseResume();
    overlapEnd.mockClear();
    (deps.bridge.sendAudio as ReturnType<typeof vi.fn>).mockClear();

    await h.handleWsClose();
    vi.advanceTimersByTime(20_000);

    expect(p.outputPaused).toBe(false);
    expect(p.turnSentenceAudio).toEqual([]);
    expect(p.bargeInPendingTimer).toBeNull();
    expect(deps.bridge.sendAudio).not.toHaveBeenCalled();
    expect(overlapEnd).not.toHaveBeenCalled();
  });
});

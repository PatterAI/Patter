/**
 * Two-stage barge-in regression tests.
 *
 * Mirrors the Python ``test_barge_in_two_stage.py`` so the cross-SDK
 * behaviour stays in lockstep:
 *
 * - sub-threshold transcripts during agent speech do not cancel
 * - threshold-meeting transcripts confirm and run the cancel path
 * - empty strategies preserve the legacy "cancel on first transcript"
 *   behaviour byte-for-byte
 * - VAD speech_start with strategies marks pending (no cancel yet) and
 *   the timeout drops pending state without flipping isSpeaking
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { MinWordsStrategy } from '../../src/services/barge-in-strategies';
import type { BargeInStrategy } from '../../src/services/barge-in-strategies';
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

function makeDeps(
  bargeInStrategies: readonly BargeInStrategy[] = [],
  bargeInConfirmMs?: number,
): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-oai-key' },
    agent: {
      systemPrompt: 'Test agent',
      provider: 'pipeline',
      bargeInStrategies,
      bargeInConfirmMs,
    },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn(),
    sanitizeVariables: vi.fn((raw) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl) => tpl),
  };
}

interface Priv {
  isSpeaking: boolean;
  speakingStartedAt: number | null;
  firstAudioSentAt: number | null;
  bargeInPendingSince: number | null;
  bargeInPendingTimer: ReturnType<typeof setTimeout> | null;
  llmAbort: AbortController | null;
  handleBargeIn: (t: { text?: string; isFinal?: boolean }) => boolean;
  handleBargeInAsync: (t: { text?: string; isFinal?: boolean }) => Promise<boolean>;
  startPendingBargeIn: () => void;
  clearPendingBargeIn: () => void;
}

function priv(h: StreamHandler): Priv {
  return h as unknown as Priv;
}

function armSpeakingState(h: StreamHandler): void {
  // Simulate "agent has been speaking for >1 s and the first chunk
  // already hit the wire" so the canBargeIn gate is open.
  const p = priv(h);
  p.isSpeaking = true;
  p.speakingStartedAt = Date.now() - 1500;
  p.firstAudioSentAt = Date.now() - 1500;
  p.llmAbort = new AbortController();
}

describe('StreamHandler — opt-in barge-in confirmation', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('legacy path (no strategies) cancels immediately on any transcript while speaking', () => {
    const deps = makeDeps([]);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);
    const result = priv(h).handleBargeIn({ text: 'okay', isFinal: true });
    expect(result).toBe(true);
    expect(priv(h).isSpeaking).toBe(false);
  });

  it('sub-threshold transcript does NOT cancel when MinWordsStrategy is configured', async () => {
    const deps = makeDeps([new MinWordsStrategy({ minWords: 3 })]);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);
    const confirmed = await priv(h).handleBargeInAsync({
      text: 'okay',
      isFinal: true,
    });
    expect(confirmed).toBe(false);
    expect(priv(h).isSpeaking).toBe(true);
    // No cancel was issued — sendClear must not have been called for
    // this attempted barge-in.
    expect(deps.bridge.sendClear).not.toHaveBeenCalled();
  });

  it('threshold-meeting transcript confirms and runs the cancel path', async () => {
    const deps = makeDeps([new MinWordsStrategy({ minWords: 3 })]);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);
    const confirmed = await priv(h).handleBargeInAsync({
      text: 'please stop talking now',
      isFinal: true,
    });
    expect(confirmed).toBe(true);
    expect(priv(h).isSpeaking).toBe(false);
    expect(deps.bridge.sendClear).toHaveBeenCalledTimes(1);
  });

  it('startPendingBargeIn marks pending without cancelling and sets a timer', () => {
    const deps = makeDeps([new MinWordsStrategy({ minWords: 3 })], 800);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);
    priv(h).startPendingBargeIn();
    expect(priv(h).bargeInPendingSince).not.toBeNull();
    expect(priv(h).bargeInPendingTimer).not.toBeNull();
    // Crucially, no cancel was issued just because VAD fired.
    expect(priv(h).isSpeaking).toBe(true);
    expect(deps.bridge.sendClear).not.toHaveBeenCalled();
  });

  it('pending barge-in times out and drops pending state (agent keeps speaking)', () => {
    const deps = makeDeps([new MinWordsStrategy({ minWords: 3 })], 50);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);
    priv(h).startPendingBargeIn();
    expect(priv(h).bargeInPendingSince).not.toBeNull();
    // Advance virtual time past the timeout.
    vi.advanceTimersByTime(60);
    expect(priv(h).bargeInPendingSince).toBeNull();
    expect(priv(h).bargeInPendingTimer).toBeNull();
    // Agent never got cancelled — that's the whole point of the
    // confirmation pipeline.
    expect(priv(h).isSpeaking).toBe(true);
    expect(deps.bridge.sendClear).not.toHaveBeenCalled();
  });

  it('confirmation clears pending state', async () => {
    const deps = makeDeps([new MinWordsStrategy({ minWords: 2 })], 10_000);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);
    priv(h).startPendingBargeIn();
    expect(priv(h).bargeInPendingSince).not.toBeNull();

    const confirmed = await priv(h).handleBargeInAsync({
      text: 'please stop',
      isFinal: true,
    });
    expect(confirmed).toBe(true);
    expect(priv(h).bargeInPendingSince).toBeNull();
    expect(priv(h).bargeInPendingTimer).toBeNull();
    expect(priv(h).isSpeaking).toBe(false);
  });

  it('startPendingBargeIn is idempotent within a turn', () => {
    const deps = makeDeps([new MinWordsStrategy({ minWords: 3 })], 10_000);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);
    priv(h).startPendingBargeIn();
    const firstSince = priv(h).bargeInPendingSince;
    const firstTimer = priv(h).bargeInPendingTimer;
    priv(h).startPendingBargeIn();
    expect(priv(h).bargeInPendingSince).toBe(firstSince);
    expect(priv(h).bargeInPendingTimer).toBe(firstTimer);
  });
});

describe('StreamHandler — overlap window preserved across VAD → strategy confirm (FIX #88)', () => {
  it('strategy-confirmed cancel does NOT restart the overlap window', async () => {
    // Use real timers for this test — we need real time to elapse so
    // detectionDelay reflects the VAD→confirm window.
    vi.useRealTimers();
    const deps = makeDeps([new MinWordsStrategy({ minWords: 3 })], 10_000);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);

    // Stage 1: VAD fires speech_start → pending. Records overlap_start (T1).
    priv(h).startPendingBargeIn();
    expect(priv(h).bargeInPendingSince).not.toBeNull();

    // Wait ~150ms so that if T1 is preserved, detectionDelay >= 150 ms;
    // if T1 is overwritten by the cancel path, detectionDelay ≈ 0.
    await new Promise((r) => setTimeout(r, 150));

    // Subscribe to the metrics event bus on the handler to observe the
    // emitted InterruptionMetrics payload.
    interface PrivWithMetrics {
      metricsAcc: { attachEventBus: (b: unknown) => void };
    }
    const { EventBus } = await import('../../src/observability/event-bus');
    const bus = new EventBus();
    const captured: { detectionDelay: number }[] = [];
    bus.on('interruption', (p: unknown) => {
      captured.push(p as { detectionDelay: number });
    });
    (h as unknown as PrivWithMetrics).metricsAcc.attachEventBus(bus);

    // Stage 2: STT delivers a confirming transcript NOW (T2).
    const confirmed = await priv(h).handleBargeInAsync({
      text: 'please stop talking now',
      isFinal: true,
    });
    expect(confirmed).toBe(true);
    expect(priv(h).isSpeaking).toBe(false);

    // Exactly one InterruptionMetrics emission. detectionDelay must
    // reflect the VAD→confirm window (~150 ms), NOT ~0.
    expect(captured.length).toBe(1);
    expect(captured[0].detectionDelay).toBeGreaterThanOrEqual(100);
    expect(captured[0].detectionDelay).toBeLessThanOrEqual(800);
  });

  it('legacy path (no strategies) records overlap_start once', () => {
    const deps = makeDeps([]);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);

    interface PrivMetrics {
      metricsAcc: { recordOverlapStart: () => void; recordOverlapEnd: (b: boolean) => void };
    }
    const acc = (h as unknown as PrivMetrics).metricsAcc;
    const startSpy = vi.spyOn(acc, 'recordOverlapStart');
    const endSpy = vi.spyOn(acc, 'recordOverlapEnd');

    const result = priv(h).handleBargeIn({ text: 'okay', isFinal: true });
    expect(result).toBe(true);
    // Without VAD pending, the cancel path is the SOLE caller of
    // recordOverlapStart — exactly once.
    expect(startSpy).toHaveBeenCalledTimes(1);
    expect(endSpy).toHaveBeenCalledTimes(1);
  });
});

describe('StreamHandler — handleStop / handleWsClose drops pending barge-in timer (FIX #89)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('handleStop cancels a pending barge-in timer so it cannot fire later', async () => {
    const deps = makeDeps([new MinWordsStrategy({ minWords: 3 })], 1_500);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);

    interface PrivMetrics {
      metricsAcc: { recordOverlapEnd: (b: boolean) => void };
    }
    const acc = (h as unknown as PrivMetrics).metricsAcc;
    const endSpy = vi.spyOn(acc, 'recordOverlapEnd');

    priv(h).startPendingBargeIn();
    expect(priv(h).bargeInPendingTimer).not.toBeNull();
    expect(priv(h).bargeInPendingSince).not.toBeNull();
    // Reset the spy after startPendingBargeIn (which doesn't call end,
    // but be defensive).
    endSpy.mockClear();

    await h.handleStop();

    // Pending state cleared; timer cancelled.
    expect(priv(h).bargeInPendingSince).toBeNull();
    expect(priv(h).bargeInPendingTimer).toBeNull();

    // Advance past when the timeout would have fired. If the timer
    // wasn't cancelled, recordOverlapEnd would fire here on a torn-down
    // metrics object — that's the regression.
    vi.advanceTimersByTime(2_000);
    expect(endSpy).not.toHaveBeenCalled();
  });

  it('handleWsClose cancels a pending barge-in timer', async () => {
    const deps = makeDeps([new MinWordsStrategy({ minWords: 3 })], 1_500);
    const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    armSpeakingState(h);

    interface PrivMetrics {
      metricsAcc: { recordOverlapEnd: (b: boolean) => void };
    }
    const acc = (h as unknown as PrivMetrics).metricsAcc;
    const endSpy = vi.spyOn(acc, 'recordOverlapEnd');

    priv(h).startPendingBargeIn();
    expect(priv(h).bargeInPendingTimer).not.toBeNull();
    endSpy.mockClear();

    await h.handleWsClose();

    expect(priv(h).bargeInPendingSince).toBeNull();
    expect(priv(h).bargeInPendingTimer).toBeNull();
    vi.advanceTimersByTime(2_000);
    expect(endSpy).not.toHaveBeenCalled();
  });
});

describe('MinWordsStrategy threshold parity (TS↔Py)', () => {
  it.each([2, 3, 5])(
    'agent stays talking below threshold and cancels at threshold (minWords=%i)',
    async (n: number) => {
      const deps = makeDeps([new MinWordsStrategy({ minWords: n })]);
      const h = new StreamHandler(deps, makeMockWs(), '+1', '+2');
      armSpeakingState(h);

      const below = Array.from({ length: n - 1 }, () => 'word').join(' ');
      const at = Array.from({ length: n }, () => 'word').join(' ');

      // Below threshold — keep talking.
      let confirmed = await priv(h).handleBargeInAsync({
        text: below,
        isFinal: true,
      });
      expect(confirmed).toBe(false);
      expect(priv(h).isSpeaking).toBe(true);

      // At threshold — confirm.
      confirmed = await priv(h).handleBargeInAsync({
        text: at,
        isFinal: true,
      });
      expect(confirmed).toBe(true);
      expect(priv(h).isSpeaking).toBe(false);
    },
  );
});

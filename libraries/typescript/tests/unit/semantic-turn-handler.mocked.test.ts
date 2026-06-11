/**
 * Pipeline-handler integration tests for semantic turn detection.
 *
 * Mirrors the Python `test_semantic_turn_handler.py` so cross-SDK behaviour
 * stays in lockstep. Exercises `StreamHandler.handleAudio`'s opt-in
 * `agent.turnDetector` wiring with a fake detector (scripted probabilities)
 * and a scripted VAD — no ONNX, no network:
 *
 *  - default path (no detector): VAD speech_end finalizes STT immediately
 *  - detector ≥ threshold: finalize once + EOU trigger stamped
 *    `semantic_turn_detector`
 *  - detector < threshold: hold, re-poll each ~200 ms silence window
 *  - speech_start during a hold cancels it (no finalize)
 *  - hard cap `maxSemanticHoldMs` force-finalizes (frame-driven and
 *    wall-clock backstop variants) with the `vad_silence` trigger
 *  - raising detector fails OPEN
 *  - rolling window bounded to 8 s
 *  - transcript commit fires `fireUserSpeechEos` with the stamped trigger
 */
import { describe, it, expect, vi } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { SpeechEvents } from '../../src/_speech-events';
import { getLogger, setLogger, type Logger } from '../../src/logger';
import type { TurnDetectorProvider, VADEvent, VADProvider } from '../../src/types';
import type { WebSocket as WSWebSocket } from 'ws';

// ---------------------------------------------------------------------------
// Fakes / helpers
// ---------------------------------------------------------------------------

class FakeTurnDetector implements TurnDetectorProvider {
  readonly windows: Buffer[] = [];
  /** Prediction attempts — counted even when predict throws. */
  attempts = 0;
  private readonly probs: number[];

  constructor(
    probs: number[],
    readonly threshold: number = 0.5,
    private readonly raiseOnCall = false,
  ) {
    this.probs = [...probs];
  }

  async predict(pcm16Window: Buffer): Promise<number> {
    this.attempts += 1;
    if (this.raiseOnCall) throw new Error('model exploded');
    this.windows.push(pcm16Window);
    if (this.probs.length > 1) return this.probs.shift()!;
    return this.probs[0];
  }

  async close(): Promise<void> {
    /* no-op */
  }
}

/** Returns one scripted event (or null) per processFrame call. */
class ScriptedVAD implements VADProvider {
  private readonly events: Array<VADEvent | null>;

  constructor(events: Array<VADEvent | null>) {
    this.events = [...events];
  }

  async processFrame(): Promise<VADEvent | null> {
    return this.events.length > 0 ? (this.events.shift() ?? null) : null;
  }

  async close(): Promise<void> {
    /* no-op */
  }
}

const speechEnd = (): VADEvent => ({ type: 'speech_end', confidence: 0.1, durationMs: 400 });
const speechStart = (): VADEvent => ({ type: 'speech_start', confidence: 0.9, durationMs: 250 });

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

interface BuildOptions {
  readonly vadEvents?: Array<VADEvent | null>;
  readonly turnDetector?: TurnDetectorProvider;
  readonly maxSemanticHoldMs?: number;
  readonly speechEvents?: SpeechEvents;
}

interface SttStub {
  sendAudio: ReturnType<typeof vi.fn>;
  finalize: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
}

interface Priv {
  semanticHoldActive: boolean;
  semanticHoldTimer: ReturnType<typeof setTimeout> | null;
  semanticAudioRingBytes: number;
  lastEouTrigger: string;
  turnDetectorFailed: boolean;
  semanticWindowBytes(): Buffer;
  cancelSemanticHold(): void;
  processTranscript(t: { text: string; isFinal: boolean; speechFinal?: boolean }): Promise<void>;
}

function buildHandler(opts: BuildOptions = {}): {
  handler: StreamHandler;
  stt: SttStub;
  priv: Priv;
} {
  const deps: StreamHandlerDeps = {
    config: { openaiKey: 'test-oai-key' },
    agent: {
      systemPrompt: 'Test agent',
      provider: 'pipeline',
      vad: new ScriptedVAD(opts.vadEvents ?? []),
      turnDetector: opts.turnDetector,
      maxSemanticHoldMs: opts.maxSemanticHoldMs,
    },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn(),
    // Observer onMessage so dispatchTurn never reaches the built-in LLM
    // (no network in unit tests).
    onMessage: vi.fn(async () => ''),
    speechEvents: opts.speechEvents,
    sanitizeVariables: vi.fn((raw) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl) => tpl),
  };
  const handler = new StreamHandler(deps, makeMockWs(), '+15551110000', '+15552220000');
  const stt: SttStub = {
    sendAudio: vi.fn(),
    finalize: vi.fn(),
    close: vi.fn().mockResolvedValue(undefined),
  };
  (handler as unknown as { stt: SttStub }).stt = stt;
  return { handler, stt, priv: handler as unknown as Priv };
}

/** 20 ms of mulaw 8 kHz silence — decodes/resamples to 640 B PCM16 @ 16 kHz. */
function mulawFrame(): Buffer {
  return Buffer.alloc(160, 0xff);
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------------------
// 1. Default path — no detector configured
// ---------------------------------------------------------------------------

describe('StreamHandler — semantic turn detection (opt-in)', () => {
  it('finalizes immediately on speech_end when no detector is configured', async () => {
    const { handler, stt, priv } = buildHandler({ vadEvents: [speechEnd()] });
    await handler.handleAudio(mulawFrame());
    expect(stt.finalize).toHaveBeenCalledTimes(1);
    expect(priv.lastEouTrigger).toBe('vad_silence');
    expect(priv.semanticHoldActive).toBe(false);
  });

  it('keeps the rolling buffer empty when no detector is configured', async () => {
    const { handler, priv } = buildHandler({ vadEvents: [null, null] });
    await handler.handleAudio(mulawFrame());
    await handler.handleAudio(mulawFrame());
    expect(priv.semanticAudioRingBytes).toBe(0);
  });

  // -------------------------------------------------------------------------
  // 2. Semantic approve on the speech_end edge
  // -------------------------------------------------------------------------

  it('finalizes with the semantic trigger when probability >= threshold', async () => {
    const detector = new FakeTurnDetector([0.9]);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [null, speechEnd()],
      turnDetector: detector,
    });

    await handler.handleAudio(mulawFrame()); // buffered, no VAD event
    expect(stt.finalize).not.toHaveBeenCalled();
    await handler.handleAudio(mulawFrame()); // speech_end → predict → finalize

    expect(stt.finalize).toHaveBeenCalledTimes(1);
    expect(priv.lastEouTrigger).toBe('semantic_turn_detector');
    expect(priv.semanticHoldActive).toBe(false);
    // The detector saw the rolling window accumulated so far (2 frames —
    // the stateful 8k→16k resampler emits a few samples less on its very
    // first chunk while its filter state warms up).
    expect(detector.windows).toHaveLength(1);
    expect(detector.windows[0].length).toBeGreaterThanOrEqual(2 * 640 - 8);
    expect(detector.windows[0].length).toBeLessThanOrEqual(2 * 640);
  });

  it('treats probability exactly at the threshold as complete', async () => {
    const detector = new FakeTurnDetector([0.5], 0.5);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd()],
      turnDetector: detector,
    });
    await handler.handleAudio(mulawFrame());
    expect(stt.finalize).toHaveBeenCalledTimes(1);
    expect(priv.lastEouTrigger).toBe('semantic_turn_detector');
  });

  // -------------------------------------------------------------------------
  // 3. Hold + re-poll on subsequent silence windows
  // -------------------------------------------------------------------------

  it('holds below threshold, then finalizes on a re-poll that crosses it', async () => {
    const detector = new FakeTurnDetector([0.2, 0.9]);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd(), ...Array<null>(30).fill(null)],
      turnDetector: detector,
    });

    await handler.handleAudio(mulawFrame()); // speech_end → p=0.2 → hold
    expect(stt.finalize).not.toHaveBeenCalled();
    expect(priv.semanticHoldActive).toBe(true);

    // 9 more silent frames = 180 ms < 200 ms poll window → still holding.
    for (let i = 0; i < 9; i++) await handler.handleAudio(mulawFrame());
    expect(stt.finalize).not.toHaveBeenCalled();
    expect(detector.windows).toHaveLength(1);

    // 10th frame crosses the 200 ms silence window → re-poll → p=0.9.
    await handler.handleAudio(mulawFrame());
    expect(stt.finalize).toHaveBeenCalledTimes(1);
    expect(detector.windows).toHaveLength(2);
    expect(priv.lastEouTrigger).toBe('semantic_turn_detector');
    expect(priv.semanticHoldActive).toBe(false);
    expect(priv.semanticHoldTimer).toBeNull();
  });

  it('keeps extending while every re-poll stays below threshold', async () => {
    const detector = new FakeTurnDetector([0.2, 0.3, 0.25]);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd(), ...Array<null>(40).fill(null)],
      turnDetector: detector,
    });

    await handler.handleAudio(mulawFrame());
    for (let i = 0; i < 20; i++) await handler.handleAudio(mulawFrame());

    expect(stt.finalize).not.toHaveBeenCalled();
    expect(priv.semanticHoldActive).toBe(true);
    expect(detector.windows).toHaveLength(3); // initial + 2 re-polls
    priv.cancelSemanticHold(); // drop the live backstop timer for teardown
  });

  // -------------------------------------------------------------------------
  // 4. speech_start cancels the hold (user resumed)
  // -------------------------------------------------------------------------

  it('cancels the hold without finalizing when the user resumes speaking', async () => {
    const detector = new FakeTurnDetector([0.1]);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd(), speechStart()],
      turnDetector: detector,
    });

    await handler.handleAudio(mulawFrame()); // hold begins
    expect(priv.semanticHoldActive).toBe(true);

    await handler.handleAudio(mulawFrame()); // user resumed speaking
    expect(priv.semanticHoldActive).toBe(false);
    expect(priv.semanticHoldTimer).toBeNull();
    expect(stt.finalize).not.toHaveBeenCalled();
  });

  // -------------------------------------------------------------------------
  // 5. Hard cap — the turn can never hang
  // -------------------------------------------------------------------------

  it('finalizes with vad_silence at the cap via the frame path', async () => {
    const detector = new FakeTurnDetector([0.1]);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd(), ...Array<null>(10).fill(null)],
      turnDetector: detector,
      maxSemanticHoldMs: 1,
    });

    await handler.handleAudio(mulawFrame()); // hold begins (cap = 1 ms)
    await sleep(20); // let the deadline lapse (the backstop may also fire)
    await handler.handleAudio(mulawFrame()); // frame path observes the cap

    expect(stt.finalize).toHaveBeenCalledTimes(1);
    expect(priv.lastEouTrigger).toBe('vad_silence');
    expect(priv.semanticHoldActive).toBe(false);
  });

  it('wall-clock backstop finalizes at the cap even with no further frames', async () => {
    const detector = new FakeTurnDetector([0.1]);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd()],
      turnDetector: detector,
      maxSemanticHoldMs: 20,
    });

    await handler.handleAudio(mulawFrame()); // hold begins; audio stalls
    expect(stt.finalize).not.toHaveBeenCalled();
    await sleep(60);

    expect(stt.finalize).toHaveBeenCalledTimes(1);
    expect(priv.lastEouTrigger).toBe('vad_silence');
    expect(priv.semanticHoldActive).toBe(false);
  });

  it('finalizes exactly once when the backstop and the frame path race', async () => {
    const detector = new FakeTurnDetector([0.1]);
    const { handler, stt } = buildHandler({
      vadEvents: [speechEnd(), ...Array<null>(30).fill(null)],
      turnDetector: detector,
      maxSemanticHoldMs: 10,
    });

    await handler.handleAudio(mulawFrame());
    await sleep(40); // backstop fires
    for (let i = 0; i < 15; i++) await handler.handleAudio(mulawFrame());

    expect(stt.finalize).toHaveBeenCalledTimes(1);
  });

  // -------------------------------------------------------------------------
  // 6. Detector failure fails OPEN
  // -------------------------------------------------------------------------

  it('falls back to an immediate vad_silence finalize when predict throws', async () => {
    const detector = new FakeTurnDetector([0.9], 0.5, true);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd()],
      turnDetector: detector,
    });
    await handler.handleAudio(mulawFrame());
    expect(stt.finalize).toHaveBeenCalledTimes(1);
    expect(priv.lastEouTrigger).toBe('vad_silence');
    expect(priv.semanticHoldActive).toBe(false);
  });

  it('disables the detector for the rest of the call after a failure (single warning)', async () => {
    // Fail ONCE: a broken runtime (e.g. onnxruntime-node missing or
    // incompatible at call time) must not warn per turn nor be retried on
    // every speech_end. Mirrors Python
    // `test_predict_failure_disables_detector_for_rest_of_call`.
    const detector = new FakeTurnDetector([0.9], 0.5, true);
    const warn = vi.fn();
    const originalLogger = getLogger();
    const spyLogger: Logger = { info: vi.fn(), warn, error: vi.fn(), debug: vi.fn() };
    setLogger(spyLogger);
    try {
      const { handler, stt, priv } = buildHandler({
        vadEvents: [speechEnd(), null, speechEnd()],
        turnDetector: detector,
      });

      await handler.handleAudio(mulawFrame()); // predict throws → warn + disable
      await handler.handleAudio(mulawFrame()); // plain frame — no detector calls
      await handler.handleAudio(mulawFrame()); // second speech_end → legacy path

      expect(detector.attempts).toBe(1); // never consulted again
      expect(priv.turnDetectorFailed).toBe(true);
      expect(stt.finalize).toHaveBeenCalledTimes(2); // both turns finalized
      expect(priv.lastEouTrigger).toBe('vad_silence');
      // Disabled ⇒ the rolling window is released and stops accumulating.
      expect(priv.semanticAudioRingBytes).toBe(0);
      const semanticWarnings = warn.mock.calls.filter((c) =>
        String(c[0]).includes('Semantic turn detector failed'),
      );
      expect(semanticWarnings).toHaveLength(1);
      expect(String(semanticWarnings[0][0])).toContain('disabling it for this call');
    } finally {
      setLogger(originalLogger);
    }
  });

  // -------------------------------------------------------------------------
  // 7. Rolling window stays bounded
  // -------------------------------------------------------------------------

  it('bounds the rolling audio window to 8 s of PCM16 @ 16 kHz', async () => {
    const detector = new FakeTurnDetector([0.9]);
    const { handler, priv } = buildHandler({
      vadEvents: Array<null>(500).fill(null),
      turnDetector: detector,
    });

    for (let i = 0; i < 500; i++) await handler.handleAudio(mulawFrame()); // 10 s

    const maxBytes = 16000 * 2 * 8;
    expect(priv.semanticAudioRingBytes).toBeLessThanOrEqual(maxBytes);
    expect(priv.semanticWindowBytes().length).toBe(priv.semanticAudioRingBytes);
    // 8 s of 20 ms frames (640 B PCM16-16k each) = 400 frames retained.
    expect(priv.semanticAudioRingBytes).toBe(400 * 640);
  });

  // -------------------------------------------------------------------------
  // 8. EOU trigger stamped on the committed turn (speech events)
  // -------------------------------------------------------------------------

  it('fires fireUserSpeechEos with the semantic trigger on transcript commit, then resets', async () => {
    const detector = new FakeTurnDetector([0.9]);
    const speechEvents = new SpeechEvents();
    const captured: Array<Record<string, unknown>> = [];
    speechEvents.onUserSpeechEos = (payload) => {
      captured.push(payload);
    };
    const { handler, priv } = buildHandler({
      vadEvents: [speechEnd()],
      turnDetector: detector,
      speechEvents,
    });

    await handler.handleAudio(mulawFrame()); // semantic finalize stamps trigger
    expect(priv.lastEouTrigger).toBe('semantic_turn_detector');

    await priv.processTranscript({ text: 'okay that is everything', isFinal: true });

    expect(captured).toHaveLength(1);
    expect(captured[0].trigger).toBe('semantic_turn_detector');
    expect(captured[0].transcript_so_far).toBe('okay that is everything');
    // Consume-once semantics: the stamp resets for the next turn …
    expect(priv.lastEouTrigger).toBe('vad_silence');
    // … and the per-turn rolling window restarts.
    expect(priv.semanticAudioRingBytes).toBe(0);
  });

  it('stamps vad_silence when the model never agreed (cap commit)', async () => {
    const detector = new FakeTurnDetector([0.1]);
    const speechEvents = new SpeechEvents();
    const captured: Array<Record<string, unknown>> = [];
    speechEvents.onUserSpeechEos = (payload) => {
      captured.push(payload);
    };
    const { handler, priv } = buildHandler({
      vadEvents: [speechEnd()],
      turnDetector: detector,
      maxSemanticHoldMs: 10,
      speechEvents,
    });

    await handler.handleAudio(mulawFrame()); // hold begins
    await sleep(40); // backstop cap → vad_silence finalize
    await priv.processTranscript({ text: 'hello?', isFinal: true });

    expect(captured).toHaveLength(1);
    expect(captured[0].trigger).toBe('vad_silence');
  });

  it('fires EOS with vad_silence on the default path (no detector)', async () => {
    // Pipeline mode emits the committed-EOS speech event unconditionally
    // (wired alongside the other speech edges); without a detector the
    // trigger reflects the active VAD's silence endpointing — never the
    // semantic trigger.
    const speechEvents = new SpeechEvents();
    const captured: Array<Record<string, unknown>> = [];
    speechEvents.onUserSpeechEos = (payload) => {
      captured.push(payload);
    };
    const { handler, priv } = buildHandler({
      vadEvents: [speechEnd()],
      speechEvents,
    });

    await handler.handleAudio(mulawFrame());
    await priv.processTranscript({ text: 'plain pipeline turn', isFinal: true });

    expect(captured).toHaveLength(1);
    expect(captured[0].trigger).toBe('vad_silence');
  });

  it('a committed transcript supersedes an active hold', async () => {
    const detector = new FakeTurnDetector([0.1]);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd()],
      turnDetector: detector,
      maxSemanticHoldMs: 60_000,
    });

    await handler.handleAudio(mulawFrame()); // hold begins (cap far away)
    expect(priv.semanticHoldActive).toBe(true);

    await priv.processTranscript({ text: 'already done', isFinal: true });

    expect(priv.semanticHoldActive).toBe(false);
    expect(priv.semanticHoldTimer).toBeNull();
    expect(stt.finalize).not.toHaveBeenCalled();
  });

  // -------------------------------------------------------------------------
  // Teardown safety
  // -------------------------------------------------------------------------

  it('handleStop drops an active hold so the backstop cannot fire later', async () => {
    const detector = new FakeTurnDetector([0.1]);
    const { handler, stt, priv } = buildHandler({
      vadEvents: [speechEnd()],
      turnDetector: detector,
      maxSemanticHoldMs: 30,
    });

    await handler.handleAudio(mulawFrame());
    expect(priv.semanticHoldActive).toBe(true);

    await handler.handleStop();
    expect(priv.semanticHoldActive).toBe(false);
    expect(priv.semanticHoldTimer).toBeNull();

    await sleep(60); // past the would-be cap
    expect(stt.finalize).not.toHaveBeenCalled();
  });
});

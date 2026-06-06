/**
 * [mocked] Multi-turn turn-taking — tail-grace new-turn rescue (parity with
 * Python ``test_pipeline_multiturn_tail_grace.py``).
 *
 * Reproduces the live-call failure where the FIRST turn works but every
 * SUBSEQUENT turn goes silent with a ghost ``[interrupted]`` metrics turn.
 *
 * Root cause: after the agent finishes, ``endSpeakingWithGrace`` keeps
 * ``isSpeaking=true`` for ``PATTER_TTS_TAIL_GRACE_MS`` (default 1500 ms) to
 * swallow the fading echo tail. Humans reply in 200-700 ms — inside that
 * window — so the user's next utterance was mis-detected as a barge-in
 * (``recordTurnInterrupted`` + leading audio withheld from STT), so no
 * transcript was produced and the agent never answered. The fix treats a VAD
 * ``speech_start`` (or a transcript) during the tail grace as a NEW turn.
 *
 * AUTHENTIC: the real StreamHandler + CallMetricsAccumulator. Mocked only at
 * the external boundary (telephony bridge, STT adapter). Private state/methods
 * are exercised via casts — the same surface the audio/STT loops drive.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { WebSocket as WSWebSocket } from 'ws';
import type { AgentOptions } from '../src/types';

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

function makeBridge(): TelephonyBridge {
  return {
    label: 'Twilio',
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

function makeDeps(bridge: TelephonyBridge, store: MetricsStore): StreamHandlerDeps {
  const agent: AgentOptions = {
    systemPrompt: 'You are a helpful test agent.',
    provider: 'pipeline',
    model: 'gpt-4o-mini',
    voice: 'alloy',
  };
  return {
    config: {},
    agent,
    bridge,
    metricsStore: store,
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(null),
    sanitizeVariables: vi.fn((raw: Record<string, unknown>) => raw),
    resolveVariables: vi.fn((tpl: string) => tpl),
  } as unknown as StreamHandlerDeps;
}

function makeHandler(): StreamHandler {
  const handler = new StreamHandler(
    makeDeps(makeBridge(), new MetricsStore()),
    makeMockWs(),
    '+15551110000',
    '+15552220000',
  );
  handler.setStreamSid('MZ-multiturn');
  return handler;
}

const FRAME = Buffer.from([0, 1, 0, 1, 0, 1]);

describe('[mocked] pipeline multi-turn tail-grace rescue', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('endSpeakingWithGrace flags the tail grace, the grace timer clears it', () => {
    vi.useFakeTimers();
    const h = makeHandler() as unknown as {
      isSpeaking: boolean;
      tailGraceActive: boolean;
      endSpeakingWithGrace(): void;
    };
    h.isSpeaking = true;
    h.endSpeakingWithGrace();
    // Grace pending: still "speaking" but flagged as tail grace.
    expect(h.isSpeaking).toBe(true);
    expect(h.tailGraceActive).toBe(true);

    vi.advanceTimersByTime(1600); // > 1500 ms default grace
    expect(h.isSpeaking).toBe(false);
    expect(h.tailGraceActive).toBe(false);
  });

  it('beginSpeaking clears the tail-grace flag', async () => {
    const h = makeHandler();
    const priv = h as unknown as { tailGraceActive: boolean; isSpeaking: boolean };
    priv.tailGraceActive = true;
    await (h as unknown as { beginSpeaking(): Promise<void> }).beginSpeaking();
    expect(priv.isSpeaking).toBe(true);
    expect(priv.tailGraceActive).toBe(false);
  });

  it('a transcript during the tail grace is a new turn, not a barge-in', () => {
    const bridge = makeBridge();
    const handler = new StreamHandler(
      makeDeps(bridge, new MetricsStore()),
      makeMockWs(),
      '+15551110000',
      '+15552220000',
    );
    const sttSendAudio = vi.fn();
    const priv = handler as unknown as {
      isSpeaking: boolean;
      tailGraceActive: boolean;
      speakingStartedAt: number | null;
      firstAudioSentAt: number | null;
      inboundAudioRing: Buffer[];
      stt: unknown;
      handleBargeIn(t: { text?: string; isFinal?: boolean }): boolean;
    };
    priv.stt = { sendAudio: sttSendAudio, finalize: vi.fn() };
    priv.isSpeaking = true;
    priv.tailGraceActive = true;
    priv.speakingStartedAt = Date.now() - 2000;
    priv.firstAudioSentAt = Date.now() - 2000;
    priv.inboundAudioRing = [FRAME, FRAME];

    const interrupted = priv.handleBargeIn({ text: 'che ore sono', isFinal: true });

    expect(interrupted).toBe(false);
    expect(priv.isSpeaking).toBe(false);
    expect(priv.tailGraceActive).toBe(false);
    // Not a barge-in: nothing was cleared on the carrier.
    expect(bridge.sendClear).not.toHaveBeenCalled();
    // Leading audio recovered: the ring was flushed to STT.
    expect(sttSendAudio).toHaveBeenCalledTimes(2);
  });

  it('a transcript during ACTIVE TTS still triggers a real barge-in', () => {
    const bridge = makeBridge();
    const handler = new StreamHandler(
      makeDeps(bridge, new MetricsStore()),
      makeMockWs(),
      '+15551110000',
      '+15552220000',
    );
    const priv = handler as unknown as {
      isSpeaking: boolean;
      tailGraceActive: boolean;
      speakingStartedAt: number | null;
      firstAudioSentAt: number | null;
      inboundAudioRing: Buffer[];
      stt: unknown;
      handleBargeIn(t: { text?: string; isFinal?: boolean }): boolean;
    };
    priv.stt = { sendAudio: vi.fn(), finalize: vi.fn() };
    priv.isSpeaking = true;
    priv.tailGraceActive = false; // active TTS, NOT the post-completion grace
    priv.speakingStartedAt = Date.now() - 2000;
    priv.firstAudioSentAt = Date.now() - 2000;
    priv.inboundAudioRing = [];

    const interrupted = priv.handleBargeIn({ text: 'actually wait', isFinal: true });

    expect(interrupted).toBe(true);
    expect(priv.isSpeaking).toBe(false);
    expect(bridge.sendClear).toHaveBeenCalled();
  });
});

/**
 * [mocked] Pipeline false-barge-in regressions: a still-silent (no-audio-yet)
 * LLM turn must NOT be self-interrupted, and a duplicate STT final must not
 * abort the turn that is still producing it.
 *
 * Reproduces the field bug "agent plays its opening line then goes mute": the
 * barge-in gate armed at LLM *dispatch* time (before any audio reached the
 * carrier) and ran ahead of the STT duplicate/hallucination filter, so the
 * lagging Deepgram speech_final twin of an already-committed is_final aborted
 * the in-flight LLM turn (llmAbort.abort()) before its first token — no output.
 *
 * AUTHENTIC: the real StreamHandler + LLMLoop + pipeline turn path. Mocked only
 * at the external boundary: the LLM provider stream (a deliberately slow /
 * parked provider, like a cold-cache model or a pre-first-token Hermes turn)
 * and the TTS byte stream.
 *
 *   Test A — silent-turn guard: a parked turn (no audio yet) leaves
 *            ``firstAudioSentAt`` null, ``canBargeIn()`` false, and a transcript
 *            delivered past the 500 ms window does NOT abort the turn.
 *   Test B — duplicate-final guard: with the warmup gate forced open, a second
 *            identical final (is_final then speech_final) commits exactly one
 *            user turn and fires NO barge-in/abort.
 *   Test C — regression: a turn that really emitted audio, past the protected
 *            window, IS interrupted by a genuinely new transcript and the turn
 *            is cancelled + re-dispatched.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { AgentOptions } from '../src/types';
import type { LLMProvider, LLMChunk, LLMStreamOptions } from '../src/llm-loop';
import type { WebSocket as WSWebSocket } from 'ws';

vi.mock('../src/dashboard/persistence', () => ({ notifyDashboard: vi.fn() }));

/**
 * Plain in-process TTS adapter. ``createTTS`` simply returns ``agent.tts``, so
 * a bare object with ``synthesizeStream`` is the whole contract. Built as a
 * plain object (NOT a vi.fn) so ``vi.restoreAllMocks`` between cases cannot
 * reset it to undefined and silently disable audio for a later test. Each chunk
 * is an even-length PCM16 frame the outbound encoder accepts, so real audio
 * actually reaches the carrier and ``markFirstAudioSent`` fires.
 */
function makeMockTts(): AgentOptions['tts'] {
  return {
    synthesizeStream: async function* (_text: string): AsyncGenerator<Buffer> {
      yield Buffer.alloc(640, 1); // ~20 ms PCM16 @ 16 kHz
    },
  } as unknown as AgentOptions['tts'];
}

interface SttFinal {
  isFinal?: boolean;
  speechFinal?: boolean;
  text?: string;
}

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(), close: vi.fn(), on: vi.fn(), once: vi.fn(), readyState: 1,
    removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makeMockStt() {
  let cb: ((t: SttFinal) => Promise<void> | undefined) | undefined;
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    onTranscript: vi.fn((fn: (t: SttFinal) => Promise<void> | undefined) => {
      cb = fn;
    }),
    get requestId() { return 'stt-silent-req'; },
    /** Deliver an arbitrary transcript frame (interim or final). */
    emit(t: SttFinal): Promise<void> | undefined {
      return cb?.(t);
    },
    /** Deliver a committed final (Deepgram is_final by default). */
    emitFinal(text: string, speechFinal = false): Promise<void> | undefined {
      return cb?.({ isFinal: true, speechFinal, text });
    },
  };
}

function makeTwilioBridge(mockStt: ReturnType<typeof makeMockStt>): TelephonyBridge {
  return {
    label: 'Twilio', telephonyProvider: 'twilio',
    sendAudio: vi.fn(), sendMark: vi.fn(), sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(mockStt),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  } as unknown as TelephonyBridge;
}

interface Gate {
  promise: Promise<void>;
  release: () => void;
}
function makeGate(): Gate {
  let release!: () => void;
  const promise = new Promise<void>((r) => {
    release = r;
  });
  return { promise, release };
}

/** Provider whose FIRST turn parks (no token, hence no audio) until ``gate``
 * resolves — models a cold-cache LLM / pre-first-token Hermes turn. Later turns
 * answer immediately so a re-dispatch after a real barge-in can complete. */
function makeGatedProvider(gate: Gate, counter: { count: number }): LLMProvider {
  return {
    model: 'agent-runtime-1',
    async *stream(
      _messages: Array<Record<string, unknown>>,
      _tools?: Array<Record<string, unknown>> | null,
      opts?: LLMStreamOptions,
    ): AsyncGenerator<LLMChunk, void, unknown> {
      counter.count += 1;
      if (counter.count === 1) {
        await gate.promise;
        if (opts?.signal?.aborted) return;
        yield { type: 'text', content: 'va bene. ' };
        return;
      }
      yield { type: 'text', content: 'ok ricevuto. ' };
    },
  } as unknown as LLMProvider;
}

/** Provider whose FIRST turn speaks ONE sentence (so real audio reaches the
 * carrier and ``markFirstAudioSent`` fires) and then parks until aborted, so
 * the turn stays in flight (``isSpeaking`` true) and a genuine barge-in has
 * something to interrupt. Later turns answer immediately. */
function makeSpeakThenParkProvider(counter: { count: number }): LLMProvider {
  return {
    model: 'agent-runtime-1',
    async *stream(
      _messages: Array<Record<string, unknown>>,
      _tools?: Array<Record<string, unknown>> | null,
      opts?: LLMStreamOptions,
    ): AsyncGenerator<LLMChunk, void, unknown> {
      counter.count += 1;
      if (counter.count === 1) {
        // Two sentences: the first flushes through the chunker (and reaches the
        // carrier as real audio) when the second arrives; then park so the turn
        // stays in flight with audio already out.
        yield { type: 'text', content: 'Ciao, ti racconto una lunga storia. ' };
        yield { type: 'text', content: 'Comincia proprio adesso. ' };
        await new Promise<void>((resolve) => {
          if (opts?.signal?.aborted) return resolve();
          opts?.signal?.addEventListener('abort', () => resolve(), { once: true });
        });
        return;
      }
      yield { type: 'text', content: 'va bene, mi fermo. ' };
    },
  } as unknown as LLMProvider;
}

function makeDeps(
  bridge: TelephonyBridge,
  agentOverrides: Partial<AgentOptions>,
): StreamHandlerDeps {
  const agent: AgentOptions = {
    systemPrompt: 'You are a test pipeline agent.',
    provider: 'pipeline',
    tts: makeMockTts(),
    ...agentOverrides,
  } as AgentOptions;
  return {
    config: {}, agent, bridge,
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn(),
    sanitizeVariables: vi.fn((raw: Record<string, unknown>) => raw),
    resolveVariables: vi.fn((tpl: string) => tpl),
  } as unknown as StreamHandlerDeps;
}

interface Probe {
  isSpeaking: boolean;
  firstAudioSentAt: number | null;
  llmAbort: AbortController | null;
  dispatchTask: Promise<void> | null;
  canBargeIn(): boolean;
}

describe('[mocked] pipeline silent-turn + duplicate-final barge-in guard', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: async () => ({}), text: async () => '',
    } as unknown as Response);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('A: a silent (no-audio-yet) turn is not armed for barge-in and a transcript past 500 ms does not abort it', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const gate = makeGate();
    const counter = { count: 0 };
    const handler = new StreamHandler(
      makeDeps(bridge, {
        llm: makeGatedProvider(gate, counter) as unknown as AgentOptions['llm'],
      }),
      makeMockWs(), '+15551111111', '+15552222222',
    );
    const probe = handler as unknown as Probe;
    await handler.handleCallStart('CA-silent-A');

    // Turn 1 dispatches and parks on its (slow) LLM stream — no token, no audio.
    await stt.emitFinal('dimmi qualcosa');
    await vi.waitFor(() => expect(probe.isSpeaking).toBe(true), { timeout: 3000 });

    // DEFECT 1: barge-in must NOT be armed before real audio went out. With the
    // dispatch-time stamp this is a number and the gate is already counting down.
    expect(probe.firstAudioSentAt).toBeNull();

    // Let the (buggy) 500 ms warmup gate elapse, then prove the gate stays shut.
    await new Promise((r) => setTimeout(r, 600));
    expect(probe.canBargeIn()).toBe(false);

    const abortCtl = probe.llmAbort;
    expect(abortCtl).not.toBeNull();

    // A transcript lands while the turn is still producing. It must NOT abort
    // the still-silent turn. Fire-and-forget: a *new* committed final blocks on
    // the parked dispatch downstream, but handleBargeIn runs synchronously first.
    void stt.emitFinal('ci sei');
    await new Promise((r) => setTimeout(r, 50));

    expect(abortCtl!.signal.aborted).toBe(false);
    expect(bridge.sendClear as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
    expect(probe.isSpeaking).toBe(true);

    // Release the parked turn so it completes cleanly: real audio now flows and
    // the warmup gate arms from that instant.
    gate.release();
    await vi.waitFor(
      () => expect(bridge.sendAudio as ReturnType<typeof vi.fn>).toHaveBeenCalled(),
      { timeout: 3000 },
    );
    await probe.dispatchTask?.catch(() => {});
  }, 10000);

  it('B: a duplicate Deepgram final (is_final then speech_final, same text) commits one turn and fires no barge-in', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const gate = makeGate();
    const counter = { count: 0 };
    const handler = new StreamHandler(
      makeDeps(bridge, {
        llm: makeGatedProvider(gate, counter) as unknown as AgentOptions['llm'],
      }),
      makeMockWs(), '+15551111111', '+15552222222',
    );
    const probe = handler as unknown as Probe;
    await handler.handleCallStart('CA-silent-B');

    // First final commits and dispatches turn 1 (parks on its slow stream).
    await stt.emitFinal('voglio prenotare un tavolo', false);
    await vi.waitFor(() => expect(probe.isSpeaking).toBe(true), { timeout: 3000 });

    // Isolate DEFECT 2 from the warmup gate: force the gate fully open, so the
    // ONLY thing that may stop the duplicate from cancelling the turn is the
    // commit-time duplicate filter running ahead of barge-in.
    (handler as unknown as { canBargeIn: () => boolean }).canBargeIn = () => true;

    const abortCtl = probe.llmAbort;
    expect(abortCtl).not.toBeNull();
    const callsAfterTurn1 = counter.count;

    // The lagging speech_final twin of the SAME utterance arrives ~0.5 s later.
    // It is a duplicate commitTranscript would drop — so it must be a no-op for
    // barge-in, not abort the turn that is still producing it.
    await stt.emitFinal('voglio prenotare un tavolo', true);

    expect(abortCtl!.signal.aborted).toBe(false);
    expect(bridge.sendClear as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
    expect(probe.isSpeaking).toBe(true);
    // Exactly one user turn committed → exactly one LLM dispatch.
    expect(counter.count).toBe(callsAfterTurn1);

    gate.release();
    await probe.dispatchTask?.catch(() => {});
  }, 10000);

  it('C: a genuinely new transcript still interrupts a turn that really emitted audio (re-dispatch)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const counter = { count: 0 };
    const handler = new StreamHandler(
      makeDeps(bridge, {
        llm: makeSpeakThenParkProvider(counter) as unknown as AgentOptions['llm'],
      }),
      makeMockWs(), '+15551111111', '+15552222222',
    );
    const probe = handler as unknown as Probe;
    await handler.handleCallStart('CA-silent-C');

    // Turn 1 speaks one sentence (real audio → markFirstAudioSent) then parks.
    await stt.emitFinal('genera una storia lunga');
    await vi.waitFor(
      () => expect(probe.firstAudioSentAt).not.toBeNull(),
      { timeout: 3000 },
    );
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).toHaveBeenCalled();

    // Move past the 500 ms no-AEC protected window (real audio already out).
    probe.firstAudioSentAt = Date.now() - 1000;
    expect(probe.canBargeIn()).toBe(true);

    const abortCtl = probe.llmAbort;
    expect(abortCtl).not.toBeNull();

    // A genuinely NEW (different) caller utterance over live audio → real
    // barge-in: cancel the turn and re-dispatch the new utterance.
    await stt.emitFinal('aspetta fermati un momento');

    await vi.waitFor(
      () => expect(bridge.sendClear as ReturnType<typeof vi.fn>).toHaveBeenCalled(),
      { timeout: 3000 },
    );
    expect(abortCtl!.signal.aborted).toBe(true);
    await vi.waitFor(() => expect(counter.count).toBe(2), { timeout: 3000 });

    await probe.dispatchTask?.catch(() => {});
  }, 10000);
});

/**
 * [mocked] Pipeline-mode opt-in spoken fallback on an LLM stream error.
 *
 * Exercises the REAL pipeline turn path:
 *   STT final → processTranscript → runPipelineLlm → real LLMLoop.run →
 *   provider.stream() THROWS (gateway-down) → the EXISTING catch(e)
 *   non-abort branch → opt-in ``agent.llmErrorMessage`` fallback spoken via
 *   the same per-sentence TTS primitive (synthesizeSentence) every normal
 *   sentence uses.
 *
 * AUTHENTIC: the StreamHandler, the CallMetricsAccumulator, the real
 * ``LLMLoop`` (constructed inside ``initPipeline`` from ``agent.llm``), the
 * sentence chunker, and the TTS-send path are REAL. The ONLY mocked surfaces
 * are the two external boundaries:
 *   1. The LLM provider's ``stream()`` — the paid HTTP gateway — stubbed to
 *      throw ``PatterConnectionError`` (or yield text then throw / abort).
 *   2. The TTS byte stream (ElevenLabsTTS ``synthesizeStream``) — replaced
 *      with a couple of PCM Buffers so audio-out is observable.
 * Everything inward (LLMLoop.run, the catch branch, the fallback gate, the
 * synthesizeSentence primitive, sendAudio) runs unmodified.
 *
 * The authenticity invariant: if ``synthesizeSentence`` is stubbed to throw,
 * the positive test's audio-out assertion fails — proving the test drives the
 * real speak primitive, not a mock (see the dedicated test at the bottom).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { AgentOptions } from '../src/types';
import type { LLMProvider, LLMChunk, LLMStreamOptions } from '../src/llm-loop';
import { PatterConnectionError } from '../src/errors';
import type { WebSocket as WSWebSocket } from 'ws';

const FALLBACK = 'Sorry, I am having trouble right now.';

// ---------------------------------------------------------------------------
// Module-level TTS mock — the external byte boundary. Each `new ElevenLabsTTS`
// returns a controllable instance; `synthesizeStream` yields a couple of PCM
// Buffers and records the exact text it was asked to speak.
// ---------------------------------------------------------------------------
vi.mock('../src/providers/elevenlabs-tts', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('../src/providers/elevenlabs-tts')>();
  return {
    ...original,
    ElevenLabsTTS: vi.fn().mockImplementation(() => ({
      synthesizeStream: vi.fn(async function* () {
        yield Buffer.from('tts-audio');
      }),
    })),
  };
});

// Silence dashboard persistence side-effects.
vi.mock('../src/dashboard/persistence', () => ({
  notifyDashboard: vi.fn(),
}));

import { ElevenLabsTTS } from '../src/providers/elevenlabs-tts';

// ---------------------------------------------------------------------------
// Harness helpers (mirrors tests/integration/pipeline-e2e.test.ts)
// ---------------------------------------------------------------------------

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

/** STT mock that lets tests push a final transcript manually. */
function makeMockStt() {
  let transcriptCb:
    | ((t: { isFinal?: boolean; text?: string }) => Promise<void>)
    | undefined;
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    onTranscript: vi.fn(
      (cb: (t: { isFinal?: boolean; text?: string }) => Promise<void>) => {
        transcriptCb = cb;
      },
    ),
    get requestId() {
      return 'stt-fallback-req';
    },
    emitTranscript(text: string): Promise<void> | undefined {
      return transcriptCb?.({ isFinal: true, text });
    },
  };
}

function makeTwilioBridge(
  mockStt: ReturnType<typeof makeMockStt>,
): TelephonyBridge {
  return {
    label: 'Twilio',
    telephonyProvider: 'twilio',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(mockStt),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  } as unknown as TelephonyBridge;
}

/**
 * A real ``LLMProvider`` whose ``stream()`` is the only mocked surface — the
 * external gateway boundary. ``mode`` selects the failure shape:
 *  - 'throw'          → throws PatterConnectionError before any text (the
 *                       gateway-down / timeout case the fallback targets).
 *  - 'partial-throw'  → yields one PARTIAL token (no sentence boundary), THEN
 *                       throws. The chunker buffers it, TTS never runs, so the
 *                       caller heard SILENCE — the fallback MUST still fire.
 *  - 'sentence-throw' → yields a COMPLETE sentence (real audio emitted), THEN
 *                       throws. The fallback MUST be suppressed (no double-speak).
 *  - 'abort'          → aborts the per-turn signal then throws AbortError
 *                       (clean barge-in cancellation — must NOT speak).
 */
function makeThrowingProvider(
  mode: 'throw' | 'partial-throw' | 'sentence-throw' | 'abort',
): LLMProvider {
  return {
    model: 'agent-runtime-1',
    async *stream(
      _messages: Array<Record<string, unknown>>,
      _tools?: Array<Record<string, unknown>> | null,
      opts?: LLMStreamOptions,
    ): AsyncGenerator<LLMChunk, void, unknown> {
      if (mode === 'partial-throw') {
        yield { type: 'text', content: 'Let me check that ' };
        throw new PatterConnectionError('LLM API returned 503: gateway down');
      }
      if (mode === 'sentence-throw') {
        yield { type: 'text', content: 'Hello there. ' };
        throw new PatterConnectionError('LLM API returned 503: gateway down');
      }
      if (mode === 'abort') {
        // Simulate a barge-in: the per-turn signal trips, then the upstream
        // fetch rejects with an AbortError — the catch branch must treat this
        // as a clean cancellation and stay silent.
        const ac = opts?.signal as AbortSignal | undefined;
        const err = new Error('The operation was aborted');
        err.name = 'AbortError';
        // Make the signal observably aborted for the isAbort check.
        if (ac && !ac.aborted) {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          Object.defineProperty(ac, 'aborted', { value: true, configurable: true });
        }
        throw err;
      }
      // 'throw' — no text emitted before the gateway failure.
      throw new PatterConnectionError('LLM API returned 503: gateway down');
      // eslint-disable-next-line no-unreachable
      yield { type: 'text', content: '' };
    },
  } as unknown as LLMProvider;
}

/**
 * Install a custom ElevenLabsTTS synthesizeStream and return the list of texts
 * it was asked to speak. Must run BEFORE handleCallStart so the factory is set
 * when the StreamHandler constructs the TTS instance.
 */
function setupTtsMock(
  impl: (text: string) => AsyncGenerator<Buffer> = async function* () {
    yield Buffer.from('pcm-chunk-1');
    yield Buffer.from('pcm-chunk-2');
  },
): { calls: string[] } {
  const calls: string[] = [];
  const MockTTS = ElevenLabsTTS as unknown as ReturnType<typeof vi.fn>;
  MockTTS.mockImplementation(() => ({
    synthesizeStream: vi.fn(async function* (text: string) {
      calls.push(text);
      yield* impl(text);
    }),
  }));
  return { calls };
}

function makeDeps(
  bridge: TelephonyBridge,
  agentOverrides: Partial<AgentOptions>,
): StreamHandlerDeps {
  // A TTS adapter instance via the mocked ElevenLabsTTS factory so the
  // pipeline's synthesizeStream path is exercised.
  const mockTts = new (ElevenLabsTTS as unknown as new (
    key: string,
    voice?: string,
  ) => { synthesizeStream: (t: string) => AsyncIterable<Buffer> })(
    'el-key',
    'rachel',
  );
  const agent: AgentOptions = {
    systemPrompt: 'You are a test pipeline agent.',
    provider: 'pipeline',
    // tts/llm cast through unknown — the StreamHandler reads these adapter
    // instances structurally (synthesizeStream / stream).
    tts: mockTts as unknown as AgentOptions['tts'],
    ...agentOverrides,
  } as AgentOptions;
  return {
    config: {},
    agent,
    bridge,
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn(),
    sanitizeVariables: vi.fn((raw: Record<string, unknown>) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl: string) => tpl),
  } as unknown as StreamHandlerDeps;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('[mocked] pipeline LLM-error spoken fallback (llmErrorMessage)', () => {
  beforeEach(() => {
    // Generic fetch stub for any incidental network the handler might touch
    // (built-in tool dispatch etc.). The LLM provider is injected directly so
    // this does NOT serve the LLM turn.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    } as Response);
    const MockTTS = ElevenLabsTTS as unknown as ReturnType<typeof vi.fn>;
    MockTTS.mockClear();
    MockTTS.mockImplementation(() => ({
      synthesizeStream: vi.fn(async function* () {
        yield Buffer.from('tts-audio');
      }),
    }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('speaks the configured line when the LLM stream throws with zero text emitted', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    const deps = makeDeps(bridge, {
      llm: makeThrowingProvider('throw') as unknown as AgentOptions['llm'],
      llmErrorMessage: FALLBACK,
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-fallback-spoken');
    await stt.emitTranscript('Can you book me an appointment?');

    // The fallback line is synthesized through the real TTS primitive and the
    // resulting PCM is pushed to the carrier (observable audio-out).
    await vi.waitFor(
      () => expect(ttsCalls).toContain(FALLBACK),
      { timeout: 5000 },
    );
    expect(ttsCalls).toEqual([FALLBACK]);
    expect(
      (bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBeGreaterThanOrEqual(1);
  }, 10000);

  it('speaks NOTHING when llmErrorMessage is unset (default = today silence-on-error)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    // No llmErrorMessage → undefined → no fallback.
    const deps = makeDeps(bridge, {
      llm: makeThrowingProvider('throw') as unknown as AgentOptions['llm'],
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-fallback-unset');
    await stt.emitTranscript('Can you book me an appointment?');

    // Let the error path settle (catch branch logs + records interrupted turn).
    await new Promise<void>((r) => setTimeout(r, 300));

    expect(ttsCalls).toHaveLength(0);
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
    // The real interrupted-turn accounting still ran (turn does not leak as
    // a completed turn). endCall() exposes the recorded turns.
    const acc = (
      handler as unknown as {
        metricsAcc: import('../src/metrics').CallMetricsAccumulator;
      }
    ).metricsAcc;
    const metrics = acc.endCall();
    expect(metrics.turns.every((t) => t.text !== FALLBACK)).toBe(true);
  }, 10000);

  it('speaks the empty-string fallback as NOTHING (falsy guard preserves silence)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    const deps = makeDeps(bridge, {
      llm: makeThrowingProvider('throw') as unknown as AgentOptions['llm'],
      llmErrorMessage: '', // empty string is treated as unset
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-fallback-empty');
    await stt.emitTranscript('Hello there?');

    await new Promise<void>((r) => setTimeout(r, 300));

    expect(ttsCalls).toHaveLength(0);
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
  }, 10000);

  it('speaks the fallback when partial tokens were buffered but NO audio was emitted', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    // Provider yields a PARTIAL token (no sentence boundary) THEN throws. The
    // chunker buffers it without flushing a sentence, so TTS never ran and the
    // caller heard SILENCE. Gating on emitted audio (not received tokens), the
    // fallback MUST fire — this is the agent-runtime gateway-timeout regression.
    const deps = makeDeps(bridge, {
      llm: makeThrowingProvider(
        'partial-throw',
      ) as unknown as AgentOptions['llm'],
      llmErrorMessage: FALLBACK,
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-fallback-partial');
    await stt.emitTranscript('What is the weather?');

    await vi.waitFor(() => expect(ttsCalls).toContain(FALLBACK), {
      timeout: 5000,
    });
    // The buffered partial token never reached TTS, so the ONLY thing spoken is
    // the fallback line, and real PCM was pushed to the carrier.
    expect(ttsCalls).toEqual([FALLBACK]);
    expect(
      (bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBeGreaterThanOrEqual(1);
  }, 10000);

  it('does NOT speak the fallback after a full sentence was already spoken (no double-speak)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    // Provider yields a COMPLETE sentence (real TTS audio emitted) THEN throws.
    // The caller already heard speech, so the fallback must be suppressed.
    const deps = makeDeps(bridge, {
      llm: makeThrowingProvider(
        'sentence-throw',
      ) as unknown as AgentOptions['llm'],
      llmErrorMessage: FALLBACK,
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-fallback-sentence');
    await stt.emitTranscript('What is the weather?');

    await vi.waitFor(() => expect(ttsCalls).toContain('Hello there.'), {
      timeout: 5000,
    });
    await new Promise<void>((r) => setTimeout(r, 200));

    // The real sentence was spoken; the fallback line must NOT be appended.
    expect(ttsCalls).not.toContain(FALLBACK);
  }, 10000);

  it('does NOT speak the fallback on a clean barge-in abort (AbortError branch stays silent)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    const deps = makeDeps(bridge, {
      llm: makeThrowingProvider('abort') as unknown as AgentOptions['llm'],
      llmErrorMessage: FALLBACK,
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-fallback-abort');
    await stt.emitTranscript('Cancel that please.');

    await new Promise<void>((r) => setTimeout(r, 300));

    // The abort is a clean cancellation — the fallback MUST NOT be spoken.
    expect(ttsCalls).not.toContain(FALLBACK);
  }, 10000);

  it('authenticity: stubbing synthesizeSentence to throw makes the positive path emit no audio', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    setupTtsMock();

    const deps = makeDeps(bridge, {
      llm: makeThrowingProvider('throw') as unknown as AgentOptions['llm'],
      llmErrorMessage: FALLBACK,
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    // Replace the REAL speak primitive with a thrower. The fallback wiring
    // wraps the call in try/catch, so the turn must not crash, but NO audio
    // can reach the carrier — proving the positive test exercised the real
    // synthesizeSentence rather than a mock.
    const synthSpy = vi
      .spyOn(
        handler as unknown as {
          synthesizeSentence: (...args: unknown[]) => Promise<void>;
        },
        'synthesizeSentence',
      )
      .mockRejectedValue(new Error('synthesizeSentence disabled'));

    await handler.handleCallStart('CA-fallback-authentic');
    await stt.emitTranscript('Book me in.');

    await new Promise<void>((r) => setTimeout(r, 300));

    expect(synthSpy).toHaveBeenCalledWith(
      FALLBACK,
      expect.anything(),
      expect.anything(),
      expect.anything(),
    );
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
  }, 10000);
});

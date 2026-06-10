/**
 * [mocked] Pipeline-mode opt-in long-turn filler (longTurnMessage, Feature #8).
 *
 * Exercises the REAL pipeline turn path:
 *   STT final → processTranscript → runPipelineLlm → real LLMLoop.run →
 *   provider.stream() is SLOW (agent runtime running tools) → the scheduled
 *   long-turn filler fires after ``longTurnMessageAfterS`` → spoken via the same
 *   per-sentence TTS primitive (synthesizeSentence) every normal sentence uses.
 *
 * AUTHENTIC: the StreamHandler, the real ``LLMLoop`` (constructed inside
 * ``initPipeline`` from ``agent.llm``), the sentence chunker, the filler
 * scheduling / cancellation, and the TTS-send path are REAL. The ONLY mocked
 * surfaces are the two external boundaries:
 *   1. The LLM provider's ``stream()`` — its TIMING / the paid gateway hop —
 *      stubbed to delay (or not) before yielding text.
 *   2. The TTS byte stream (ElevenLabsTTS ``synthesizeStream``) — replaced with
 *      PCM Buffers so audio-out is observable.
 * Everything inward runs unmodified.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { AgentOptions } from '../src/types';
import type { LLMProvider, LLMChunk, LLMStreamOptions } from '../src/llm-loop';
import type { WebSocket as WSWebSocket } from 'ws';

const FILLER = 'One moment while I look that up.';

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

vi.mock('../src/dashboard/persistence', () => ({
  notifyDashboard: vi.fn(),
}));

import { ElevenLabsTTS } from '../src/providers/elevenlabs-tts';

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
      return 'stt-filler-req';
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
 * A real ``LLMProvider`` whose ``stream()`` timing is the only mocked surface.
 *  - ``delayMs``: wait this long before yielding the (single, complete) reply
 *    sentence. A delay past ``longTurnMessageAfterS`` lets the filler fire; a
 *    zero delay starts real audio before the filler timer.
 */
function makeSlowProvider(delayMs: number, reply = 'Here is your answer. '): LLMProvider {
  return {
    model: 'agent-runtime-1',
    async *stream(
      _messages: Array<Record<string, unknown>>,
      _tools?: Array<Record<string, unknown>> | null,
      _opts?: LLMStreamOptions,
    ): AsyncGenerator<LLMChunk, void, unknown> {
      if (delayMs > 0) {
        await new Promise<void>((r) => setTimeout(r, delayMs));
      }
      yield { type: 'text', content: reply };
    },
  } as unknown as LLMProvider;
}

function setupTtsMock(): { calls: string[] } {
  const calls: string[] = [];
  const MockTTS = ElevenLabsTTS as unknown as ReturnType<typeof vi.fn>;
  MockTTS.mockImplementation(() => ({
    synthesizeStream: vi.fn(async function* (text: string) {
      calls.push(text);
      yield Buffer.from('pcm-chunk-1');
      yield Buffer.from('pcm-chunk-2');
    }),
  }));
  return { calls };
}

function makeDeps(
  bridge: TelephonyBridge,
  agentOverrides: Partial<AgentOptions>,
): StreamHandlerDeps {
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

describe('[mocked] pipeline long-turn filler (longTurnMessage)', () => {
  beforeEach(() => {
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

  it('speaks the filler when the turn is slow, then the real reply', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    // Provider takes 120 ms before its first word; the filler fires at ~20 ms.
    const deps = makeDeps(bridge, {
      llm: makeSlowProvider(120) as unknown as AgentOptions['llm'],
      longTurnMessage: FILLER,
      longTurnMessageAfterS: 0.02,
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-filler-slow');
    await stt.emitTranscript('What is the weather?');

    await vi.waitFor(() => expect(ttsCalls).toContain(FILLER), {
      timeout: 5000,
    });
    // Dispatch now runs as a backgrounded task (so the STT loop can barge-in
    // mid-turn) — await it so the real reply has been synthesized before we
    // assert on the full TTS sequence.
    await (handler as unknown as { dispatchTask: Promise<void> | null }).dispatchTask?.catch(
      () => {},
    );
    // The filler was spoken FIRST, then the real reply — exactly one filler.
    expect(ttsCalls.indexOf(FILLER)).toBe(0);
    expect(ttsCalls).toContain('Here is your answer.');
    expect(ttsCalls.filter((t) => t === FILLER)).toHaveLength(1);
    expect(
      (bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBeGreaterThanOrEqual(1);
  }, 10000);

  it('does NOT speak the filler when real audio starts quickly (no double-speak)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    // Reply is immediate; the filler timer (500 ms) must be cleared before firing.
    const deps = makeDeps(bridge, {
      llm: makeSlowProvider(0) as unknown as AgentOptions['llm'],
      longTurnMessage: FILLER,
      longTurnMessageAfterS: 0.5,
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-filler-fast');
    await stt.emitTranscript('What is the weather?');

    await vi.waitFor(() => expect(ttsCalls).toContain('Here is your answer.'), {
      timeout: 5000,
    });
    // Give the (cleared) timer ample chance to (not) fire.
    await new Promise<void>((r) => setTimeout(r, 600));

    expect(ttsCalls).not.toContain(FILLER);
  }, 10000);

  it('speaks NOTHING extra when longTurnMessage is unset (feature OFF)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { calls: ttsCalls } = setupTtsMock();

    // Slow turn but no longTurnMessage → no filler, behaviour unchanged.
    const deps = makeDeps(bridge, {
      llm: makeSlowProvider(80) as unknown as AgentOptions['llm'],
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CA-filler-off');
    await stt.emitTranscript('What is the weather?');

    await vi.waitFor(() => expect(ttsCalls).toContain('Here is your answer.'), {
      timeout: 5000,
    });
    await new Promise<void>((r) => setTimeout(r, 150));

    // Only the real reply — the filler never appears.
    expect(ttsCalls).toEqual(['Here is your answer.']);
  }, 10000);

  it('authenticity: stubbing synthesizeSentence to throw makes the filler emit no audio', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    setupTtsMock();

    const deps = makeDeps(bridge, {
      llm: makeSlowProvider(120) as unknown as AgentOptions['llm'],
      longTurnMessage: FILLER,
      longTurnMessageAfterS: 0.02,
    });
    const handler = new StreamHandler(
      deps,
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );

    // Break the REAL speak primitive only for the filler line. Its scheduled
    // call is wrapped in a .catch(), so the turn must not crash, but no filler
    // PCM can reach the carrier — proving the positive test exercised the real
    // synthesizeSentence rather than a mock.
    const realSynth = (
      handler as unknown as {
        synthesizeSentence: (...args: unknown[]) => Promise<void>;
      }
    ).synthesizeSentence.bind(handler);
    const synthSpy = vi
      .spyOn(
        handler as unknown as {
          synthesizeSentence: (...args: unknown[]) => Promise<void>;
        },
        'synthesizeSentence',
      )
      .mockImplementation(async (...args: unknown[]) => {
        if (args[0] === FILLER) {
          throw new Error('synthesizeSentence disabled for filler');
        }
        return realSynth(...args);
      });

    await handler.handleCallStart('CA-filler-authentic');
    await stt.emitTranscript('What is the weather?');

    // The real reply still plays; the broken filler degraded to silence.
    // Filler audio is not part of the LLM reply — recordSegment=false.
    await vi.waitFor(() => expect(synthSpy).toHaveBeenCalledWith(
      FILLER,
      expect.anything(),
      expect.anything(),
      expect.anything(),
      false,
    ), { timeout: 5000 });
    await vi.waitFor(() => expect(synthSpy).toHaveBeenCalledWith(
      'Here is your answer.',
      expect.anything(),
      expect.anything(),
      expect.anything(),
    ), { timeout: 5000 });
  }, 10000);
});

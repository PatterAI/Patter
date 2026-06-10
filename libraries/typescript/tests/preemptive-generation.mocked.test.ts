/**
 * [mocked] PREEMPTIVE GENERATION (parity with Python
 * test_preemptive_generation.py).
 *
 * Opt-in `agent.preemptiveGeneration`: a confident INTERIM transcript starts
 * a speculative LLM+TTS dispatch whose audio is HELD in memory; the final
 * transcript's commit either RELEASES it (matching text — buffered audio
 * flushes to the carrier and exactly one history/metrics turn is recorded)
 * or discards it silently and dispatches normally.
 *
 * AUTHENTIC: the real StreamHandler + LLMLoop + speculation machinery. Mocked
 * only at the external boundary: the LLM provider stream, the TTS byte
 * stream, STT transcripts, and VAD events.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler, endsWithSentenceFinalPunct, speculationMatches } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { AgentOptions, VADProvider } from '../src/types';
import type { LLMProvider, LLMChunk, LLMStreamOptions } from '../src/llm-loop';
import type { CallMetricsAccumulator } from '../src/metrics';
import type { WebSocket as WSWebSocket } from 'ws';

vi.mock('../src/dashboard/persistence', () => ({ notifyDashboard: vi.fn() }));

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(), close: vi.fn(), on: vi.fn(), once: vi.fn(), readyState: 1,
    removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makeMockStt() {
  let cb:
    | ((t: { isFinal?: boolean; text?: string }) => Promise<void>)
    | undefined;
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    onTranscript: vi.fn(
      (fn: (t: { isFinal?: boolean; text?: string }) => Promise<void>) => {
        cb = fn;
      },
    ),
    get requestId() { return 'stt-preemptive-req'; },
    emitInterim(text: string): Promise<void> | undefined {
      return cb?.({ isFinal: false, text });
    },
    emitFinal(text: string): Promise<void> | undefined {
      return cb?.({ isFinal: true, text });
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

/** Scripted provider: records every stream() call and yields the reply for
 * that call index token-by-token, honouring the abort signal. */
function makeScriptedProvider(replies: string[]) {
  const calls: Array<{ messages: Array<{ role: string; content: string }> }> = [];
  const provider = {
    model: 'fake-llm-1',
    async *stream(
      messages: Array<Record<string, unknown>>,
      _tools?: Array<Record<string, unknown>> | null,
      opts?: LLMStreamOptions,
    ): AsyncGenerator<LLMChunk, void, unknown> {
      const index = calls.length;
      calls.push({
        messages: JSON.parse(JSON.stringify(messages)) as Array<{
          role: string;
          content: string;
        }>,
      });
      const reply = replies[Math.min(index, replies.length - 1)];
      for (const word of reply.split(' ')) {
        if (opts?.signal?.aborted) return;
        yield { type: 'text', content: `${word} ` };
      }
    },
  } as unknown as LLMProvider;
  return { provider, calls };
}

/** Provider that parks pre-first-token until its abort signal fires. */
function makeParkUntilAbortProvider() {
  const state = { calls: 0, aborted: false };
  const provider = {
    model: 'fake-llm-1',
    async *stream(
      _messages: Array<Record<string, unknown>>,
      _tools?: Array<Record<string, unknown>> | null,
      opts?: LLMStreamOptions,
    ): AsyncGenerator<LLMChunk, void, unknown> {
      state.calls += 1;
      const signal = opts?.signal;
      await new Promise<void>((resolve) => {
        if (signal?.aborted) return resolve();
        signal?.addEventListener('abort', () => resolve(), { once: true });
      });
      state.aborted = true;
      // Yield nothing — torn down pre-first-token.
    },
  } as unknown as LLMProvider;
  return { provider, state };
}

function makeFakeTts(chunk: Buffer = Buffer.alloc(320, 1)) {
  const synthesized: string[] = [];
  return {
    synthesized,
    tts: {
      synthesizeStream: vi.fn(async function* (text: string) {
        synthesized.push(text);
        yield chunk;
      }),
      close: vi.fn(),
    },
  };
}

function makeDeps(
  bridge: TelephonyBridge,
  agentOverrides: Partial<AgentOptions>,
): StreamHandlerDeps {
  const agent: AgentOptions = {
    systemPrompt: 'You are a test pipeline agent.',
    provider: 'pipeline',
    preemptiveGeneration: true,
    preemptiveMinStableMs: 30,
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

type HandlerInternals = {
  speculation: { interimText: string; failed: boolean; abort: AbortController } | null;
  dispatchTask: Promise<void> | null;
  history: { entries: Array<{ role: string; text: string }> };
  metricsAcc: CallMetricsAccumulator;
  isSpeaking: boolean;
  abortSpeculation: (reason: string, countMiss?: boolean) => Promise<void>;
  clearInterimStabilityTimer: () => void;
  noteInterimTranscript: (text: string) => Promise<void>;
};

function internals(handler: StreamHandler): HandlerInternals {
  return handler as unknown as HandlerInternals;
}

async function settle(handler: StreamHandler): Promise<void> {
  const h = internals(handler);
  await h.abortSpeculation('test_teardown', false);
  await h.dispatchTask?.catch(() => {});
  h.clearInterimStabilityTimer();
}

describe('[mocked] preemptive generation (pipeline)', () => {
  beforeEach(() => {
    process.env.PATTER_TTS_TAIL_GRACE_MS = '0';
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: async () => ({}), text: async () => '',
    } as unknown as Response);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    delete process.env.PATTER_TTS_TAIL_GRACE_MS;
  });

  it('helpers: sentence-final punctuation + normalized matching', () => {
    expect(endsWithSentenceFinalPunct('che ore sono?')).toBe(true);
    expect(endsWithSentenceFinalPunct('prenota un tavolo. ')).toBe(true);
    expect(endsWithSentenceFinalPunct('prenota un tavolo per')).toBe(false);
    expect(endsWithSentenceFinalPunct('')).toBe(false);
    expect(speculationMatches('what time is it', 'What time is it?')).toBe(true);
    expect(speculationMatches('ciao, come stai?', 'ciao come stai')).toBe(true);
    expect(speculationMatches('what time', 'what time is the meeting')).toBe(false);
    expect(speculationMatches('', '')).toBe(false);
  });

  it('default off: interims never start a speculation', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider } = makeParkUntilAbortProvider();
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      preemptiveGeneration: undefined, // Agent default
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-off');

    await stt.emitInterim('ciao come stai?');
    await new Promise((r) => setTimeout(r, 80));
    expect(internals(handler).speculation).toBeNull();
    await settle(handler);
  });

  it('starts a speculation immediately on a punctuated interim', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider, state } = makeParkUntilAbortProvider();
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      preemptiveMinStableMs: 60_000, // only the punctuation fast-path can fire
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-punct');

    await stt.emitInterim('ciao come stai?');
    const spec = internals(handler).speculation;
    expect(spec).not.toBeNull();
    expect(spec!.interimText).toBe('ciao come stai?');
    await vi.waitFor(() => expect(state.calls).toBe(1), { timeout: 2000 });
    await settle(handler);
  });

  it('starts a speculation once a non-punctuated interim is stable', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider } = makeParkUntilAbortProvider();
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      preemptiveMinStableMs: 30,
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-stable');

    await stt.emitInterim('prenota un tavolo per due');
    expect(internals(handler).speculation).toBeNull(); // no punctuation → not yet
    await vi.waitFor(
      () => expect(internals(handler).speculation).not.toBeNull(),
      { timeout: 2000 },
    );
    expect(internals(handler).speculation!.interimText).toBe(
      'prenota un tavolo per due',
    );
    await settle(handler);
  });

  it('releases on a matching final: audio flushed, single history entry, single turn, hit counted', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider, calls } = makeScriptedProvider([
      'Sono le tre del pomeriggio precise adesso.',
    ]);
    const { tts, synthesized } = makeFakeTts();
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-release');

    // Confident interim (sentence-final punctuation) → speculate.
    await stt.emitInterim('che ore sono?');
    expect(internals(handler).speculation).not.toBeNull();
    // Let the speculative LLM+TTS run and buffer. Nothing audible yet.
    await vi.waitFor(() => expect(synthesized.length).toBeGreaterThan(0), {
      timeout: 2000,
    });
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();

    // The final matches (only punctuation/case differ) → RELEASE.
    await stt.emitFinal('Che ore sono?');
    await vi.waitFor(
      () =>
        expect(bridge.sendAudio as ReturnType<typeof vi.fn>).toHaveBeenCalled(),
      { timeout: 2000 },
    );
    await internals(handler).dispatchTask?.catch(() => {});

    // The LLM ran exactly once — on the interim, with a snapshot history.
    expect(calls.length).toBe(1);
    const userMsgs = calls[0].messages.filter((m) => m.role === 'user');
    expect(userMsgs[userMsgs.length - 1].content).toBe('che ore sono?');
    // Exactly ONE user history entry — the FINAL transcript text — and one
    // assistant entry.
    const entries = internals(handler).history.entries;
    const userEntries = entries.filter((e) => e.role === 'user');
    const assistantEntries = entries.filter((e) => e.role === 'assistant');
    expect(userEntries.map((e) => e.text)).toEqual(['Che ore sono?']);
    expect(assistantEntries.length).toBe(1);
    expect(assistantEntries[0].text).toContain('Sono le tre');
    // Metrics: exactly one recorded turn; hit counted, no misses.
    const metrics = internals(handler).metricsAcc.endCall();
    expect(metrics.preemptive_hits).toBe(1);
    expect(metrics.preemptive_misses).toBe(0);
    const realTurns = metrics.turns.filter((t) => t.agent_text !== '[interrupted]');
    expect(realTurns.length).toBe(1);
    expect(realTurns[0].user_text).toBe('Che ore sono?');
    await settle(handler);
  }, 10000);

  it('discards on a mismatched final and dispatches normally (no duplicate history)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider, calls } = makeScriptedProvider([
      'RISPOSTA SPECULATIVA MOLTO INTERESSANTE DAVVERO OGGI.',
      'RISPOSTA REALE MOLTO INTERESSANTE DAVVERO OGGI SI.',
    ]);
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-mismatch');

    await stt.emitInterim('prenota un tavolo.');
    await vi.waitFor(() => expect(calls.length).toBe(1), { timeout: 2000 });
    // Give the speculative TTS a beat to buffer; still nothing audible.
    await new Promise((r) => setTimeout(r, 50));
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();

    // Final adds real words → mismatch → discard + normal dispatch.
    await stt.emitFinal('prenota un tavolo per quattro.');
    await internals(handler).dispatchTask?.catch(() => {});

    expect(calls.length).toBe(2);
    const userMsgs2 = calls[1].messages.filter((m) => m.role === 'user');
    expect(userMsgs2[userMsgs2.length - 1].content).toBe(
      'prenota un tavolo per quattro.',
    );
    // No duplicate history: one user entry (final text), one assistant.
    const entries = internals(handler).history.entries;
    const userEntries = entries.filter((e) => e.role === 'user');
    const assistantEntries = entries.filter((e) => e.role === 'assistant');
    expect(userEntries.map((e) => e.text)).toEqual(['prenota un tavolo per quattro.']);
    expect(assistantEntries.length).toBe(1);
    expect(assistantEntries[0].text).toContain('REALE');
    const metrics = internals(handler).metricsAcc.endCall();
    expect(metrics.preemptive_hits).toBe(0);
    expect(metrics.preemptive_misses).toBe(1);
    await settle(handler);
  }, 10000);

  it('aborts silently on VAD speech_start during speculation (barge-in by the user)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider, state } = makeParkUntilAbortProvider();
    const { tts } = makeFakeTts();
    const vadEvents: Array<{ type: string } | null> = [{ type: 'speech_start' }];
    const scriptedVad = {
      processFrame: vi.fn(async () => vadEvents.shift() ?? null),
      reset: vi.fn(),
    } as unknown as VADProvider;
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
      vad: scriptedVad,
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-vad');

    await stt.emitInterim('aspetta un attimo?');
    const spec = internals(handler).speculation;
    expect(spec).not.toBeNull();
    await vi.waitFor(() => expect(state.calls).toBe(1), { timeout: 2000 });

    // The user resumes speaking → VAD speech_start while the agent idles.
    await handler.handleAudio(Buffer.alloc(320, 0x7f));

    expect(internals(handler).speculation).toBeNull();
    expect(spec!.abort.signal.aborted).toBe(true);
    expect(state.aborted).toBe(true); // the LLM stream was torn down
    // Silent: nothing sent, nothing cleared, nothing in history.
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
    expect(bridge.sendClear as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
    expect(internals(handler).history.entries.length).toBe(0);
    const metrics = internals(handler).metricsAcc.endCall();
    expect(metrics.preemptive_misses).toBe(1);
    await settle(handler);
  }, 10000);

  it('a newer qualifying interim replaces the in-flight speculation', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider, state } = makeParkUntilAbortProvider();
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      preemptiveMinStableMs: 60_000,
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-replace');

    await stt.emitInterim('prenota un tavolo?');
    const first = internals(handler).speculation;
    expect(first).not.toBeNull();

    await stt.emitInterim('prenota un tavolo per due?');
    await vi.waitFor(
      () =>
        expect(internals(handler).speculation?.interimText).toBe(
          'prenota un tavolo per due?',
        ),
      { timeout: 2000 },
    );
    expect(internals(handler).speculation).not.toBe(first);
    expect(first!.abort.signal.aborted).toBe(true);
    await vi.waitFor(() => expect(state.calls).toBe(2), { timeout: 2000 });
    const metrics = internals(handler).metricsAcc.endCall();
    expect(metrics.preemptive_misses).toBe(1); // the replaced one
    await settle(handler);
  }, 10000);

  it('re-emitting the same interim keeps the existing speculation', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider, state } = makeParkUntilAbortProvider();
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-same');

    await stt.emitInterim('ciao come stai?');
    const first = internals(handler).speculation;
    expect(first).not.toBeNull();
    await stt.emitInterim('Ciao, come stai?'); // normalized-equal re-emit
    expect(internals(handler).speculation).toBe(first);
    await vi.waitFor(() => expect(state.calls).toBe(1), { timeout: 2000 });
    await settle(handler);
  });

  it('aborts the speculation when the held audio exceeds the ~15s cap', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider } = makeScriptedProvider([
      'Una risposta molto lunga che genera tanto audio sintetizzato.',
    ]);
    // One chunk > 15 s of PCM16 @ 16 kHz (32 bytes/ms).
    const { tts } = makeFakeTts(Buffer.alloc(16_000 * 32, 1));
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-overflow');

    await stt.emitInterim('raccontami una storia lunga?');
    await vi.waitFor(
      () => expect(internals(handler).speculation).toBeNull(),
      { timeout: 2000 },
    );
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
    const metrics = internals(handler).metricsAcc.endCall();
    expect(metrics.preemptive_misses).toBe(1);
    await settle(handler);
  }, 10000);

  it('never speculates while the agent is speaking or a dispatch is in flight', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider } = makeParkUntilAbortProvider();
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-gates');
    const h = internals(handler);

    // Agent speaking → an interim is barge-in material, not a next turn.
    h.isSpeaking = true;
    await h.noteInterimTranscript('ciao come stai?');
    expect(h.speculation).toBeNull();
    h.isSpeaking = false;

    // A turn dispatch in flight → single-in-flight contract.
    let resolveDispatch!: () => void;
    h.dispatchTask = new Promise<void>((r) => { resolveDispatch = r; });
    await h.noteInterimTranscript('ciao come stai?');
    expect(h.speculation).toBeNull();
    resolveDispatch();
    h.dispatchTask = null;
    await settle(handler);
  });

  it('releases while the LLM is still mid-stream: buffered sentence flushes immediately, the rest streams live', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    // One long sentence + a confirming token, then the stream parks until
    // the gate opens (models an agent-runtime LLM pausing mid-stream).
    let openGate!: () => void;
    const gate = new Promise<void>((r) => { openGate = r; });
    const llmCalls: number[] = [];
    const provider = {
      model: 'fake-llm-1',
      async *stream(
        _messages: Array<Record<string, unknown>>,
        _tools?: Array<Record<string, unknown>> | null,
        opts?: LLMStreamOptions,
      ): AsyncGenerator<LLMChunk, void, unknown> {
        llmCalls.push(1);
        yield { type: 'text', content: 'La prima frase della risposta è già abbastanza lunga. ' };
        // Confirms the sentence boundary so the chunker emits sentence 1.
        yield { type: 'text', content: 'E' };
        await gate;
        if (opts?.signal?.aborted) return;
        yield { type: 'text', content: ' la seconda frase arriva in diretta dopo il rilascio. ' };
      },
    } as unknown as LLMProvider;
    const { tts, synthesized } = makeFakeTts();
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-midstream');

    await stt.emitInterim('dimmi due cose?');
    // Sentence 1 synthesized and HELD — nothing audible yet.
    await vi.waitFor(() => expect(synthesized.length).toBe(1), { timeout: 2000 });
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();

    // The final commits while the LLM is parked mid-stream (no next token):
    // the decision race must flush the buffered sentence immediately.
    await stt.emitFinal('Dimmi due cose?');
    await vi.waitFor(
      () =>
        expect(bridge.sendAudio as ReturnType<typeof vi.fn>).toHaveBeenCalled(),
      { timeout: 2000 },
    );
    expect(synthesized.length).toBe(1); // sentence 2 not generated yet

    // The stream resumes — sentence 2 goes out live.
    openGate();
    await vi.waitFor(() => expect(synthesized.length).toBe(2), { timeout: 2000 });
    await internals(handler).dispatchTask?.catch(() => {});

    expect(llmCalls.length).toBe(1);
    const assistantEntries = internals(handler).history.entries.filter(
      (e) => e.role === 'assistant',
    );
    expect(assistantEntries.length).toBe(1);
    expect(assistantEntries[0].text).toContain('la seconda frase');
    const metrics = internals(handler).metricsAcc.endCall();
    expect(metrics.preemptive_hits).toBe(1);
    expect(metrics.preemptive_misses).toBe(0);
    await settle(handler);
  }, 10000);

  it('speculates again on the next turn after a released speculation (dispatch handle cleared)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider, calls } = makeScriptedProvider([
      'Prima risposta completa per il test adesso.',
      'Seconda risposta completa per il test adesso.',
    ]);
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-two-hits');

    // Turn 1: speculate → matching final → released.
    await stt.emitInterim('che ore sono?');
    expect(internals(handler).speculation).not.toBeNull();
    await stt.emitFinal('Che ore sono?');
    await internals(handler).dispatchTask?.catch(() => {});
    await vi.waitFor(
      () =>
        expect(
          internals(handler).history.entries.filter((e) => e.role === 'assistant').length,
        ).toBe(1),
      { timeout: 2000 },
    );
    // The released speculative task must clear the dispatch handle on
    // completion (like dispatchTurn does) and release the floor, or turn 2
    // below can never speculate again.
    await vi.waitFor(() => {
      expect(internals(handler).dispatchTask).toBeNull();
      expect(internals(handler).isSpeaking).toBe(false);
    }, { timeout: 2000 });

    // Turn 2: a fresh speculation must start — regression for the dispatch
    // handle never being cleared after a released speculation.
    await stt.emitInterim('e domani che si fa?');
    expect(internals(handler).speculation).not.toBeNull();
    await stt.emitFinal('E domani che si fa?');
    await internals(handler).dispatchTask?.catch(() => {});
    await vi.waitFor(
      () =>
        expect(
          internals(handler).history.entries.filter((e) => e.role === 'assistant').length,
        ).toBe(2),
      { timeout: 2000 },
    );

    expect(calls.length).toBe(2); // one LLM call per turn — both speculative
    const metrics = internals(handler).metricsAcc.endCall();
    expect(metrics.preemptive_hits).toBe(2);
    expect(metrics.preemptive_misses).toBe(0);
    await settle(handler);
  }, 10000);

  it('handleStop tears down an in-flight speculation without counting a miss', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const { provider, state } = makeParkUntilAbortProvider();
    const { tts } = makeFakeTts();
    const deps = makeDeps(bridge, {
      llm: provider as unknown as AgentOptions['llm'],
      tts: tts as unknown as AgentOptions['tts'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+1', '+2');
    await handler.handleCallStart('CA-preemptive-teardown');

    await stt.emitInterim('aspetta un secondo?');
    const spec = internals(handler).speculation;
    expect(spec).not.toBeNull();
    await vi.waitFor(() => expect(state.calls).toBe(1), { timeout: 2000 });

    await handler.handleStop();

    expect(internals(handler).speculation).toBeNull();
    expect(spec!.abort.signal.aborted).toBe(true);
    expect(bridge.sendAudio as ReturnType<typeof vi.fn>).not.toHaveBeenCalled();
    const metrics = internals(handler).metricsAcc.endCall();
    expect(metrics.preemptive_hits).toBe(0);
    expect(metrics.preemptive_misses).toBe(0);
  }, 10000);
});

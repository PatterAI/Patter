/**
 * [mocked] Backgrounded-dispatch barge-in (parity with Python
 * test_pipeline_bargein_backgrounded.py).
 *
 * The transcript drain loop must keep running ``handleBargeIn`` DURING a long
 * agent-runtime turn — so a caller who speaks over a slow (30-90 s, tool-
 * running) Hermes/OpenClaw response actually interrupts it, instead of being
 * answered only after the turn finishes. Previously ``processTranscript``
 * awaited ``runPipelineLlm`` inline and stopped draining transcripts for the
 * whole turn (head-of-line blocking).
 *
 * AUTHENTIC: the real StreamHandler + LLMLoop + pipeline turn path. Mocked only
 * at the external boundary: the LLM provider stream (parks until aborted, like
 * Hermes pre-first-token) and the TTS byte stream.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { AgentOptions } from '../src/types';
import type { LLMProvider, LLMChunk, LLMStreamOptions } from '../src/llm-loop';
import type { WebSocket as WSWebSocket } from 'ws';

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
vi.mock('../src/dashboard/persistence', () => ({ notifyDashboard: vi.fn() }));

import { ElevenLabsTTS } from '../src/providers/elevenlabs-tts';

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(), close: vi.fn(), on: vi.fn(), once: vi.fn(), readyState: 1,
    removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makeMockStt() {
  let cb: ((t: { isFinal?: boolean; text?: string }) => Promise<void>) | undefined;
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    onTranscript: vi.fn((fn: (t: { isFinal?: boolean; text?: string }) => Promise<void>) => {
      cb = fn;
    }),
    get requestId() { return 'stt-bg-req'; },
    emitTranscript(text: string): Promise<void> | undefined {
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

/** Provider whose FIRST turn parks until aborted (models a long Hermes turn
 * that only ends on barge-in); any later turn replies quickly so the real
 * follow-up — no longer swallowed by the back-to-back dedup — can complete. */
function makeParkUntilAbortProvider(aborted: { value: boolean }): LLMProvider {
  let calls = 0;
  return {
    model: 'agent-runtime-1',
    async *stream(
      _messages: Array<Record<string, unknown>>,
      _tools?: Array<Record<string, unknown>> | null,
      opts?: LLMStreamOptions,
    ): AsyncGenerator<LLMChunk, void, unknown> {
      calls += 1;
      if (calls > 1) {
        yield { type: 'text', content: 'va bene. ' };
        return;
      }
      const signal = opts?.signal;
      await new Promise<void>((resolve) => {
        if (signal?.aborted) return resolve();
        signal?.addEventListener('abort', () => resolve(), { once: true });
      });
      aborted.value = true;
      // Yield nothing after abort — the turn was interrupted pre-first-token.
    },
  } as unknown as LLMProvider;
}

/** Captures the built messages (prompt) the first stream() call receives, then
 * yields a quick reply — to assert the in-flight turn's prompt is built from a
 * history SNAPSHOT and cannot be contaminated by a later transcript's push. */
function makeMessageCapturingProvider(captured: {
  messages?: Array<{ role: string; content: string }>;
}): LLMProvider {
  return {
    model: 'agent-runtime-1',
    async *stream(
      messages: Array<Record<string, unknown>>,
      _tools?: Array<Record<string, unknown>> | null,
      _opts?: LLMStreamOptions,
    ): AsyncGenerator<LLMChunk, void, unknown> {
      if (!captured.messages) {
        captured.messages = JSON.parse(JSON.stringify(messages)) as Array<{
          role: string;
          content: string;
        }>;
      }
      yield { type: 'text', content: 'va bene. ' };
    },
  } as unknown as LLMProvider;
}

function makeDeps(bridge: TelephonyBridge, agentOverrides: Partial<AgentOptions>): StreamHandlerDeps {
  const mockTts = new (ElevenLabsTTS as unknown as new (k: string, v?: string) => {
    synthesizeStream: (t: string) => AsyncIterable<Buffer>;
  })('el-key', 'rachel');
  const agent: AgentOptions = {
    systemPrompt: 'You are a test pipeline agent.',
    provider: 'pipeline',
    tts: mockTts as unknown as AgentOptions['tts'],
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

describe('[mocked] pipeline backgrounded-dispatch barge-in', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: async () => ({}), text: async () => '',
    } as unknown as Response);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    delete process.env.PATTER_FORWARD_STT_WHILE_SPEAKING;
  });

  it('a barge-in transcript cancels the in-flight long turn (loop not blocked)', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const aborted = { value: false };
    const deps = makeDeps(bridge, {
      llm: makeParkUntilAbortProvider(aborted) as unknown as AgentOptions['llm'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+15551111111', '+15552222222');
    // Past the warmup gate so the barge-in is allowed to fire.
    (handler as unknown as { canBargeIn: () => boolean }).canBargeIn = () => true;

    await handler.handleCallStart('CA-bg-bargein');

    // Turn 1 starts and parks on its (long) LLM stream.
    await stt.emitTranscript('dimmi una storia lunga');
    await vi.waitFor(
      () => expect((handler as unknown as { isSpeaking: boolean }).isSpeaking).toBe(true),
      { timeout: 3000 },
    );

    // Caller barges in WHILE turn 1 is in flight. With the old inline-await
    // this transcript would not be read until turn 1 ended.
    await stt.emitTranscript('ferma per favore');

    // The in-flight turn was cancelled: the carrier buffer was cleared and the
    // LLM stream's abort signal fired (turn 1 torn down pre-first-token).
    await vi.waitFor(
      () => expect(bridge.sendClear as ReturnType<typeof vi.fn>).toHaveBeenCalled(),
      { timeout: 3000 },
    );
    await vi.waitFor(() => expect(aborted.value).toBe(true), { timeout: 3000 });

    // Settle the backgrounded dispatch before teardown. The real follow-up
    // "ferma per favore" (different text, <0.5s) is NOT swallowed by the
    // back-to-back dedup — it dispatches as turn 2 and replies.
    await (handler as unknown as { dispatchTask: Promise<void> | null }).dispatchTask?.catch(
      () => {},
    );
  }, 10000);

  it('PATTER_FORWARD_STT_WHILE_SPEAKING is read from env (default off)', () => {
    const offBridge = makeTwilioBridge(makeMockStt());
    const offHandler = new StreamHandler(
      makeDeps(offBridge, {}), makeMockWs(), '+1', '+2',
    );
    expect(
      (offHandler as unknown as { forwardSttWhileSpeaking: boolean }).forwardSttWhileSpeaking,
    ).toBe(false);

    process.env.PATTER_FORWARD_STT_WHILE_SPEAKING = '1';
    const onBridge = makeTwilioBridge(makeMockStt());
    const onHandler = new StreamHandler(
      makeDeps(onBridge, {}), makeMockWs(), '+1', '+2',
    );
    expect(
      (onHandler as unknown as { forwardSttWhileSpeaking: boolean }).forwardSttWhileSpeaking,
    ).toBe(true);
  });

  it("a later transcript's history push does NOT contaminate the in-flight turn's prompt", async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const captured: { messages?: Array<{ role: string; content: string }> } = {};
    const deps = makeDeps(bridge, {
      llm: makeMessageCapturingProvider(captured) as unknown as AgentOptions['llm'],
    });
    const handler = new StreamHandler(deps, makeMockWs(), '+15551111111', '+15552222222');
    await handler.handleCallStart('CA-bg-snapshot');

    // Turn A is committed and its dispatch launched (backgrounded). emitTranscript
    // resolves after the snapshot was captured at launch.
    await stt.emitTranscript('domanda del turno A');
    // Simulate a FOLLOWING transcript's user push landing on the drain loop while
    // turn A is still in flight (the exact race the snapshot fix guards against).
    (handler as unknown as { history: { push: (e: unknown) => void } }).history.push({
      role: 'user',
      text: 'TURNO B PIU TARDI',
      timestamp: Date.now(),
    });

    await vi.waitFor(() => expect(captured.messages).toBeDefined(), { timeout: 3000 });
    await (handler as unknown as { dispatchTask: Promise<void> | null }).dispatchTask?.catch(
      () => {},
    );

    const contents = (captured.messages ?? []).map((m) => m.content);
    // Turn A's prompt was built from the launch-time snapshot — the later push
    // is absent. With the pre-fix LIVE array it would leak in.
    expect(contents).toContain('domanda del turno A');
    expect(contents).not.toContain('TURNO B PIU TARDI');
  }, 10000);
});

/**
 * [mocked] StreamHandler — issue #154 Realtime call-handling fixes.
 *
 * Covers four behavioural fixes on the OpenAI Realtime path:
 *   FIX-1  flush a buffered assistant turn immediately when the paired user
 *          transcript is dropped as a hallucination (no ~3 s stall).
 *   FIX-2  the hallucination blocklist is now DISPLAY-ONLY and narrowed to
 *          non-speech artefacts — real words ('yes'/'no'/'okay'/'right') must
 *          NOT be dropped, caption credits still are.
 *   FIX-3  barge-in on the Realtime interrupt path. In the DEFAULT
 *          server-managed mode the engine turn stays anchored at speech_stopped
 *          (the server owns VAD + cancel) so NO recordBargeinDetected /
 *          anchorUserSpeechStart fire — the client only truncates + sendClear.
 *          The legacy opt-out (gateResponseOnTranscript=true) keeps stamping
 *          recordBargeinDetected + anchorUserSpeechStart so its post-barge-in
 *          hygiene gate fires.
 *   FIX-4  Realtime 'error' events are surfaced at WARN level (no PII) and do
 *          NOT terminate the call.
 *   FIX-5  a reserved monotonic turn index threads through to recordTurnComplete
 *          and the live per-line transcript events.
 *
 * AUTHENTIC: the StreamHandler, the CallMetricsAccumulator, the
 * OpenAIRealtime2Adapter, and the hallucination filter are REAL. Mocked ONLY
 * at the external boundary — the OpenAI WebSocket transport (injected mock
 * ``ws``) and the network ``connect()``. Events are injected through the real
 * onEvent subscription the handler registers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler, isSttHallucination } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import { setLogger, type Logger } from '../src/logger';
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

/**
 * Build a REAL OpenAIRealtime2Adapter (extends OpenAIRealtimeAdapter so the
 * stream-handler ``instanceof`` feature gates fire). Stub only ``connect``
 * (the external OpenAI handshake) and inject a mock WS.
 */
function makeRealAdapter(
  ws: WSWebSocket,
  gateResponseOnTranscript = false,
): OpenAIRealtime2Adapter {
  const adapter = new OpenAIRealtime2Adapter(
    'sk-test',
    'gpt-realtime-2',
    'alloy',
    'You are a helpful test agent.',
    undefined,
    undefined,
    { gateResponseOnTranscript },
  );
  vi.spyOn(adapter, 'connect').mockResolvedValue(undefined);
  (adapter as unknown as { ws: WSWebSocket }).ws = ws;
  return adapter;
}

function makeDeps(
  bridge: TelephonyBridge,
  adapter: OpenAIRealtime2Adapter,
  store: MetricsStore,
  overrides?: Partial<StreamHandlerDeps>,
): StreamHandlerDeps {
  const agent: AgentOptions = {
    systemPrompt: 'You are a helpful test agent.',
    provider: 'openai_realtime',
    model: 'gpt-realtime-2',
    voice: 'alloy',
  };
  return {
    config: { openaiKey: 'sk-test' },
    agent,
    bridge,
    metricsStore: store,
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(adapter),
    sanitizeVariables: vi.fn((raw: Record<string, unknown>) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl: string) => tpl),
    ...overrides,
  } as unknown as StreamHandlerDeps;
}

/** Capture the StreamHandler's registered onEvent callback (real subscription). */
function captureEventCallback(
  adapter: OpenAIRealtimeAdapter,
): { current: ((type: string, data: unknown) => Promise<void>) | undefined } {
  const box: { current: ((type: string, data: unknown) => Promise<void>) | undefined } = {
    current: undefined,
  };
  const realOnEvent = adapter.onEvent.bind(adapter);
  vi.spyOn(adapter, 'onEvent').mockImplementation((cb) => {
    box.current = cb as (type: string, data: unknown) => Promise<void>;
    realOnEvent(cb);
  });
  return box;
}

/**
 * Ensure the store carries a ``recordTranscriptLine`` method and return a spy
 * over it. The real method is added to ``MetricsStore`` for issue #154; this
 * helper makes the test resilient to build ordering by installing a no-op
 * stub if the method is not yet present, then spying on it either way.
 */
function spyTranscriptLine(store: MetricsStore): ReturnType<typeof vi.fn> {
  const s = store as unknown as { recordTranscriptLine?: (d: unknown) => void };
  if (typeof s.recordTranscriptLine !== 'function') {
    s.recordTranscriptLine = () => {};
  }
  const spy = vi.fn();
  vi.spyOn(s as { recordTranscriptLine: (d: unknown) => void }, 'recordTranscriptLine').mockImplementation(
    (d: unknown) => spy(d),
  );
  return spy;
}

describe('[mocked] StreamHandler — FIX-2 narrowed hallucination blocklist (issue #154)', () => {
  it('does NOT drop standalone conversational words (display-only filter)', () => {
    expect(isSttHallucination('yes')).toBe(false);
    expect(isSttHallucination('no')).toBe(false);
    expect(isSttHallucination('okay')).toBe(false);
    expect(isSttHallucination('right')).toBe(false);
    expect(isSttHallucination('you')).toBe(false);
    expect(isSttHallucination('thanks')).toBe(false);
    expect(isSttHallucination('thank you')).toBe(false);
    // Punctuation variants of real words are also kept.
    expect(isSttHallucination('Yes.')).toBe(false);
    expect(isSttHallucination('Okay!')).toBe(false);
  });

  it('still drops caption credits, music/silence markers, and multi-closer sign-offs', () => {
    expect(isSttHallucination('thank you for watching')).toBe(true);
    expect(isSttHallucination('Thank you for watching!')).toBe(true);
    expect(isSttHallucination('please subscribe')).toBe(true);
    expect(isSttHallucination('subtitles by the amara.org community')).toBe(true);
    expect(isSttHallucination('[music]')).toBe(true);
    expect(isSttHallucination('[silence]')).toBe(true);
    // Multi-sentence sign-off: EVERY piece is a known hallucination.
    expect(isSttHallucination("we'll see you next time. bye bye.")).toBe(true);
    // Empty / whitespace-only is still a drop.
    expect(isSttHallucination('   ')).toBe(true);
  });

  it('keeps a real sentence that merely contains a filler word', () => {
    expect(isSttHallucination('yes I would like to book a haircut')).toBe(false);
    expect(isSttHallucination('thank you for the help. can you transfer me?')).toBe(false);
  });
});

describe('[mocked] StreamHandler — FIX-1 flush buffered assistant on hallucination drop (issue #154)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    } as Response);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    void fetchSpy;
  });

  it('flushes the buffered assistant reply immediately when the user transcript is a hallucination', async () => {
    vi.useFakeTimers();
    try {
      const bridge = makeBridge();
      const adapterWs = makeMockWs();
      const adapter = makeRealAdapter(adapterWs);
      const events = captureEventCallback(adapter);
      const onTranscript = vi.fn().mockResolvedValue(undefined);
      const store = new MetricsStore();
      spyTranscriptLine(store);

      const handler = new StreamHandler(
        makeDeps(bridge, adapter, store, { onTranscript }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      handler.setStreamSid('MZ-stream-flush');
      await handler.handleCallStart('CA-call-flush');

      // 1) User stops speaking — turn opens, userTranscriptPending = true.
      await events.current!('speech_stopped', null);
      // 2) Agent text streams in, then response.done fires BEFORE the user
      //    transcript — the assistant turn is buffered behind the 3 s fallback
      //    timer waiting for a user transcript.
      await events.current!('transcript_output', 'The weather is sunny today.');
      await events.current!('response_done', null);
      // Assistant must NOT have flushed yet (still buffered, timer pending).
      const flushedBefore = onTranscript.mock.calls.filter(
        (c) => (c[0] as { role?: string }).role === 'assistant',
      );
      expect(flushedBefore).toHaveLength(0);

      // 3) The user transcript turns out to be a hallucination → dropped. The
      //    buffered assistant must flush IMMEDIATELY (not after 3 s) so the
      //    reply does not stall and turns do not interleave.
      await events.current!('transcript_input', 'thank you for watching');

      const flushedAfter = onTranscript.mock.calls.filter(
        (c) => (c[0] as { role?: string }).role === 'assistant',
      );
      expect(flushedAfter).toHaveLength(1);
      expect((flushedAfter[0][0] as { text: string }).text).toBe(
        'The weather is sunny today.',
      );
      // No user transcript was surfaced (it was a hallucination).
      const userEvents = onTranscript.mock.calls.filter(
        (c) => (c[0] as { role?: string }).role === 'user',
      );
      expect(userEvents).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('[mocked] StreamHandler — FIX-3 barge-in metrics on Realtime interrupt (issue #154)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    } as Response);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    void fetchSpy;
  });

  it('DEFAULT server-managed: barge-in does NOT stamp recordBargeinDetected / anchorUserSpeechStart (engine turn stays anchored at speech_stopped)', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(adapterWs); // gate=false (server-managed)
    const events = captureEventCallback(adapter);
    const store = new MetricsStore();
    spyTranscriptLine(store);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, store),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-stream-barge-sm');
    await handler.handleCallStart('CA-call-barge-sm');

    const acc = (handler as unknown as { metricsAcc: import('../src/metrics').CallMetricsAccumulator }).metricsAcc;
    const bargeinSpy = vi.spyOn(acc, 'recordBargeinDetected');
    const anchorSpy = vi.spyOn(acc, 'anchorUserSpeechStart');

    // Even with a long-running response (would clear any gate), server-managed
    // mode never runs the gate and never stamps the barge-in metrics.
    (
      adapter as unknown as { currentResponseFirstAudioAt: number | null }
    ).currentResponseFirstAudioAt = Date.now() - 5000;

    await events.current!('speech_started', null);

    // Server owns VAD + cancel — the client only clears the carrier buffer
    // (sendClear) and truncates; no engine barge-in metrics fire.
    expect(bargeinSpy).not.toHaveBeenCalled();
    expect(anchorSpy).not.toHaveBeenCalled();
    expect(bridge.sendClear).toHaveBeenCalled();
  });

  it('LEGACY opt-out (gate=true): stamps recordBargeinDetected + anchorUserSpeechStart and the next turn drops endpoint_ms / stt_ms', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(adapterWs, true); // gate=true (client-managed)
    const events = captureEventCallback(adapter);
    const store = new MetricsStore();
    spyTranscriptLine(store);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, store),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-stream-barge');
    await handler.handleCallStart('CA-call-barge');

    const acc = (handler as unknown as { metricsAcc: import('../src/metrics').CallMetricsAccumulator }).metricsAcc;
    const bargeinSpy = vi.spyOn(acc, 'recordBargeinDetected');
    const anchorSpy = vi.spyOn(acc, 'anchorUserSpeechStart');

    // Force the agent past the anti-flicker gate so the interrupt path runs
    // the full cancel + metric-stamp sequence.
    (
      adapter as unknown as { currentResponseFirstAudioAt: number | null }
    ).currentResponseFirstAudioAt = Date.now() - 5000;

    // User barges in over the agent.
    await events.current!('speech_started', null);

    expect(bargeinSpy).toHaveBeenCalledTimes(1);
    expect(anchorSpy).toHaveBeenCalledTimes(1);
    expect(bridge.sendClear).toHaveBeenCalled();

    // The next turn opens immediately (within 100 ms of the barge-in) — the
    // post-barge-in hygiene gate must drop endpoint_ms and zero stt_ms so the
    // p95 distribution is not polluted with synthetic spikes.
    acc.recordSttComplete('next user reply');
    const turn = acc.recordTurnComplete('agent answer', 1);
    expect(turn).not.toBeNull();
    expect(turn!.latency.endpoint_ms).toBeUndefined();
    expect(turn!.latency.stt_ms).toBe(0);
  });
});

describe('[mocked] StreamHandler — FIX-4 Realtime error events surfaced (issue #154)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;
  let warnings: string[];
  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    } as Response);
    warnings = [];
    const capture: Logger = {
      info: () => {},
      warn: (msg) => warnings.push(msg),
      error: () => {},
      debug: () => {},
    };
    setLogger(capture);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    void fetchSpy;
  });

  it('logs an injected error frame at WARN (type/code/message only) and does not terminate the call', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(adapterWs);
    const events = captureEventCallback(adapter);
    const store = new MetricsStore();
    spyTranscriptLine(store);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, store),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-stream-error');
    await handler.handleCallStart('CA-call-error');

    await events.current!('error', {
      type: 'invalid_request_error',
      code: 'input_audio_buffer_commit_empty',
      message: 'buffer too small',
    });

    const matched = warnings.find((w) => w.includes('Realtime error'));
    expect(matched).toBeDefined();
    expect(matched).toContain('invalid_request_error');
    expect(matched).toContain('input_audio_buffer_commit_empty');
    expect(matched).toContain('buffer too small');

    // The call must NOT be terminated by an error event.
    expect(bridge.endCall).not.toHaveBeenCalled();

    // Handler still processes subsequent events (call alive).
    await events.current!('speech_stopped', null);
  });
});

describe('[mocked] StreamHandler — FIX-5 reserved index + live transcript lines (issue #154)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    } as Response);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    void fetchSpy;
  });

  it('reserveTurnIndex returns a monotonic sequence and threads into turn_index', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(adapterWs);
    const events = captureEventCallback(adapter);
    const store = new MetricsStore();
    const lineSpy = spyTranscriptLine(store);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, store),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-stream-idx');
    await handler.handleCallStart('CA-call-idx');

    // Turn 0
    await events.current!('speech_stopped', null);
    await events.current!('transcript_input', 'first user line');
    await events.current!('transcript_output', 'first agent line');
    await events.current!('response_done', null);

    // Turn 1
    await events.current!('speech_stopped', null);
    await events.current!('transcript_input', 'second user line');
    await events.current!('transcript_output', 'second agent line');
    await events.current!('response_done', null);

    // Live per-line events carry the reserved monotonic turnIndex, user then
    // assistant within each turn.
    const lines = lineSpy.mock.calls.map((c) => c[0] as { turnIndex: number; role: string; text: string });
    expect(lines).toEqual([
      { call_id: 'CA-call-idx', turnIndex: 0, role: 'user', text: 'first user line' },
      { call_id: 'CA-call-idx', turnIndex: 0, role: 'assistant', text: 'first agent line' },
      { call_id: 'CA-call-idx', turnIndex: 1, role: 'user', text: 'second user line' },
      { call_id: 'CA-call-idx', turnIndex: 1, role: 'assistant', text: 'second agent line' },
    ]);

    // The metrics turns carry the same reserved indices.
    const acc = (handler as unknown as { metricsAcc: import('../src/metrics').CallMetricsAccumulator }).metricsAcc;
    const metrics = acc.endCall();
    expect(metrics.turns[0].turn_index).toBe(0);
    expect(metrics.turns[1].turn_index).toBe(1);
  });
});

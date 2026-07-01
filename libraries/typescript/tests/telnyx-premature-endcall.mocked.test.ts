/**
 * [mocked] Premature end_call guard for native-audio realtime engines over Telnyx.
 *
 * BUG: a realtime engine (GeminiLive / OpenAIRealtime2) can emit ``end_call``
 * the instant its greeting finishes — before the caller has spoken. On Telnyx
 * the cold realtime connect→first-audio window eats the opening call budget, so
 * the model greets and immediately bails ("no_response") ~1.5 s in, killing a
 * live caller. Twilio and pipeline mode never hit this. The fix refuses a
 * model-initiated hang-up that fires before ANY caller speech AND within the
 * opening grace window, telling the model to keep listening instead.
 *
 * AUTHENTIC: the StreamHandler and the real OpenAIRealtime2Adapter are exercised
 * end-to-end. The real ``handleFunctionCall`` end_call branch, the real
 * ``onAdapterTranscriptInput`` caller-spoke path (incl. the real hallucination
 * filter), and the real grace-window arithmetic all run. Mocked only at the
 * external boundary — the OpenAI WebSocket transport (injected mock ``ws`` +
 * ``connect`` stub, a network call we cannot place in CI) and the carrier
 * ``TelephonyBridge`` (we cannot place phone calls in CI). We assert on the
 * observable outcomes: ``bridge.endCall`` (did the call die?) and the
 * ``sendFunctionResult`` payload the model receives back.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
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

function makeBridge(label = 'Telnyx', provider = 'telnyx'): TelephonyBridge {
  return {
    label,
    telephonyProvider: provider,
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  } as unknown as TelephonyBridge;
}

/** Build a REAL OpenAIRealtime2Adapter; stub only ``connect`` + inject mock WS. */
function makeRealAdapter(ws: WSWebSocket): OpenAIRealtime2Adapter {
  const adapter = new OpenAIRealtime2Adapter(
    'sk-test',
    'gpt-realtime-2',
    'alloy',
    'You are a helpful test agent.',
  );
  vi.spyOn(adapter, 'connect').mockResolvedValue(undefined);
  (adapter as unknown as { ws: WSWebSocket }).ws = ws;
  return adapter;
}

function makeRealtimeDeps(
  bridge: TelephonyBridge,
  adapter: OpenAIRealtime2Adapter,
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
    metricsStore: new MetricsStore(),
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
  } as unknown as StreamHandlerDeps;
}

/** Capture the StreamHandler's real adapter-event callback. */
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

const END_CALL_FC = { call_id: 'fc-1', name: 'end_call', arguments: JSON.stringify({ reason: 'no_response' }) };

describe('[mocked] premature end_call guard over Telnyx', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    } as Response);
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    void fetchSpy;
  });

  async function boot(bridge: TelephonyBridge) {
    const adapter = makeRealAdapter(makeMockWs());
    const sendResult = vi.spyOn(adapter, 'sendFunctionResult').mockResolvedValue(undefined);
    const events = captureEventCallback(adapter);
    const handler = new StreamHandler(
      makeRealtimeDeps(bridge, adapter),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('telnyx-stream-endcall');
    await handler.handleCallStart('telnyx-call-endcall');
    expect(events.current).toBeDefined();
    return { adapter, sendResult, events, handler };
  }

  it('refuses a model end_call before the caller has spoken, keeping the line open', async () => {
    const bridge = makeBridge();
    const { sendResult, events } = await boot(bridge);

    // Model emits end_call ~1.5 s in — before any caller transcript.
    await vi.advanceTimersByTimeAsync(1500);
    await events.current!('function_call', END_CALL_FC);

    // Call MUST NOT be torn down.
    expect((bridge as unknown as { endCall: ReturnType<typeof vi.fn> }).endCall).not.toHaveBeenCalled();
    // Model is told to keep listening (rejection payload, session left open).
    expect(sendResult).toHaveBeenCalledTimes(1);
    const [, payload] = sendResult.mock.calls[0] as [string, string];
    expect(JSON.parse(payload)).toMatchObject({ status: 'rejected', reason: 'caller_still_connecting' });
  });

  it('honors end_call once the caller has actually spoken (within the grace window)', async () => {
    const bridge = makeBridge();
    const { events } = await boot(bridge);

    // Real caller transcript flows through the real hallucination filter and
    // sets the "caller has spoken" flag.
    await events.current!('transcript_input', 'Hi, I would like to book an appointment.');
    await events.current!('function_call', {
      call_id: 'fc-2',
      name: 'end_call',
      arguments: JSON.stringify({ reason: 'conversation_complete' }),
    });

    expect((bridge as unknown as { endCall: ReturnType<typeof vi.fn> }).endCall).toHaveBeenCalledTimes(1);
  });

  it('honors end_call after the grace window even if the caller never spoke', async () => {
    const bridge = makeBridge();
    const { events } = await boot(bridge);

    // Past the 6 s opening grace window — a genuine no-answer hang-up is allowed.
    await vi.advanceTimersByTimeAsync(6001);
    await events.current!('function_call', END_CALL_FC);

    expect((bridge as unknown as { endCall: ReturnType<typeof vi.fn> }).endCall).toHaveBeenCalledTimes(1);
  });

  it('does not treat a Whisper hallucination as the caller speaking', async () => {
    const bridge = makeBridge();
    const { sendResult, events } = await boot(bridge);

    // Known Whisper-on-silence hallucination — must NOT flip userHasSpoken.
    await events.current!('transcript_input', 'Thank you for watching.');
    await vi.advanceTimersByTimeAsync(1000);
    await events.current!('function_call', END_CALL_FC);

    expect((bridge as unknown as { endCall: ReturnType<typeof vi.fn> }).endCall).not.toHaveBeenCalled();
    expect(sendResult).toHaveBeenCalledTimes(1);
  });
});

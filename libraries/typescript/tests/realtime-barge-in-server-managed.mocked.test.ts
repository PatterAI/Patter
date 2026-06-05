/**
 * [mocked] OpenAI Realtime barge-in — server-managed vs legacy client-managed (issue #154).
 *
 * GOAL: for end-to-end OpenAI Realtime engines the server owns turn-taking by
 * default (turn_detection.create_response=true + interrupt_response=true). On a
 * WebSocket transport the client STILL must, on the server's speech_started:
 *   (a) clear the carrier output buffer (sendClear), and
 *   (b) send conversation.item.truncate (the played-offset bookkeeping)
 * but it must NOT send response.cancel (the server cancels server-side), and it
 * must NOT run the client anti-flicker gate or re-anchor turn metrics.
 *
 * The legacy opt-out (gateResponseOnTranscript=true) restores the full
 * client-managed path: anti-flicker gate + full cancelResponse (truncate +
 * response.cancel) + recordBargeinDetected + anchorUserSpeechStart.
 *
 * ConvAI's interruption stays sendClear-only (server-managed by ElevenLabs).
 *
 * AUTHENTIC: the StreamHandler and the adapters are REAL. The only mock is the
 * OpenAI / ElevenLabs WebSocket transport — a real EventEmitter so the
 * adapters' real message-parsing + item-tracking code runs — plus the network
 * connect()/fetch() (external API calls we cannot place in CI). We assert on
 * observable wire frames the real code sends (conversation.item.truncate,
 * response.cancel) and on real metrics-accumulator calls (spied at the public
 * API).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'events';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import { ElevenLabsConvAIAdapter } from '../src/providers/elevenlabs-convai';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { WebSocket as WSWebSocket } from 'ws';
import type { AgentOptions } from '../src/types';

/**
 * A real EventEmitter standing in for the `ws` socket. `send` records the JSON
 * frames the adapter emits so we can assert on observable wire output; `on`
 * delegates to EventEmitter so the adapter's REAL `ensureMessageListener`
 * wires up and its real message parser / item-tracking runs when we `emit`
 * upstream frames.
 */
class FakeWs extends EventEmitter {
  static OPEN = 1;
  readyState = FakeWs.OPEN;
  OPEN = FakeWs.OPEN;
  sent: string[] = [];
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.readyState = 3;
  }
  ping(): void {
    /* no-op heartbeat */
  }
}

function framesOfType(ws: FakeWs, type: string): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const raw of ws.sent) {
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      if (parsed.type === type) out.push(parsed);
    } catch {
      /* ignore non-JSON */
    }
  }
  return out;
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

function makeDeps(
  bridge: TelephonyBridge,
  adapter: OpenAIRealtimeAdapter | ElevenLabsConvAIAdapter,
  provider: string,
): StreamHandlerDeps {
  const agent: AgentOptions = {
    systemPrompt: 'You are a helpful test agent.',
    provider: provider as AgentOptions['provider'],
    voice: 'alloy',
  };
  return {
    config: { openaiKey: 'sk-test', elevenlabsKey: 'el-test' },
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

/** Capture the StreamHandler's registered onEvent callback (real subscription). */
function captureEventCallback(
  adapter: OpenAIRealtimeAdapter | ElevenLabsConvAIAdapter,
): { current: ((type: string, data: unknown) => Promise<void>) | undefined } {
  const box: { current: ((type: string, data: unknown) => Promise<void>) | undefined } = {
    current: undefined,
  };
  const realOnEvent = adapter.onEvent.bind(adapter);
  vi.spyOn(adapter, 'onEvent').mockImplementation((cb) => {
    box.current = cb as (type: string, data: unknown) => Promise<void>;
    realOnEvent(cb as never);
  });
  return box;
}

/** Build a real GA adapter with a real EventEmitter WS, connect() stubbed. */
function makeRealtimeAdapter(gate: boolean): { adapter: OpenAIRealtime2Adapter; ws: FakeWs } {
  const ws = new FakeWs();
  const adapter = new OpenAIRealtime2Adapter(
    'sk-test',
    'gpt-realtime-2',
    'alloy',
    'You are a helpful test agent.',
    undefined,
    undefined,
    { gateResponseOnTranscript: gate },
  );
  vi.spyOn(adapter, 'connect').mockResolvedValue(undefined);
  // Adopt the fake WS without the GA event-name shim (we feed v1-named frames
  // directly) and arm the real heartbeat + message listener so the adapter's
  // real item-tracking parses the frames we emit below.
  (adapter as unknown as { ws: FakeWs }).ws = ws;
  (adapter as unknown as { armHeartbeatAndListener(): void }).armHeartbeatAndListener();
  return { adapter, ws };
}

/**
 * Drive the adapter's REAL message listener with a v1-named in-flight response
 * item + one audio delta so `currentResponseItemId` / `currentResponseAudioMs`
 * / `currentResponseFirstAudioAt` are set by the real parser. truncate() /
 * cancelResponse() both no-op unless an item is in flight, so this is required
 * to exercise the barge-in wire output.
 *
 * `firstAudioAgeMs` back-dates the (real) `currentResponseFirstAudioAt` field
 * the parser stamps, so the legacy anti-flicker gate (which measures wall-clock
 * elapsed since first audio) sees a response that has been playing long enough
 * NOT to be suppressed. Without this the delta + speech_started land in the
 * same tick → 0 ms elapsed → suppressed. The field models "this response
 * started N ms ago", an authentic state the parser would reach on a real call.
 */
function seedInFlightResponse(ws: FakeWs, adapter: OpenAIRealtime2Adapter, firstAudioAgeMs = 1000): void {
  ws.emit(
    'message',
    Buffer.from(JSON.stringify({ type: 'response.output_item.added', item: { id: 'item-1' } })),
  );
  // 8000 bytes of g711 ≈ 1000 ms of byte-counted audio (caps audio_end_ms).
  const delta = Buffer.alloc(8000).toString('base64');
  ws.emit('message', Buffer.from(JSON.stringify({ type: 'response.audio.delta', delta })));
  (adapter as unknown as { currentResponseFirstAudioAt: number }).currentResponseFirstAudioAt =
    Date.now() - firstAudioAgeMs;
}

describe('[mocked] OpenAI Realtime barge-in dispatch', () => {
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

  it('server-managed (gate=false): speech_started → sendClear + conversation.item.truncate, NO response.cancel, NO recordBargeinDetected', async () => {
    const bridge = makeBridge();
    const { adapter, ws } = makeRealtimeAdapter(false);
    const events = captureEventCallback(adapter);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, 'openai_realtime'),
      new FakeWs() as unknown as WSWebSocket,
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-server-managed');
    await handler.handleCallStart('CA-server-managed');
    expect(events.current).toBeDefined();

    const metricsAcc = (handler as unknown as {
      metricsAcc: { recordBargeinDetected: () => void; anchorUserSpeechStart: () => void };
    }).metricsAcc;
    const bargeSpy = vi.spyOn(metricsAcc, 'recordBargeinDetected');
    const anchorSpy = vi.spyOn(metricsAcc, 'anchorUserSpeechStart');

    // Put an assistant response in flight so truncate has an item to bound.
    seedInFlightResponse(ws, adapter);
    // Mark agent as mid-speech so the barge-in actually closes the turn.
    (handler as unknown as { responseAudioStarted: boolean }).responseAudioStarted = true;

    await events.current!('speech_started', null);

    // (a) carrier buffer cleared
    expect(bridge.sendClear).toHaveBeenCalledTimes(1);
    // (b) truncate sent with a bounded audio_end_ms
    const truncs = framesOfType(ws, 'conversation.item.truncate');
    expect(truncs).toHaveLength(1);
    expect(truncs[0].item_id).toBe('item-1');
    expect(truncs[0].content_index).toBe(0);
    expect(typeof truncs[0].audio_end_ms).toBe('number');
    expect(truncs[0].audio_end_ms as number).toBeGreaterThanOrEqual(0);
    // NO response.cancel — the server cancels server-side (interrupt_response=true)
    expect(framesOfType(ws, 'response.cancel')).toHaveLength(0);
    // Engine barge-in metrics must NOT fire in server-managed mode
    expect(bargeSpy).not.toHaveBeenCalled();
    expect(anchorSpy).not.toHaveBeenCalled();
  });

  it('legacy opt-out (gate=true): speech_started → full cancelResponse (truncate + response.cancel) + recordBargeinDetected + anchorUserSpeechStart', async () => {
    const bridge = makeBridge();
    const { adapter, ws } = makeRealtimeAdapter(true);
    const events = captureEventCallback(adapter);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, 'openai_realtime'),
      new FakeWs() as unknown as WSWebSocket,
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-legacy');
    await handler.handleCallStart('CA-legacy');
    expect(events.current).toBeDefined();

    const metricsAcc = (handler as unknown as {
      metricsAcc: { recordBargeinDetected: () => void; anchorUserSpeechStart: () => void };
    }).metricsAcc;
    const bargeSpy = vi.spyOn(metricsAcc, 'recordBargeinDetected');
    const anchorSpy = vi.spyOn(metricsAcc, 'anchorUserSpeechStart');

    seedInFlightResponse(ws, adapter);
    (handler as unknown as { responseAudioStarted: boolean }).responseAudioStarted = true;

    await events.current!('speech_started', null);

    expect(bridge.sendClear).toHaveBeenCalledTimes(1);
    // FULL cancel: BOTH truncate AND response.cancel sent.
    expect(framesOfType(ws, 'conversation.item.truncate')).toHaveLength(1);
    expect(framesOfType(ws, 'response.cancel')).toHaveLength(1);
    // Legacy engine barge-in metrics fire.
    expect(bargeSpy).toHaveBeenCalledTimes(1);
    expect(anchorSpy).toHaveBeenCalledTimes(1);
  });

  it('legacy opt-out (gate=true): anti-flicker gate SUPPRESSES an early barge-in (< 500 ms of audio)', async () => {
    const bridge = makeBridge();
    const { adapter, ws } = makeRealtimeAdapter(true);
    const events = captureEventCallback(adapter);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, 'openai_realtime'),
      new FakeWs() as unknown as WSWebSocket,
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-legacy-gate');
    await handler.handleCallStart('CA-legacy-gate');

    // Seed an in-flight item with a FRESH first-audio timestamp (just now), so
    // elapsed < MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_NO_AEC (500 ms) and the
    // legacy anti-flicker gate fires. The real parser stamps
    // currentResponseFirstAudioAt = Date.now() on the first delta.
    ws.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'response.output_item.added', item: { id: 'item-x' } })),
    );
    ws.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'response.audio.delta', delta: Buffer.alloc(160).toString('base64') })),
    );
    (handler as unknown as { responseAudioStarted: boolean }).responseAudioStarted = true;

    await events.current!('speech_started', null);

    // Suppressed: no sendClear, no truncate, no cancel.
    expect(bridge.sendClear).not.toHaveBeenCalled();
    expect(framesOfType(ws, 'conversation.item.truncate')).toHaveLength(0);
    expect(framesOfType(ws, 'response.cancel')).toHaveLength(0);
  });

  it('server-managed (gate=false): NO anti-flicker gate — an early barge-in (< 500 ms) STILL fires truncate', async () => {
    const bridge = makeBridge();
    const { adapter, ws } = makeRealtimeAdapter(false);
    const events = captureEventCallback(adapter);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, 'openai_realtime'),
      new FakeWs() as unknown as WSWebSocket,
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-server-early');
    await handler.handleCallStart('CA-server-early');

    // Fresh first-audio timestamp, tiny delta — would be suppressed by the
    // legacy gate, but server-managed mode has NO gate.
    ws.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'response.output_item.added', item: { id: 'item-y' } })),
    );
    ws.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'response.audio.delta', delta: Buffer.alloc(160).toString('base64') })),
    );
    (handler as unknown as { responseAudioStarted: boolean }).responseAudioStarted = true;

    await events.current!('speech_started', null);

    expect(bridge.sendClear).toHaveBeenCalledTimes(1);
    expect(framesOfType(ws, 'conversation.item.truncate')).toHaveLength(1);
    expect(framesOfType(ws, 'response.cancel')).toHaveLength(0);
  });

  it('ConvAI interruption → sendClear ONLY: no truncate, no response.cancel, no recordBargeinDetected', async () => {
    const bridge = makeBridge();
    const convaiWs = new FakeWs();
    const adapter = new ElevenLabsConvAIAdapter('el-test', 'agent-1');
    vi.spyOn(adapter, 'connect').mockResolvedValue(undefined);
    (adapter as unknown as { ws: FakeWs }).ws = convaiWs;
    const events = captureEventCallback(adapter);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, 'elevenlabs_convai'),
      new FakeWs() as unknown as WSWebSocket,
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-convai');
    await handler.handleCallStart('CA-convai');
    expect(events.current).toBeDefined();

    const metricsAcc = (handler as unknown as {
      metricsAcc: { recordBargeinDetected: () => void; anchorUserSpeechStart: () => void };
    }).metricsAcc;
    const bargeSpy = vi.spyOn(metricsAcc, 'recordBargeinDetected');
    const anchorSpy = vi.spyOn(metricsAcc, 'anchorUserSpeechStart');

    (handler as unknown as { responseAudioStarted: boolean }).responseAudioStarted = true;

    await events.current!('interruption', null);

    // sendClear only.
    expect(bridge.sendClear).toHaveBeenCalledTimes(1);
    // ConvAI has no truncate / cancel concept and no item tracking — the adapter
    // must not emit either frame on its socket.
    expect(framesOfType(convaiWs, 'conversation.item.truncate')).toHaveLength(0);
    expect(framesOfType(convaiWs, 'response.cancel')).toHaveLength(0);
    // No engine barge-in metrics for ConvAI.
    expect(bargeSpy).not.toHaveBeenCalled();
    expect(anchorSpy).not.toHaveBeenCalled();
  });
});

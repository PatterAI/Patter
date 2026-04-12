/**
 * Integration test: Twilio + OpenAI Realtime
 *
 * Simulates an inbound Twilio call handled by an OpenAI Realtime adapter.
 * Mocks at the network boundary (WebSocket, fetch).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { fakeAudioBuffer } from '../setup';
import type { WebSocket as WSWebSocket } from 'ws';

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

function makeTwilioBridge(): TelephonyBridge {
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
  };
}

function makeMockRealtimeAdapter() {
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    sendText: vi.fn().mockResolvedValue(undefined),
    cancelResponse: vi.fn(),
    onEvent: vi.fn(),
    sendFunctionResult: vi.fn().mockResolvedValue(undefined),
  };
}

function makeDeps(
  bridge: TelephonyBridge,
  adapter: ReturnType<typeof makeMockRealtimeAdapter>,
  overrides?: Partial<StreamHandlerDeps>,
): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-oai-key', twilioSid: 'AC123', twilioToken: 'tok' },
    agent: {
      systemPrompt: 'You are a test Twilio Realtime agent',
      provider: 'openai_realtime',
      model: 'gpt-4o-mini-realtime-preview',
      voice: 'alloy',
    },
    bridge,
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(adapter),
    sanitizeVariables: vi.fn((raw) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl) => tpl),
    ...overrides,
  };
}

describe('Integration: Twilio + OpenAI Realtime', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: async () => ({}), text: async () => '',
    } as Response);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('full inbound call lifecycle: start -> audio -> stop', async () => {
    const bridge = makeTwilioBridge();
    const adapter = makeMockRealtimeAdapter();
    const onCallStart = vi.fn().mockResolvedValue(undefined);
    const onCallEnd = vi.fn().mockResolvedValue(undefined);

    const deps = makeDeps(bridge, adapter, { onCallStart, onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    // 1. Call start
    handler.setStreamSid('MZ-stream-123');
    await handler.handleCallStart('CA-call-123');

    expect(onCallStart).toHaveBeenCalledWith(
      expect.objectContaining({
        call_id: 'CA-call-123',
        caller: '+15551111111',
        callee: '+15552222222',
      }),
    );
    expect(adapter.connect).toHaveBeenCalledOnce();

    // 2. Audio frames flow through the pipeline
    const audio = fakeAudioBuffer(20);
    handler.handleAudio(audio);
    expect(adapter.sendAudio).toHaveBeenCalledWith(audio);

    // 3. More audio
    handler.handleAudio(fakeAudioBuffer(20));
    expect(adapter.sendAudio).toHaveBeenCalledTimes(2);

    // 4. Disconnect
    await handler.handleStop();
    expect(adapter.close).toHaveBeenCalled();
    expect(onCallEnd).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'CA-call-123' }),
    );
  });

  it('adapter audio events flow back to Twilio bridge', async () => {
    const bridge = makeTwilioBridge();
    const adapter = makeMockRealtimeAdapter();

    // Capture the onEvent callback
    let eventCallback: ((type: string, data: unknown) => Promise<void>) | undefined;
    adapter.onEvent.mockImplementation((cb: (type: string, data: unknown) => Promise<void>) => {
      eventCallback = cb;
    });

    const deps = makeDeps(bridge, adapter);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    handler.setStreamSid('MZ-stream-456');
    await handler.handleCallStart('CA-call-456');

    // Simulate adapter emitting audio
    expect(eventCallback).toBeDefined();
    await eventCallback!('audio', Buffer.from('test-audio'));

    expect(bridge.sendAudio).toHaveBeenCalledWith(ws, expect.any(String), 'MZ-stream-456');
    expect(bridge.sendMark).toHaveBeenCalled();
  });

  it('interruption clears audio via bridge', async () => {
    const bridge = makeTwilioBridge();
    const adapter = makeMockRealtimeAdapter();

    let eventCallback: ((type: string, data: unknown) => Promise<void>) | undefined;
    adapter.onEvent.mockImplementation((cb: (type: string, data: unknown) => Promise<void>) => {
      eventCallback = cb;
    });

    const deps = makeDeps(bridge, adapter);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    handler.setStreamSid('MZ-stream-int');
    await handler.handleCallStart('CA-call-int');

    await eventCallback!('speech_started', null);
    expect(bridge.sendClear).toHaveBeenCalledWith(ws, 'MZ-stream-int');
    // cancelResponse requires instanceof OpenAIRealtimeAdapter check, which
    // fails for mock objects. The bridge.sendClear call validates the integration.
  });

  it('DTMF events fire onTranscript callback', async () => {
    const bridge = makeTwilioBridge();
    const adapter = makeMockRealtimeAdapter();
    adapter.onEvent.mockImplementation(() => {});

    const onTranscript = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps(bridge, adapter, { onTranscript });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    await handler.handleCallStart('CA-call-dtmf');

    await handler.handleDtmf('9');
    // sendText requires instanceof OpenAIRealtimeAdapter check — not available on mocks.
    // The onTranscript callback validates the DTMF was processed.
    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ text: '[DTMF: 9]' }),
    );
  });

  it('cleanup on WebSocket close fires call end only once', async () => {
    const bridge = makeTwilioBridge();
    const adapter = makeMockRealtimeAdapter();
    adapter.onEvent.mockImplementation(() => {});

    const onCallEnd = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps(bridge, adapter, { onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    await handler.handleCallStart('CA-call-close');

    await handler.handleWsClose();
    await handler.handleWsClose(); // duplicate
    expect(onCallEnd).toHaveBeenCalledTimes(1);
  });
});

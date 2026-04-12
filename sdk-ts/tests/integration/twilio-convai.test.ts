/**
 * Integration test: Twilio + ElevenLabs ConvAI
 *
 * Simulates an inbound Twilio call handled by an ElevenLabs ConvAI adapter.
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
    send: vi.fn(), close: vi.fn(), on: vi.fn(), once: vi.fn(),
    readyState: 1, removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makeTwilioBridge(): TelephonyBridge {
  return {
    label: 'Twilio',
    telephonyProvider: 'twilio',
    sendAudio: vi.fn(), sendMark: vi.fn(), sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  };
}

function makeMockConvAIAdapter() {
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    onEvent: vi.fn(),
  };
}

function makeDeps(
  bridge: TelephonyBridge,
  adapter: ReturnType<typeof makeMockConvAIAdapter>,
  overrides?: Partial<StreamHandlerDeps>,
): StreamHandlerDeps {
  return {
    config: {},
    agent: {
      systemPrompt: 'You are a ConvAI agent',
      provider: 'elevenlabs_convai',
      elevenlabsKey: 'el-key',
      elevenlabsAgentId: 'agent-id',
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

describe('Integration: Twilio + ElevenLabs ConvAI', () => {
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
    const adapter = makeMockConvAIAdapter();
    const onCallStart = vi.fn().mockResolvedValue(undefined);
    const onCallEnd = vi.fn().mockResolvedValue(undefined);

    const deps = makeDeps(bridge, adapter, { onCallStart, onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    handler.setStreamSid('MZ-convai-001');
    await handler.handleCallStart('CA-convai-001');

    expect(onCallStart).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'CA-convai-001' }),
    );
    expect(adapter.connect).toHaveBeenCalledOnce();

    // Audio flows to adapter
    const audio = fakeAudioBuffer(20);
    handler.handleAudio(audio);
    expect(adapter.sendAudio).toHaveBeenCalledWith(audio);

    // Stop
    await handler.handleStop();
    expect(adapter.close).toHaveBeenCalled();
    expect(onCallEnd).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'CA-convai-001' }),
    );
  });

  it('adapter audio events flow to Twilio bridge', async () => {
    const bridge = makeTwilioBridge();
    const adapter = makeMockConvAIAdapter();

    let eventCallback: ((type: string, data: unknown) => Promise<void>) | undefined;
    adapter.onEvent.mockImplementation((cb: (type: string, data: unknown) => Promise<void>) => {
      eventCallback = cb;
    });

    const deps = makeDeps(bridge, adapter);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    handler.setStreamSid('MZ-convai-002');
    await handler.handleCallStart('CA-convai-002');

    await eventCallback!('audio', Buffer.from('convai-audio'));
    expect(bridge.sendAudio).toHaveBeenCalledWith(ws, expect.any(String), 'MZ-convai-002');
  });

  it('interruption sends clear event', async () => {
    const bridge = makeTwilioBridge();
    const adapter = makeMockConvAIAdapter();

    let eventCallback: ((type: string, data: unknown) => Promise<void>) | undefined;
    adapter.onEvent.mockImplementation((cb: (type: string, data: unknown) => Promise<void>) => {
      eventCallback = cb;
    });

    const deps = makeDeps(bridge, adapter);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    handler.setStreamSid('MZ-convai-int');
    await handler.handleCallStart('CA-convai-int');

    await eventCallback!('interruption', null);
    expect(bridge.sendClear).toHaveBeenCalledWith(ws, 'MZ-convai-int');
  });

  it('cleanup on disconnect fires call end once', async () => {
    const bridge = makeTwilioBridge();
    const adapter = makeMockConvAIAdapter();
    adapter.onEvent.mockImplementation(() => {});

    const onCallEnd = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps(bridge, adapter, { onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    await handler.handleCallStart('CA-convai-end');

    await handler.handleStop();
    await handler.handleWsClose();
    expect(onCallEnd).toHaveBeenCalledTimes(1);
  });
});

/**
 * Integration test: Telnyx + ElevenLabs ConvAI
 *
 * Simulates an inbound Telnyx call handled by an ElevenLabs ConvAI adapter.
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

function makeTelnyxBridge(): TelephonyBridge {
  return {
    label: 'Telnyx',
    telephonyProvider: 'telnyx',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockImplementation(async (_callId: string, ws: WSWebSocket) => { ws.close(); }),
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
      systemPrompt: 'You are a Telnyx ConvAI agent',
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

describe('Integration: Telnyx + ElevenLabs ConvAI', () => {
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
    const bridge = makeTelnyxBridge();
    const adapter = makeMockConvAIAdapter();
    const onCallStart = vi.fn().mockResolvedValue(undefined);
    const onCallEnd = vi.fn().mockResolvedValue(undefined);

    const deps = makeDeps(bridge, adapter, { onCallStart, onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tc-001');

    expect(onCallStart).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'ctrl-tc-001' }),
    );
    expect(adapter.connect).toHaveBeenCalledOnce();

    // Audio
    const audio = fakeAudioBuffer(20, 16000);
    handler.handleAudio(audio);
    expect(adapter.sendAudio).toHaveBeenCalledWith(audio);

    // Stop
    await handler.handleStop();
    expect(adapter.close).toHaveBeenCalled();
    expect(onCallEnd).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'ctrl-tc-001' }),
    );
  });

  it('adapter audio events flow to Telnyx bridge', async () => {
    const bridge = makeTelnyxBridge();
    const adapter = makeMockConvAIAdapter();

    let eventCallback: ((type: string, data: unknown) => Promise<void>) | undefined;
    adapter.onEvent.mockImplementation((cb: (type: string, data: unknown) => Promise<void>) => {
      eventCallback = cb;
    });

    const deps = makeDeps(bridge, adapter);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    await handler.handleCallStart('ctrl-tc-audio');

    await eventCallback!('audio', Buffer.from('telnyx-convai-audio'));
    expect(bridge.sendAudio).toHaveBeenCalledWith(ws, expect.any(String), '');
  });

  it('interruption triggers clear event', async () => {
    const bridge = makeTelnyxBridge();
    const adapter = makeMockConvAIAdapter();

    let eventCallback: ((type: string, data: unknown) => Promise<void>) | undefined;
    adapter.onEvent.mockImplementation((cb: (type: string, data: unknown) => Promise<void>) => {
      eventCallback = cb;
    });

    const deps = makeDeps(bridge, adapter);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    await handler.handleCallStart('ctrl-tc-int');

    await eventCallback!('speech_started', null);
    expect(bridge.sendClear).toHaveBeenCalled();
  });

  it('metrics store records call lifecycle', async () => {
    const store = new MetricsStore();
    const startSpy = vi.spyOn(store, 'recordCallStart');
    const endSpy = vi.spyOn(store, 'recordCallEnd');

    const bridge = makeTelnyxBridge();
    const adapter = makeMockConvAIAdapter();
    adapter.onEvent.mockImplementation(() => {});

    const deps = makeDeps(bridge, adapter, { metricsStore: store });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tc-metrics');
    expect(startSpy).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'ctrl-tc-metrics' }),
    );

    await handler.handleStop();
    expect(endSpy).toHaveBeenCalled();
  });

  it('cleanup on disconnect is idempotent', async () => {
    const bridge = makeTelnyxBridge();
    const adapter = makeMockConvAIAdapter();
    adapter.onEvent.mockImplementation(() => {});

    const onCallEnd = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps(bridge, adapter, { onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tc-close');
    await handler.handleWsClose();
    await handler.handleWsClose();
    expect(onCallEnd).toHaveBeenCalledTimes(1);
  });
});

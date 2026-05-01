import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { fakeAudioBuffer } from '../setup';
import type { WebSocket as WSWebSocket } from 'ws';

// ---------------------------------------------------------------------------
// Factory helpers
// ---------------------------------------------------------------------------

function makeMockBridge(overrides?: Partial<TelephonyBridge>): TelephonyBridge {
  return {
    label: 'TestBridge',
    telephonyProvider: 'twilio',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

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

function makeMockAdapter() {
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

function makeDeps(overrides?: Partial<StreamHandlerDeps>): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-oai-key' },
    agent: {
      systemPrompt: 'Test agent',
      provider: 'openai_realtime',
    },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(makeMockAdapter()),
    sanitizeVariables: vi.fn((raw) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) {
        safe[k] = String(v);
      }
      return safe;
    }),
    resolveVariables: vi.fn((tpl, vars) => {
      let result = tpl;
      for (const [k, v] of Object.entries(vars)) {
        result = result.replaceAll(`{${k}}`, v);
      }
      return result;
    }),
    ...overrides,
  };
}

describe('StreamHandler', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
    fetchSpy.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    } as Response);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --- Construction ---

  it('creates a StreamHandler without error', () => {
    const deps = makeDeps();
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    expect(handler).toBeDefined();
  });

  // --- handleCallStart ---

  describe('handleCallStart()', () => {
    it('initializes the realtime adapter for non-pipeline mode', async () => {
      const mockAdapter = makeMockAdapter();
      const deps = makeDeps({
        buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-123');

      expect(deps.buildAIAdapter).toHaveBeenCalledOnce();
      expect(mockAdapter.connect).toHaveBeenCalledOnce();
    });

    it('fires onCallStart callback', async () => {
      const onCallStart = vi.fn().mockResolvedValue(undefined);
      const deps = makeDeps({ onCallStart });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-456');

      expect(onCallStart).toHaveBeenCalledWith(
        expect.objectContaining({
          call_id: 'call-456',
          caller: '+15551111111',
          callee: '+15552222222',
          direction: 'inbound',
        }),
      );
    });

    it('records call start in metrics store', async () => {
      const store = new MetricsStore();
      const spy = vi.spyOn(store, 'recordCallStart');
      const deps = makeDeps({ metricsStore: store });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-789');
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ call_id: 'call-789' }),
      );
    });

    it('resolves variables in system prompt', async () => {
      const resolveVariables = vi.fn((tpl: string, vars: Record<string, string>) => {
        let result = tpl;
        for (const [k, v] of Object.entries(vars)) {
          result = result.replaceAll(`{${k}}`, v);
        }
        return result;
      });
      const deps = makeDeps({
        agent: {
          systemPrompt: 'Hello {name}!',
          provider: 'openai_realtime',
          variables: { name: 'World' },
        },
        resolveVariables,
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-var');
      expect(resolveVariables).toHaveBeenCalled();
      const calledWith = (deps.buildAIAdapter as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(calledWith).toBe('Hello World!');
    });

    it('passes custom params to onCallStart', async () => {
      const onCallStart = vi.fn().mockResolvedValue(undefined);
      const deps = makeDeps({ onCallStart });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-cp', { company: 'Acme' });
      expect(onCallStart).toHaveBeenCalledWith(
        expect.objectContaining({ custom_params: { company: 'Acme' } }),
      );
    });

    it('starts recording when enabled (Twilio)', async () => {
      fetchSpy.mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({}),
        text: async () => '',
      } as Response);
      const deps = makeDeps({
        config: { openaiKey: 'key', twilioSid: 'AC123', twilioToken: 'tok' },
        recording: true,
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      // Must be a valid Twilio CallSid (CA + 32 hex) — the recording start
      // now validates the SID to prevent SSRF against the Twilio API.
      await handler.handleCallStart('CA00000000000000000000000000000001');
      // Should have called fetch for recording + adapter connect
      const recordingCall = fetchSpy.mock.calls.find(
        (c) => typeof c[0] === 'string' && c[0].includes('Recordings.json'),
      );
      expect(recordingCall).toBeDefined();
    });
  });

  // --- handleAudio ---

  describe('handleAudio()', () => {
    it('forwards audio to adapter in realtime mode', async () => {
      const mockAdapter = makeMockAdapter();
      const deps = makeDeps({
        buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-audio');

      const audio = fakeAudioBuffer(20);
      handler.handleAudio(audio);

      expect(mockAdapter.sendAudio).toHaveBeenCalledWith(audio);
    });
  });

  // --- handleDtmf ---

  describe('handleDtmf()', () => {
    it('fires onTranscript with DTMF digit', async () => {
      const onTranscript = vi.fn().mockResolvedValue(undefined);
      const deps = makeDeps({ onTranscript });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-dtmf');

      await handler.handleDtmf('5');
      expect(onTranscript).toHaveBeenCalledWith(
        expect.objectContaining({ text: '[DTMF: 5]' }),
      );
    });
  });

  // --- handleStop / handleWsClose ---

  describe('handleStop()', () => {
    it('fires call end and records metrics', async () => {
      const onCallEnd = vi.fn().mockResolvedValue(undefined);
      const store = new MetricsStore();
      const spy = vi.spyOn(store, 'recordCallEnd');
      const deps = makeDeps({ onCallEnd, metricsStore: store });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-stop');

      await handler.handleStop();
      expect(onCallEnd).toHaveBeenCalledWith(
        expect.objectContaining({ call_id: 'call-stop' }),
      );
      expect(spy).toHaveBeenCalled();
    });
  });

  describe('handleWsClose()', () => {
    it('fires call end only once (idempotent)', async () => {
      const onCallEnd = vi.fn().mockResolvedValue(undefined);
      const deps = makeDeps({ onCallEnd });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-close');

      await handler.handleWsClose();
      await handler.handleWsClose(); // second call should be no-op
      expect(onCallEnd).toHaveBeenCalledTimes(1);
    });
  });

  // --- setStreamSid ---

  describe('setStreamSid()', () => {
    it('sets the stream SID for Twilio media events', async () => {
      const bridge = makeMockBridge();
      const mockAdapter = makeMockAdapter();
      // When an adapter event fires 'audio', it uses bridge.sendAudio with streamSid
      mockAdapter.onEvent.mockImplementation(async (cb: (type: string, data: unknown) => Promise<void>) => {
        await cb('audio', Buffer.from('test'));
      });
      const deps = makeDeps({
        bridge,
        buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      handler.setStreamSid('stream-abc');
      await handler.handleCallStart('call-sid');

      // After adapter event fires 'audio', sendAudio should use the stream SID
      expect(bridge.sendAudio).toHaveBeenCalledWith(
        ws,
        expect.any(String),
        'stream-abc',
      );
    });
  });

  // --- TelephonyBridge interface abstraction ---

  describe('TelephonyBridge abstraction', () => {
    it('uses bridge.sendClear on interruption event', async () => {
      const bridge = makeMockBridge();
      const mockAdapter = makeMockAdapter();
      mockAdapter.onEvent.mockImplementation(async (cb: (type: string, data: unknown) => Promise<void>) => {
        await cb('speech_started', null);
      });
      const deps = makeDeps({
        bridge,
        buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-int');

      expect(bridge.sendClear).toHaveBeenCalled();
    });

    it('uses bridge label for logging context', () => {
      const bridge = makeMockBridge({ label: 'CustomBridge' });
      const deps = makeDeps({ bridge });
      const ws = makeMockWs();
      // Just verify construction doesn't throw with custom bridge
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      expect(handler).toBeDefined();
    });
  });
});

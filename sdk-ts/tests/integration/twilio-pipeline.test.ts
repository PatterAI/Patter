/**
 * Integration test: Twilio + Pipeline mode
 *
 * Simulates an inbound Twilio call handled by the STT -> LLM -> TTS pipeline.
 * Mocks at the network boundary (WebSocket, fetch, STT, TTS).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { fakeAudioBuffer, fakeMulawBuffer } from '../setup';
import type { WebSocket as WSWebSocket } from 'ws';

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(), close: vi.fn(), on: vi.fn(), once: vi.fn(),
    readyState: 1, removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makeMockStt() {
  let transcriptCb: ((t: { isFinal?: boolean; text?: string }) => Promise<void>) | undefined;
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    onTranscript: vi.fn((cb: (t: { isFinal?: boolean; text?: string }) => Promise<void>) => {
      transcriptCb = cb;
    }),
    get requestId() { return 'dg-req-123'; },
    emitTranscript(text: string) {
      return transcriptCb?.({ isFinal: true, text });
    },
  };
}

function makeTwilioBridge(mockStt: ReturnType<typeof makeMockStt>): TelephonyBridge {
  return {
    label: 'Twilio',
    telephonyProvider: 'twilio',
    sendAudio: vi.fn(), sendMark: vi.fn(), sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(mockStt),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  };
}

function makeDeps(
  bridge: TelephonyBridge,
  overrides?: Partial<StreamHandlerDeps>,
): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-oai-key' },
    agent: {
      systemPrompt: 'You are a pipeline agent',
      provider: 'pipeline',
      deepgramKey: 'dg-key',
      elevenlabsKey: 'el-key',
    },
    bridge,
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn(),
    sanitizeVariables: vi.fn((raw) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl) => tpl),
    ...overrides,
  };
}

describe('Integration: Twilio + Pipeline', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: async () => ({}), text: async () => '',
    } as Response);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('call start initializes STT pipeline', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const onCallStart = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps(bridge, { onCallStart });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    handler.setStreamSid('MZ-pipe-001');
    await handler.handleCallStart('CA-pipe-001');

    expect(stt.connect).toHaveBeenCalledOnce();
    expect(stt.onTranscript).toHaveBeenCalled();
    expect(onCallStart).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'CA-pipe-001' }),
    );
  });

  it('audio frames are sent to STT, not adapter', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const deps = makeDeps(bridge);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    handler.setStreamSid('MZ-pipe-audio');
    await handler.handleCallStart('CA-pipe-audio');

    const audio = fakeMulawBuffer(20);
    handler.handleAudio(audio);
    expect(stt.sendAudio).toHaveBeenCalledWith(audio);
    // buildAIAdapter should NOT be called for pipeline mode
    expect(deps.buildAIAdapter).not.toHaveBeenCalled();
  });

  it('pipeline mode with onMessage handler processes transcripts', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const onMessage = vi.fn().mockResolvedValue('I heard you!');
    const onTranscript = vi.fn().mockResolvedValue(undefined);

    const deps = makeDeps(bridge, { onMessage, onTranscript });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    handler.setStreamSid('MZ-pipe-msg');
    await handler.handleCallStart('CA-pipe-msg');

    // Simulate STT producing a transcript
    await stt.emitTranscript('Hello pipeline');

    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ role: 'user', text: 'Hello pipeline' }),
    );
    expect(onMessage).toHaveBeenCalledWith(
      expect.objectContaining({ text: 'Hello pipeline' }),
    );
    // TTS would stream audio to bridge — but since we can't mock ElevenLabsTTS constructor easily,
    // verify the message handler was called
  });

  it('cleanup on stop closes STT', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const onCallEnd = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps(bridge, { onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-pipe-stop');
    await handler.handleStop();

    expect(stt.close).toHaveBeenCalled();
    expect(onCallEnd).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'CA-pipe-stop' }),
    );
  });

  it('WS close after stop does not double-fire call end', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const onCallEnd = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps(bridge, { onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-pipe-double');
    await handler.handleStop();
    await handler.handleWsClose();

    expect(onCallEnd).toHaveBeenCalledTimes(1);
  });
});

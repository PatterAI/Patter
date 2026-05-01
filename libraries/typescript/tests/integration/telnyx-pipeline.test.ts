/**
 * Integration test: Telnyx + Pipeline mode
 *
 * Simulates an inbound Telnyx call handled by the STT -> LLM -> TTS pipeline.
 * Mocks at the network boundary (WebSocket, fetch, STT, TTS).
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

function makeMockStt() {
  let transcriptCb: ((t: { isFinal?: boolean; text?: string }) => Promise<void>) | undefined;
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    onTranscript: vi.fn((cb: (t: { isFinal?: boolean; text?: string }) => Promise<void>) => {
      transcriptCb = cb;
    }),
    get requestId() { return 'dg-telnyx-req'; },
    emitTranscript(text: string) {
      return transcriptCb?.({ isFinal: true, text });
    },
  };
}

function makeTelnyxBridge(mockStt: ReturnType<typeof makeMockStt>): TelephonyBridge {
  return {
    label: 'Telnyx',
    telephonyProvider: 'telnyx',
    sendAudio: vi.fn(), sendMark: vi.fn(), sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockImplementation(async (_callId: string, ws: WSWebSocket) => { ws.close(); }),
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
      systemPrompt: 'You are a Telnyx pipeline agent',
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

describe('Integration: Telnyx + Pipeline', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: async () => ({}), text: async () => '',
    } as Response);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('call start initializes STT for Telnyx pipeline', async () => {
    const stt = makeMockStt();
    const bridge = makeTelnyxBridge(stt);
    const onCallStart = vi.fn().mockResolvedValue(undefined);

    const deps = makeDeps(bridge, { onCallStart });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tp-001');

    expect(stt.connect).toHaveBeenCalledOnce();
    expect(stt.onTranscript).toHaveBeenCalled();
    expect(onCallStart).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'ctrl-tp-001' }),
    );
  });

  it('Telnyx 16kHz PCM audio flows to STT', async () => {
    const stt = makeMockStt();
    const bridge = makeTelnyxBridge(stt);
    const deps = makeDeps(bridge);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tp-audio');

    // Telnyx with the default streaming_start (PCMU bidirectional) sends
    // mulaw 8 kHz inbound. The pipeline transcodes to PCM16 16 kHz before
    // STT, so the mock sees the transcoded buffer (different from input).
    const { fakeMulawBuffer } = await import('../setup');
    const mulaw = fakeMulawBuffer(20, 8000);
    await handler.handleAudio(mulaw);
    expect(stt.sendAudio).toHaveBeenCalledTimes(1);
    const forwarded = (stt.sendAudio as any).mock.calls[0][0] as Buffer;
    // Transcoded output is PCM16 @ 16 kHz. The stateful 8k→16k resampler defers
    // the last input sample until the next chunk so it can be correctly
    // interpolated. For the first chunk of N mulaw bytes the output is
    // (N - 1) * 4 bytes (the deferred sample pairs with the next chunk).
    expect(forwarded.length).toBe((mulaw.length - 1) * 4);
  });

  it('pipeline mode processes transcript with onMessage handler', async () => {
    const stt = makeMockStt();
    const bridge = makeTelnyxBridge(stt);
    const onMessage = vi.fn().mockResolvedValue('Telnyx pipeline response');
    const onTranscript = vi.fn().mockResolvedValue(undefined);

    const deps = makeDeps(bridge, { onMessage, onTranscript });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tp-msg');

    await stt.emitTranscript('Hello from Telnyx');

    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ role: 'user', text: 'Hello from Telnyx' }),
    );
    expect(onMessage).toHaveBeenCalledWith(
      expect.objectContaining({ text: 'Hello from Telnyx' }),
    );
  });

  it('ignores non-final transcripts', async () => {
    const stt = makeMockStt();
    const bridge = makeTelnyxBridge(stt);
    const onMessage = vi.fn().mockResolvedValue('response');

    const deps = makeDeps(bridge, { onMessage });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tp-nonfinal');

    // Get the transcript callback
    const transcriptCb = stt.onTranscript.mock.calls[0][0];
    // Emit non-final transcript
    await transcriptCb({ isFinal: false, text: 'partial' });

    expect(onMessage).not.toHaveBeenCalled();
  });

  it('cleanup on stop closes STT and fires call end', async () => {
    const stt = makeMockStt();
    const bridge = makeTelnyxBridge(stt);
    const onCallEnd = vi.fn().mockResolvedValue(undefined);

    const deps = makeDeps(bridge, { onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tp-stop');
    await handler.handleStop();

    expect(stt.close).toHaveBeenCalled();
    expect(onCallEnd).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'ctrl-tp-stop' }),
    );
  });

  it('WS close after stop does not double-fire call end', async () => {
    const stt = makeMockStt();
    const bridge = makeTelnyxBridge(stt);
    const onCallEnd = vi.fn().mockResolvedValue(undefined);

    const deps = makeDeps(bridge, { onCallEnd });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tp-double');
    await handler.handleStop();
    await handler.handleWsClose();

    expect(onCallEnd).toHaveBeenCalledTimes(1);
  });

  it('metrics recorded for Telnyx pipeline calls', async () => {
    const store = new MetricsStore();
    const startSpy = vi.spyOn(store, 'recordCallStart');
    const endSpy = vi.spyOn(store, 'recordCallEnd');

    const stt = makeMockStt();
    const bridge = makeTelnyxBridge(stt);
    const deps = makeDeps(bridge, { metricsStore: store });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('ctrl-tp-metrics');
    expect(startSpy).toHaveBeenCalledWith(
      expect.objectContaining({ call_id: 'ctrl-tp-metrics' }),
    );

    await handler.handleStop();
    expect(endSpy).toHaveBeenCalled();
  });
});

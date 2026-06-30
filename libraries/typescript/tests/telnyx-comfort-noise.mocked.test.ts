/**
 * [mocked] Realtime-engine outbound keepalive over Telnyx (comfort-noise pump).
 *
 * BUG: native-audio realtime engines (GeminiLive / OpenAIRealtime2) put NO
 * bytes on the carrier between the carrier ``start`` and the model's first
 * audio delta (cold ``connect()`` + TTFT + resampler warmup, often >1.5 s).
 * Twilio tolerates the gap; Telnyx clears the idle bidirectional RTP leg
 * (~1.6 s). The fix pumps paced μ-law-8k silence from stream-start until the
 * first real model frame.
 *
 * AUTHENTIC: the StreamHandler and the OpenAIRealtime2Adapter are REAL. The
 * pump, the silence frame, ``startComfortNoise``/``stopComfortNoise``, and the
 * real ``mulawToPcm16`` decoder are all exercised. Mocked only at the external
 * boundary — the OpenAI WebSocket transport (injected mock ``ws``), the network
 * ``connect()`` (an external-API call we cannot place in CI), and the carrier
 * ``TelephonyBridge`` (we cannot place phone calls in CI). We assert on the
 * observable outcome: ``bridge.sendAudio`` calls.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import { mulawToPcm16 } from '../src/audio/transcoding';
import type { WebSocket as WSWebSocket } from 'ws';
import type { AgentOptions } from '../src/types';

/** The exact frame the pump emits (private static — re-derived here). */
const SILENCE_FRAME_B64 = Buffer.alloc(160, 0xff).toString('base64');

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

/**
 * Build a REAL OpenAIRealtime2Adapter (a native-audio realtime engine). Stub
 * only ``connect`` (the OpenAI network handshake) and inject a mock WS.
 */
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
  return makeDeps(bridge, agent, adapter);
}

function makePipelineDeps(bridge: TelephonyBridge): StreamHandlerDeps {
  const agent: AgentOptions = {
    systemPrompt: 'You are a helpful test agent.',
    provider: 'pipeline',
    // Supply a VAD so initPipeline skips the optional SileroVAD/onnxruntime
    // import (irrelevant to the pump under test). Minimal real-shaped
    // VADProvider — never emits, never used by these assertions.
    vad: {
      processFrame: async () => null,
      close: async () => {},
    },
  };
  return makeDeps(bridge, agent, undefined);
}

function makeDeps(
  bridge: TelephonyBridge,
  agent: AgentOptions,
  adapter: OpenAIRealtime2Adapter | undefined,
): StreamHandlerDeps {
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

/** Count ``sendAudio`` calls whose payload is the silence frame. */
function countSilenceFrames(bridge: TelephonyBridge): number {
  const send = (bridge as unknown as { sendAudio: { mock: { calls: unknown[][] } } }).sendAudio;
  return send.mock.calls.filter((c) => c[1] === SILENCE_FRAME_B64).length;
}

describe('[unit] comfort-noise silence frame', () => {
  it('decodes via real mulawToPcm16 to 160 PCM16 samples of value 0 (true silence)', () => {
    const frame = Buffer.from(SILENCE_FRAME_B64, 'base64');
    expect(frame.length).toBe(160); // 20 ms @ 8 kHz μ-law, 1 byte/sample

    const pcm = mulawToPcm16(frame);
    // 160 μ-law bytes -> 160 PCM16 samples (2 bytes each) = 320 bytes.
    expect(pcm.length).toBe(320);
    const samples = pcm.length / 2;
    expect(samples).toBe(160);
    for (let i = 0; i < pcm.length; i += 2) {
      expect(pcm.readInt16LE(i)).toBe(0);
    }
  });
});

describe('[mocked] realtime-engine comfort-noise pump over Telnyx', () => {
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

  it('emits silence frames before the first model delta, then stops once it fires', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(adapterWs);
    const events = captureEventCallback(adapter);

    const handler = new StreamHandler(
      makeRealtimeDeps(bridge, adapter),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('telnyx-stream-1');
    await handler.handleCallStart('telnyx-call-1');
    expect(events.current).toBeDefined();

    // Simulate the connect->first-delta gap (~300 ms). The pump runs every
    // 20 ms, so ≥1 silence frame must reach the bridge in that window.
    await vi.advanceTimersByTimeAsync(300);
    const framesBeforeDelta = countSilenceFrames(bridge);
    expect(framesBeforeDelta).toBeGreaterThanOrEqual(1);

    // First real model audio arrives -> pump must stop.
    await events.current!('audio', Buffer.alloc(480)); // real model PCM frame
    const framesAtDelta = countSilenceFrames(bridge);

    // Advance well past several pump intervals; NO further silence frames.
    await vi.advanceTimersByTimeAsync(200);
    expect(countSilenceFrames(bridge)).toBe(framesAtDelta);
  });

  it('pipeline mode never arms the pump (no comfort-noise frames ever)', async () => {
    const bridge = makeBridge();
    const handler = new StreamHandler(
      makePipelineDeps(bridge),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('telnyx-stream-pipeline');
    await handler.handleCallStart('telnyx-call-pipeline');

    await vi.advanceTimersByTimeAsync(2000);
    expect(countSilenceFrames(bridge)).toBe(0);
  });

  it('handleStop before any model audio clears the interval (no leaked timer)', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(adapterWs);

    const handler = new StreamHandler(
      makeRealtimeDeps(bridge, adapter),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('telnyx-stream-stop');
    await handler.handleCallStart('telnyx-call-stop');

    await handler.handleStop();
    expect(
      (handler as unknown as { comfortNoiseTimer: unknown }).comfortNoiseTimer,
    ).toBeNull();

    // No further silence frames after teardown.
    const frames = countSilenceFrames(bridge);
    await vi.advanceTimersByTimeAsync(200);
    expect(countSilenceFrames(bridge)).toBe(frames);
  });

  it('handleWsClose before any model audio clears the interval (no leaked timer)', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(adapterWs);

    const handler = new StreamHandler(
      makeRealtimeDeps(bridge, adapter),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('telnyx-stream-wsclose');
    await handler.handleCallStart('telnyx-call-wsclose');

    await handler.handleWsClose();
    expect(
      (handler as unknown as { comfortNoiseTimer: unknown }).comfortNoiseTimer,
    ).toBeNull();
  });
});

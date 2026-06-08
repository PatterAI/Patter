/**
 * [mocked] OpenAI Realtime — response decoupled from Whisper transcript (issue #154).
 *
 * GOAL: the end-to-end speech-to-speech model must respond INDEPENDENTLY of
 * the Whisper input transcription. By default the response is requested the
 * moment the user stops speaking (``speech_stopped``); the Whisper
 * ``transcript_input`` becomes a pure display side-channel. The opt-out flag
 * ``gateResponseOnTranscript`` restores the legacy transcript-gated path.
 *
 * AUTHENTIC: the StreamHandler and the OpenAIRealtime2Adapter are REAL. Only
 * mocked at the external boundary — the OpenAI WebSocket transport (injected
 * mock ``ws``) and the network ``connect()`` (an external-API call we cannot
 * place in CI). ``requestResponse()`` produces a real ``response.create`` JSON
 * frame on the mock socket; we assert on that observable outcome.
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
 * Build a REAL OpenAIRealtime2Adapter (extends OpenAIRealtimeAdapter, so the
 * stream-handler ``instanceof`` feature gates fire). Stub only ``connect``
 * (the OpenAI network handshake) and inject a mock WS so ``requestResponse``
 * lands a real ``response.create`` frame we can observe.
 */
function makeRealAdapter(
  gateResponseOnTranscript: boolean,
  ws: WSWebSocket,
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
  // ``connect`` opens a real WS to OpenAI — stub it (external boundary) and
  // wire the mock socket the real ``requestResponse`` sends through.
  vi.spyOn(adapter, 'connect').mockResolvedValue(undefined);
  (adapter as unknown as { ws: WSWebSocket }).ws = ws;
  return adapter;
}

function makeDeps(
  bridge: TelephonyBridge,
  adapter: OpenAIRealtime2Adapter,
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

/** Count ``response.create`` frames sent on the mock socket. */
function countResponseCreate(ws: WSWebSocket): number {
  const send = (ws as unknown as { send: { mock: { calls: unknown[][] } } }).send;
  let n = 0;
  for (const call of send.mock.calls) {
    const arg = call[0];
    if (typeof arg === 'string' && JSON.parse(arg).type === 'response.create') n++;
  }
  return n;
}

describe('[mocked] OpenAI Realtime response decoupled from Whisper transcript', () => {
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

  it('default (flag false): session enables create_response=true (server auto-creates); Patter never drives response.create', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(false, adapterWs);
    const events = captureEventCallback(adapter);

    // The decoupling mechanism: the GA session config turns ON server-side
    // auto-response, so the model replies on input_audio_buffer.committed —
    // independently of the Whisper transcript, with no client-side race.
    const cfg = (adapter as unknown as { buildGASessionConfig(): Record<string, unknown> }).buildGASessionConfig();
    const td = (cfg.audio as { input: { turn_detection: Record<string, unknown> } }).input.turn_detection;
    expect(td.create_response).toBe(true);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-stream-decoupled');
    await handler.handleCallStart('CA-call-decoupled');
    expect(events.current).toBeDefined();

    // The SERVER creates the response (create_response=true); Patter must NOT
    // drive response.create itself — on speech_stopped it would race the
    // server-side buffer commit, and on transcript_input it would double up.
    await events.current!('speech_stopped', null);
    expect(countResponseCreate(adapterWs)).toBe(0);
    await events.current!('transcript_input', 'What is the weather today?');
    expect(countResponseCreate(adapterWs)).toBe(0);
  });

  it('legacy (flag true): create_response=false; Patter drives response.create on transcript_input, not speech_stopped', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(true, adapterWs);
    const events = captureEventCallback(adapter);

    // Legacy gating keeps server auto-response OFF so Patter can defer
    // response.create until the hallucination filter accepts the transcript.
    const cfg = (adapter as unknown as { buildGASessionConfig(): Record<string, unknown> }).buildGASessionConfig();
    const td = (cfg.audio as { input: { turn_detection: Record<string, unknown> } }).input.turn_detection;
    expect(td.create_response).toBe(false);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-stream-legacy');
    await handler.handleCallStart('CA-call-legacy');
    expect(events.current).toBeDefined();

    // User stops speaking -> NO response.create yet (gated on transcript).
    await events.current!('speech_stopped', null);
    expect(countResponseCreate(adapterWs)).toBe(0);

    // Whisper transcript arrives -> response.create fires now.
    await events.current!('transcript_input', 'What is the weather today?');
    expect(countResponseCreate(adapterWs)).toBe(1);
  });

  it('hallucination filter drops phantom transcripts from DISPLAY regardless of flag (default false)', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(false, adapterWs);
    const events = captureEventCallback(adapter);
    const onTranscript = vi.fn().mockResolvedValue(undefined);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, { onTranscript }),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-stream-hallu');
    await handler.handleCallStart('CA-call-hallu');

    // Response is created server-side (create_response=true); Patter drives
    // nothing on speech_stopped.
    await events.current!('speech_stopped', null);
    expect(countResponseCreate(adapterWs)).toBe(0);

    // A known Whisper caption-credit hallucination on silence/echo arrives —
    // it must NOT be surfaced as a user turn (no onTranscript user event), and
    // Patter must still drive no response.create (the transcript is
    // display-only; the filter never gates/cancels the server-created
    // response). Issue #154 narrowed the blocklist to non-speech artefacts
    // only, so we use a caption credit ('thank you for watching') that is
    // still dropped — a bare 'thank you.' is now a legitimate user reply.
    await events.current!('transcript_input', 'Thank you for watching.');
    const userEvents = onTranscript.mock.calls.filter(
      (c) => (c[0] as { role?: string }).role === 'user',
    );
    expect(userEvents).toHaveLength(0);
    expect(countResponseCreate(adapterWs)).toBe(0);
  });

  it('real transcript reaches onTranscript as a user turn (display side-channel), flag false', async () => {
    const bridge = makeBridge();
    const adapterWs = makeMockWs();
    const adapter = makeRealAdapter(false, adapterWs);
    const events = captureEventCallback(adapter);
    const onTranscript = vi.fn().mockResolvedValue(undefined);

    const handler = new StreamHandler(
      makeDeps(bridge, adapter, { onTranscript }),
      makeMockWs(),
      '+15551111111',
      '+15552222222',
    );
    handler.setStreamSid('MZ-stream-display');
    await handler.handleCallStart('CA-call-display');

    await events.current!('speech_stopped', null);
    await events.current!('transcript_input', 'I would like to book a haircut.');

    const userEvents = onTranscript.mock.calls.filter(
      (c) => (c[0] as { role?: string }).role === 'user',
    );
    expect(userEvents).toHaveLength(1);
    expect((userEvents[0][0] as { text: string }).text).toBe(
      'I would like to book a haircut.',
    );
  });
});

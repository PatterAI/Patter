/**
 * Tests for the prewarm-handoff (FIX A) — keep parked WSs OPEN and adopt
 * them at call connect, instead of close-and-reopen which doesn't warm
 * TLS on Node `ws`.
 *
 * Coverage:
 *  1. `Patter.parkProviderConnections` invokes `openParkedConnection`
 *     on the configured STT / TTS adapters.
 *  2. The parked WS stays OPEN (readyState === OPEN) past the historic
 *     250 ms idle window.
 *  3. `popPrewarmedConnections` returns the parked handles and removes
 *     them from the cache (consume-once semantics).
 *  4. `closePrewarmedConnections` (and `recordPrewarmWaste`) drains
 *     parked sockets cleanly.
 *  5. A WS that died between park and adopt does NOT crash the consumer
 *     — the consumer falls back to fresh open. (Verified via the
 *     adapter-level `synthesizeStream` dropping a closed parked WS.)
 *
 * Tests use authentic real-code paths — only the upstream provider
 * boundary is mocked. See `.claude/rules/authentic-tests.md`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { Patter } from '../../src/client';
import { Twilio } from '../../src/index';
import type { AgentOptions } from '../../src/types';
import type { STTAdapter, TTSAdapter, STTTranscriptCallback } from '../../src/provider-factory';
import type { ElevenLabsParkedWS } from '../../src/providers/elevenlabs-ws-tts';

// Stub the EmbeddedServer so constructing a Patter doesn't spin up a
// real HTTP server.
vi.mock('../../src/server', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../../src/server')>();
  class MockEmbeddedServer {
    voicemailMessage = '';
    popPrewarmAudio: (id: string) => Buffer | undefined = () => undefined;
    popPrewarmedConnections: (id: string) => unknown = () => undefined;
    recordPrewarmWaste: (id: string) => void = () => undefined;
    metricsStore = { recordCallInitiated: vi.fn() } as unknown as {
      recordCallInitiated: (...args: unknown[]) => void;
    };
    start = vi.fn().mockResolvedValue(undefined);
    stop = vi.fn().mockResolvedValue(undefined);
    constructor(..._args: unknown[]) {}
  }
  return {
    ...orig,
    EmbeddedServer: MockEmbeddedServer,
  };
});

// A minimal fake WS that exposes the readyState lifecycle but no
// network traffic. ws.OPEN === 1 by convention.
class FakeWS {
  readyState = 1; // OPEN
  closed = false;
  close(): void {
    this.readyState = 3; // CLOSED
    this.closed = true;
  }
}

class StubSTTWithPark implements STTAdapter {
  warmupCalls = 0;
  parkCalls = 0;
  adoptCalls = 0;
  connectCalls = 0;
  parkedWs: FakeWS | null = null;
  async connect(): Promise<void> {
    this.connectCalls += 1;
  }
  sendAudio(_pcm: Buffer): void {}
  onTranscript(_cb: STTTranscriptCallback): void {}
  async close(): Promise<void> {}
  async warmup(): Promise<void> {
    this.warmupCalls += 1;
  }
  async openParkedConnection(): Promise<unknown> {
    this.parkCalls += 1;
    this.parkedWs = new FakeWS();
    return this.parkedWs;
  }
  adoptWebSocket(_ws: unknown): void {
    this.adoptCalls += 1;
  }
}

class StubTTSWithPark implements TTSAdapter {
  warmupCalls = 0;
  parkCalls = 0;
  adoptCalls = 0;
  parkedHandle: ElevenLabsParkedWS | null = null;
  // eslint-disable-next-line require-yield
  async *synthesizeStream(_text: string): AsyncGenerator<Buffer> {
    return;
  }
  async warmup(): Promise<void> {
    this.warmupCalls += 1;
  }
  async openParkedConnection(): Promise<ElevenLabsParkedWS> {
    this.parkCalls += 1;
    this.parkedHandle = { ws: new FakeWS() as unknown as import('ws').WebSocket, bosSent: true };
    return this.parkedHandle;
  }
  adoptWebSocket(parked: ElevenLabsParkedWS): void {
    this.adoptCalls += 1;
    void parked;
  }
}

function makePatter(): Patter {
  return new Patter({
    carrier: new Twilio({
      accountSid: 'ACtest000000000000000000000000000',
      authToken: 'tok',
    }),
    phoneNumber: '+15551234567',
    webhookUrl: 'example.test',
  });
}

describe('[unit] prewarm-handoff', () => {
  let phone: Patter;
  beforeEach(() => {
    phone = makePatter();
  });

  it('parkProviderConnections invokes openParkedConnection on STT and TTS', async () => {
    const stt = new StubSTTWithPark();
    const tts = new StubTTSWithPark();
    const agent: AgentOptions = {
      systemPrompt: 'p',
      provider: 'pipeline',
      stt,
      tts,
    };
    // Private method — accessed via cast for the test only.
    (phone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CAtest1');
    // Wait microtask + small delay for the async park tasks.
    await new Promise<void>((r) => setTimeout(r, 30));
    expect(stt.parkCalls).toBe(1);
    expect(tts.parkCalls).toBe(1);
  });

  it('parked WS stays OPEN past the historic 250 ms idle window', async () => {
    const stt = new StubSTTWithPark();
    const tts = new StubTTSWithPark();
    const agent: AgentOptions = { systemPrompt: 'p', provider: 'pipeline', stt, tts };
    (phone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CAtest2');
    await new Promise<void>((r) => setTimeout(r, 350));
    expect(stt.parkedWs?.readyState).toBe(1); // OPEN
    expect(tts.parkedHandle?.ws.readyState).toBe(1);
  });

  it('popPrewarmedConnections returns parked handles exactly once', async () => {
    const stt = new StubSTTWithPark();
    const tts = new StubTTSWithPark();
    const agent: AgentOptions = { systemPrompt: 'p', provider: 'pipeline', stt, tts };
    (phone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CAtest3');
    await new Promise<void>((r) => setTimeout(r, 30));
    const slot = phone.popPrewarmedConnections('CAtest3');
    expect(slot).toBeDefined();
    expect(slot?.stt).toBe(stt.parkedWs);
    expect(slot?.tts).toBe(tts.parkedHandle);
    // Second pop should be undefined — slot already drained.
    expect(phone.popPrewarmedConnections('CAtest3')).toBeUndefined();
  });

  it('closePrewarmedConnections closes parked sockets and drains the slot', async () => {
    const stt = new StubSTTWithPark();
    const tts = new StubTTSWithPark();
    const agent: AgentOptions = { systemPrompt: 'p', provider: 'pipeline', stt, tts };
    (phone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CAtest4');
    await new Promise<void>((r) => setTimeout(r, 30));
    expect(stt.parkedWs?.readyState).toBe(1);
    phone.closePrewarmedConnections('CAtest4');
    expect(stt.parkedWs?.readyState).toBe(3); // CLOSED
    expect(tts.parkedHandle?.ws.readyState).toBe(3);
    // Slot drained.
    expect(phone.popPrewarmedConnections('CAtest4')).toBeUndefined();
  });

  it('recordPrewarmWaste also drains parked sockets (call ended pre-pickup)', async () => {
    const stt = new StubSTTWithPark();
    const tts = new StubTTSWithPark();
    const agent: AgentOptions = { systemPrompt: 'p', provider: 'pipeline', stt, tts };
    (phone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CAtest5');
    await new Promise<void>((r) => setTimeout(r, 30));
    phone.recordPrewarmWaste('CAtest5');
    expect(stt.parkedWs?.readyState).toBe(3);
    expect(tts.parkedHandle?.ws.readyState).toBe(3);
  });

  it('does nothing when neither provider exposes openParkedConnection', () => {
    // Adapters without the optional method must not allocate a slot.
    const minimalStt: STTAdapter = {
      async connect(): Promise<void> {},
      sendAudio(): void {},
      onTranscript(): void {},
      async close(): Promise<void> {},
    };
    const agent: AgentOptions = { systemPrompt: 'p', provider: 'pipeline', stt: minimalStt };
    (phone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CAtest6');
    // Slot was never created — pop returns undefined.
    expect(phone.popPrewarmedConnections('CAtest6')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// OpenAI Realtime parking + adoption
// ---------------------------------------------------------------------------

describe('[unit] prewarm-handoff — OpenAI Realtime', () => {
  let phone: Patter;
  let openParkedSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(async () => {
    phone = new Patter({
      carrier: new Twilio({
        accountSid: 'ACtest000000000000000000000000000',
        authToken: 'tok',
      }),
      phoneNumber: '+15551234567',
      webhookUrl: 'example.test',
      openaiKey: 'sk-test',
    });
    // Spy on the prototype method so any transient adapter instance the
    // SDK builds inside ``parkProviderConnections`` returns a controllable
    // FakeWS instead of opening a real WebSocket.
    const realtimeModule = await import('../../src/providers/openai-realtime');
    openParkedSpy = vi
      .spyOn(realtimeModule.OpenAIRealtimeAdapter.prototype, 'openParkedConnection')
      .mockImplementation(async () => new FakeWS() as unknown as import('ws').WebSocket);
  });

  it('parkProviderConnections opens a primed Realtime WS for openai_realtime agents', async () => {
    const agent: AgentOptions = {
      systemPrompt: 'p',
      provider: 'openai_realtime',
      voice: 'alloy',
    };
    (phone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CArt1');
    await new Promise<void>((r) => setTimeout(r, 30));
    expect(openParkedSpy).toHaveBeenCalledTimes(1);
    const slot = phone.popPrewarmedConnections('CArt1');
    expect(slot).toBeDefined();
    expect(slot?.openaiRealtime).toBeDefined();
  });

  it('skips Realtime parking when the OpenAI key is missing', async () => {
    const keylessPhone = new Patter({
      carrier: new Twilio({
        accountSid: 'ACtest000000000000000000000000000',
        authToken: 'tok',
      }),
      phoneNumber: '+15551234567',
      webhookUrl: 'example.test',
    });
    const agent: AgentOptions = { systemPrompt: 'p', provider: 'openai_realtime' };
    (keylessPhone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CArt2');
    await new Promise<void>((r) => setTimeout(r, 30));
    expect(openParkedSpy).not.toHaveBeenCalled();
    expect(keylessPhone.popPrewarmedConnections('CArt2')).toBeUndefined();
  });

  it('Realtime park failure is best-effort and does not block other providers', async () => {
    openParkedSpy.mockRejectedValueOnce(new Error('network down'));
    const stt = new StubSTTWithPark();
    const agent: AgentOptions = {
      systemPrompt: 'p',
      provider: 'openai_realtime',
      stt,
    };
    (phone as unknown as { parkProviderConnections: (a: AgentOptions, id: string) => void })
      .parkProviderConnections(agent, 'CArt3');
    await new Promise<void>((r) => setTimeout(r, 30));
    // STT still parked successfully.
    expect(stt.parkCalls).toBe(1);
    const slot = phone.popPrewarmedConnections('CArt3');
    // ``openaiRealtime`` key absent on the slot; STT key present.
    expect(slot?.openaiRealtime).toBeUndefined();
    expect(slot?.stt).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Built-in tools (transfer_call / end_call) MUST land in the primed session
// so adopted parked sessions can still call them. Regression for the bug
// where ``buildRealtimeWarmupAdapter`` constructed the transient adapter
// with no ``tools`` argument and the session.update sent during ringing
// carried an empty tool list.
// ---------------------------------------------------------------------------

describe('[unit] prewarm-handoff — built-in tools in primed session', () => {
  function makeRealtimePhone(): Patter {
    return new Patter({
      carrier: new Twilio({
        accountSid: 'ACtest000000000000000000000000000',
        authToken: 'tok',
      }),
      phoneNumber: '+15551234567',
      webhookUrl: 'example.test',
      openaiKey: 'sk-test',
    });
  }

  it('warmup adapter is constructed with user tools + transfer_call + end_call', () => {
    const phone = makeRealtimePhone();

    const customTool = {
      name: 'lookup_order',
      description: 'Look up an order by id',
      parameters: {
        type: 'object',
        properties: { orderId: { type: 'string' } },
        required: ['orderId'],
      },
    } as const;

    const agent: AgentOptions = {
      systemPrompt: 'p',
      provider: 'openai_realtime',
      voice: 'alloy',
      tools: [customTool],
    };

    const adapter = (
      phone as unknown as {
        buildRealtimeWarmupAdapter: (a: AgentOptions) => unknown;
      }
    ).buildRealtimeWarmupAdapter(agent);
    expect(adapter).not.toBeNull();

    // ``tools`` is a private field on ``OpenAIRealtimeAdapter`` — access
    // via bracket to inspect the wired value.
    const tools = (adapter as { tools?: Array<{ name: string }> }).tools;
    expect(tools).toBeDefined();
    const names = (tools ?? []).map((t) => t.name);
    expect(names).toContain('lookup_order');
    expect(names).toContain('transfer_call');
    expect(names).toContain('end_call');
  });

  it('warmup adapter still injects transfer_call + end_call when agent has no tools', () => {
    const phone = makeRealtimePhone();
    const agent: AgentOptions = {
      systemPrompt: 'p',
      provider: 'openai_realtime',
    };

    const adapter = (
      phone as unknown as {
        buildRealtimeWarmupAdapter: (a: AgentOptions) => unknown;
      }
    ).buildRealtimeWarmupAdapter(agent);
    expect(adapter).not.toBeNull();

    const tools = (adapter as { tools?: Array<{ name: string }> }).tools;
    expect(tools).toBeDefined();
    const names = (tools ?? []).map((t) => t.name);
    expect(names).toEqual(['transfer_call', 'end_call']);
  });
});

// ---------------------------------------------------------------------------
// Adopt-failure recovery — when ``adoptWebSocket`` raises, the partially-
// adopted adapter is in an inconsistent state (messageListenerAttached may
// be true, heartbeat may have started, currentResponseItemId may carry leaked
// state from the parked session). Calling ``connect()`` on that carcass races
// ``session.created`` against stale state. Handler must recreate the adapter
// before falling through to the cold ``connect()`` path.
// ---------------------------------------------------------------------------

describe('[unit] prewarm-handoff — adapter recreation on adopt failure', () => {
  it('recreates the adapter when adoptWebSocket throws, then connects on the fresh one', async () => {
    const { StreamHandler } = await import('../../src/stream-handler');
    const { OpenAIRealtimeAdapter } = await import('../../src/providers/openai-realtime');
    const { MetricsStore } = await import('../../src/dashboard/store');
    const { RemoteMessageHandler } = await import('../../src/remote-message');
    const wsMod = await import('ws');

    // Force ``adoptWebSocket`` on every adapter instance to throw — the
    // SDK must respond by rebuilding the adapter before falling through.
    const adoptSpy = vi
      .spyOn(OpenAIRealtimeAdapter.prototype, 'adoptWebSocket')
      .mockImplementation(() => {
        throw new Error('adopt blew up');
      });
    const connectSpy = vi
      .spyOn(OpenAIRealtimeAdapter.prototype, 'connect')
      .mockResolvedValue(undefined);
    const onEventSpy = vi
      .spyOn(OpenAIRealtimeAdapter.prototype, 'onEvent')
      .mockImplementation(() => undefined);

    // Parked WS — alive (readyState OPEN); ``adoptWebSocket`` will fail
    // before it gets attached.
    const parkedWs = {
      readyState: 1,
      close: () => undefined,
    } as unknown as import('ws').WebSocket;

    const built: Array<unknown> = [];
    const deps = {
      config: { openaiKey: 'sk-test' },
      agent: {
        systemPrompt: 'Test agent',
        provider: 'openai_realtime' as const,
      },
      bridge: {
        label: 'TestBridge',
        telephonyProvider: 'twilio' as const,
        sendAudio: vi.fn(),
        sendMark: vi.fn(),
        sendClear: vi.fn(),
        transferCall: vi.fn().mockResolvedValue(undefined),
        endCall: vi.fn().mockResolvedValue(undefined),
        createStt: vi.fn().mockReturnValue(null),
        queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
      },
      metricsStore: new MetricsStore(),
      pricing: null,
      remoteHandler: new RemoteMessageHandler(),
      recording: false,
      buildAIAdapter: vi.fn().mockImplementation((_prompt: string) => {
        const instance = new OpenAIRealtimeAdapter('sk-test', 'gpt-4o-mini-realtime-preview');
        built.push(instance);
        return instance;
      }),
      sanitizeVariables: vi.fn((raw: Record<string, unknown>) => {
        const safe: Record<string, string> = {};
        for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
        return safe;
      }),
      resolveVariables: vi.fn((tpl: string) => tpl),
      popPrewarmedConnections: vi.fn().mockReturnValue({ openaiRealtime: parkedWs }),
    };

    const mockWs = {
      send: vi.fn(),
      close: vi.fn(),
      on: vi.fn(),
      once: vi.fn(),
      readyState: 1,
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as import('ws').WebSocket;

    const handler = new StreamHandler(
      deps,
      mockWs,
      '+15551111111',
      '+15552222222',
    );

    await handler.handleCallStart('CAtest-recreate');

    // buildAIAdapter was called twice: first to build the original
    // adapter (whose adopt failed), then again to recreate it for the
    // cold connect path.
    expect(deps.buildAIAdapter).toHaveBeenCalledTimes(2);
    // Both adapters were OpenAIRealtimeAdapter instances.
    expect(built).toHaveLength(2);
    expect(built[0]).toBeInstanceOf(OpenAIRealtimeAdapter);
    expect(built[1]).toBeInstanceOf(OpenAIRealtimeAdapter);
    expect(built[0]).not.toBe(built[1]);
    // adopt was called once (on the first adapter, threw). connect was
    // called once (on the fresh adapter).
    expect(adoptSpy).toHaveBeenCalledTimes(1);
    expect(connectSpy).toHaveBeenCalledTimes(1);

    adoptSpy.mockRestore();
    connectSpy.mockRestore();
    onEventSpy.mockRestore();
  });
});

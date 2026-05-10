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

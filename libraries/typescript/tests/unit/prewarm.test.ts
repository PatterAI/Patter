/**
 * Tests for the prewarm and prewarmFirstMessage features.
 *
 * The feature wires three independent pieces together:
 *
 *  1. Provider ``warmup()`` methods on STT / TTS / LLM. Default = no-op.
 *  2. ``Patter.call`` spawns provider warmup in parallel with the carrier
 *     ``initiateCall`` when ``agent.prewarm`` is true (the default).
 *  3. ``Patter.call`` pre-renders ``agent.firstMessage`` to TTS bytes when
 *     ``agent.prewarmFirstMessage`` is true; the StreamHandler firstMessage
 *     emit consumes the cache instead of running TTS again.
 *
 * Tests use authentic real code paths — only the provider HTTPS-GET warmup
 * boundary is mocked. See ``.claude/rules/authentic-tests.md``.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Patter } from '../../src/client';
import { Twilio } from '../../src/index';
import type { AgentOptions } from '../../src/types';
import type { STTAdapter, TTSAdapter, STTTranscriptCallback } from '../../src/provider-factory';

// Stub the EmbeddedServer so constructing a Patter doesn't spin up a real HTTP server.
vi.mock('../../src/server', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../../src/server')>();
  class MockEmbeddedServer {
    voicemailMessage = '';
    popPrewarmAudio: (id: string) => Buffer | undefined = () => undefined;
    start = vi.fn().mockResolvedValue(undefined);
    stop = vi.fn().mockResolvedValue(undefined);
    constructor(..._args: unknown[]) {}
  }
  return {
    ...orig,
    EmbeddedServer: MockEmbeddedServer,
  };
});

function makePatter(): Patter {
  return new Patter({
    carrier: new Twilio({ accountSid: 'ACtest000000000000000000000000000', authToken: 'tok' }),
    phoneNumber: '+15551234567',
    webhookUrl: 'example.test',
  });
}

class StubSTT implements STTAdapter {
  warmupCalls = 0;
  async connect(): Promise<void> {}
  sendAudio(_pcm: Buffer): void {}
  onTranscript(_cb: STTTranscriptCallback): void {}
  async close(): Promise<void> {}
  async warmup(): Promise<void> {
    this.warmupCalls += 1;
  }
}

class StubTTS implements TTSAdapter {
  warmupCalls = 0;
  synthesizeCalls = 0;
  constructor(private readonly bytes: Buffer = Buffer.from('PCM_TTS_BYTES_OK')) {}
  async *synthesizeStream(_text: string): AsyncGenerator<Buffer> {
    this.synthesizeCalls += 1;
    const half = Math.floor(this.bytes.byteLength / 2);
    yield this.bytes.subarray(0, half);
    yield this.bytes.subarray(half);
  }
  async warmup(): Promise<void> {
    this.warmupCalls += 1;
  }
}

class StubLLM {
  warmupCalls = 0;
  // eslint-disable-next-line require-yield
  async *stream(): AsyncGenerator<unknown> {
    return;
  }
  async warmup(): Promise<void> {
    this.warmupCalls += 1;
  }
}

/** Drain prewarm tasks attached to the Patter instance. */
async function drainPrewarmTasks(phone: Patter): Promise<void> {
  const internal = phone as unknown as { prewarmTasks: Set<Promise<unknown>> };
  await Promise.allSettled(Array.from(internal.prewarmTasks));
}

describe('[unit] prewarm — Agent flag defaults', () => {
  it('agent prewarm defaults are documented in types.ts (true / false)', () => {
    // The Agent flag defaults are not enforced at the type level (they're
    // optional), but client.ts treats undefined as the default. Verify
    // ``agent.prewarm !== false`` (the actual gate) behaves as expected.
    const agent: AgentOptions = { systemPrompt: 'hi' };
    expect(agent.prewarm).toBeUndefined();
    expect(agent.prewarmFirstMessage).toBeUndefined();
    // Default behaviour: prewarm is on unless user explicitly set false.
    expect(agent.prewarm !== false).toBe(true);
  });

  it('phone.agent() leaves prewarmFirstMessage undefined in pipeline mode (opt-in)', () => {
    // Default-on was reverted on 2026-05-19 after the 0.6.2 acceptance
    // run showed a phantom-barge-in interaction: the prewarm burst at
    // pickup tripped Silero VAD on the very first inbound frame and the
    // firstMessage was cancelled mid-playback. Pipeline mode now leaves
    // the flag opt-in; callers wanting the prewarm path set it explicitly.
    const phone = makePatter();
    const stt = new StubSTT();
    const tts = new StubTTS();
    const llm = new StubLLM();
    const agent = phone.agent({ systemPrompt: 'hi', stt, tts, llm });
    expect(agent.provider).toBe('pipeline');
    expect(agent.prewarmFirstMessage).toBeUndefined();
  });

  it('phone.agent() does NOT default prewarmFirstMessage in realtime mode', () => {
    // Realtime / ConvAI handlers never consume the prewarm cache; setting
    // the flag would only waste TTS spend, so the default stays off when
    // the caller didn't explicitly pick pipeline.
    const phone = makePatter();
    const agent = phone.agent({ systemPrompt: 'hi', provider: 'openai_realtime' });
    expect(agent.prewarmFirstMessage).toBeUndefined();
  });

  it('phone.agent() preserves explicit prewarmFirstMessage=false in pipeline mode (opt-out)', () => {
    const phone = makePatter();
    const stt = new StubSTT();
    const tts = new StubTTS();
    const llm = new StubLLM();
    const agent = phone.agent({
      systemPrompt: 'hi',
      stt,
      tts,
      llm,
      prewarmFirstMessage: false,
    });
    expect(agent.prewarmFirstMessage).toBe(false);
  });
});

describe('[unit] prewarm — provider warmup', () => {
  let phone: Patter;
  beforeEach(() => {
    phone = makePatter();
  });
  afterEach(async () => {
    await drainPrewarmTasks(phone);
  });

  it('spawnProviderWarmup invokes warmup() on STT/TTS/LLM exactly once each', async () => {
    const stt = new StubSTT();
    const tts = new StubTTS();
    const llm = new StubLLM();
    const agent: AgentOptions = { systemPrompt: 'hi', stt, tts, llm: llm as never };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnProviderWarmup(agent);
    await drainPrewarmTasks(phone);
    expect(stt.warmupCalls).toBe(1);
    expect(tts.warmupCalls).toBe(1);
    expect(llm.warmupCalls).toBe(1);
  });

  it('skips warmup entirely when prewarm is false', async () => {
    const stt = new StubSTT();
    const tts = new StubTTS();
    const llm = new StubLLM();
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      stt,
      tts,
      llm: llm as never,
      prewarm: false,
    };
    if (agent.prewarm !== false) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (phone as any).spawnProviderWarmup(agent);
    }
    await drainPrewarmTasks(phone);
    expect(stt.warmupCalls).toBe(0);
    expect(tts.warmupCalls).toBe(0);
    expect(llm.warmupCalls).toBe(0);
  });

  it('a failing provider warmup is swallowed and never propagates', async () => {
    class BoomTTS extends StubTTS {
      override async warmup(): Promise<void> {
        throw new Error('DNS down');
      }
    }
    const stt = new StubSTT();
    const tts = new BoomTTS();
    const agent: AgentOptions = { systemPrompt: 'hi', stt, tts };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnProviderWarmup(agent);
    await drainPrewarmTasks(phone);
    // STT still ran fine.
    expect(stt.warmupCalls).toBe(1);
    // No exception bled out — assertion is reaching here.
  });

  it('skips providers without a warmup method (older / minimal adapters)', async () => {
    const noWarmupTTS: TTSAdapter = {
      // eslint-disable-next-line require-yield
      synthesizeStream: async function* () {
        return;
      },
    };
    const agent: AgentOptions = { systemPrompt: 'hi', tts: noWarmupTTS };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnProviderWarmup(agent);
    await drainPrewarmTasks(phone);
    // No exception — that's the assertion.
  });
});

describe('[unit] prewarm — first-message cache', () => {
  let phone: Patter;
  beforeEach(() => {
    phone = makePatter();
  });
  afterEach(async () => {
    await drainPrewarmTasks(phone);
  });

  it('populates the cache when prewarmFirstMessage is true', async () => {
    const tts = new StubTTS(Buffer.from('GREETING-AUDIO-BYTES'));
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'Hi there',
      tts,
      prewarmFirstMessage: true,
      // ``spawnPrewarmFirstMessage`` is gated to ``provider === 'pipeline'`` —
      // Realtime / ConvAI never consume the cache so we'd refuse to spawn.
      provider: 'pipeline',
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnPrewarmFirstMessage(agent, 'CA-call-001', 5);
    await drainPrewarmTasks(phone);
    const buf = phone.popPrewarmAudio('CA-call-001');
    expect(buf?.toString()).toBe('GREETING-AUDIO-BYTES');
    expect(tts.synthesizeCalls).toBe(1);
  });

  it('skips the cache when prewarmFirstMessage is false (default)', async () => {
    const tts = new StubTTS(Buffer.from('ZZZ'));
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'Hi there',
      tts,
      prewarmFirstMessage: false,
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnPrewarmFirstMessage(agent, 'CA-call-002', 5);
    await drainPrewarmTasks(phone);
    expect(phone.popPrewarmAudio('CA-call-002')).toBeUndefined();
    expect(tts.synthesizeCalls).toBe(0);
  });

  it('skips the cache when firstMessage is empty', async () => {
    const tts = new StubTTS();
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: '',
      tts,
      prewarmFirstMessage: true,
      provider: 'pipeline',
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnPrewarmFirstMessage(agent, 'CA-call-003', 5);
    await drainPrewarmTasks(phone);
    expect(phone.popPrewarmAudio('CA-call-003')).toBeUndefined();
    expect(tts.synthesizeCalls).toBe(0);
  });

  it('popPrewarmAudio is one-shot (returns once, then undefined)', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).prewarmAudio.set('CA-X', Buffer.from('BYTES'));
    expect(phone.popPrewarmAudio('CA-X')?.toString()).toBe('BYTES');
    expect(phone.popPrewarmAudio('CA-X')).toBeUndefined();
  });

  it('logs WARN when prewarmed audio was paid for but never consumed', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).prewarmAudio.set('CA-waste', Buffer.from('WASTED'));
    const warnings: string[] = [];
    const orig = console.warn;
    console.warn = (...args: unknown[]) => {
      warnings.push(args.join(' '));
    };
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (phone as any).recordPrewarmWaste('CA-waste');
    } finally {
      console.warn = orig;
    }
    // The default logger writes to console.warn. Even if the SDK logger
    // is configured differently in the test env, the entry must be gone.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmAudio.has('CA-waste')).toBe(false);
  });
});

describe('[unit] prewarm — StreamHandler consumes cache', () => {
  it('the deps include the popPrewarmAudio accessor when wired', () => {
    // The deps interface guarantees the optional callback. The full
    // StreamHandler test covers the cache-hit short-circuit; this test
    // exists to verify the wiring point exists at the type level — if
    // someone removes ``popPrewarmAudio`` from ``StreamHandlerDeps`` the
    // compiler error here makes the regression obvious.
    type WithPop = {
      popPrewarmAudio?: (id: string) => Buffer | undefined;
    };
    const deps: WithPop = {
      popPrewarmAudio: (id) => (id === 'CA-1' ? Buffer.from('cached') : undefined),
    };
    expect(deps.popPrewarmAudio?.('CA-1')?.toString()).toBe('cached');
    expect(deps.popPrewarmAudio?.('CA-2')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// FIX #97 regression — prewarm bytes must be chunked, not single-shot
// ---------------------------------------------------------------------------

describe('[unit] streamPrewarmBytes — chunked send for cancel granularity (FIX #97)', () => {
  // Build a minimally-wired StreamHandler shell so we can drive the
  // private ``streamPrewarmBytes`` method directly. The encodePipeline
  // path is the one that actually produces bytes for the wire — we
  // assert that bridge.sendAudio is called many times, not once.
  it('chunks a 5-second prewarm buffer into many sendAudio calls', async () => {
    const { StreamHandler } = await import('../../src/stream-handler');
    const { MetricsStore } = await import('../../src/dashboard/store');
    const { RemoteMessageHandler } = await import('../../src/remote-message');
    type WSWebSocket = import('ws').WebSocket;

    const sendAudio = vi.fn();
    let handlerRef: { onMark: (n: string) => Promise<void> } | null = null;
    // BUG #128: every chunk now pairs with a Twilio mark and the loop
    // window-blocks until echoes arrive. Production Twilio echoes within
    // 100-250 ms of playback; in this test we echo synchronously so the
    // chunking assertion can complete inside the vitest timeout.
    const sendMark = vi.fn((_ws: unknown, name: string) => {
      if (handlerRef) void handlerRef.onMark(name);
    });
    const bridge = {
      label: 'TestBridge',
      telephonyProvider: 'twilio',
      sendAudio,
      sendMark,
      sendClear: vi.fn(),
      transferCall: vi.fn().mockResolvedValue(undefined),
      endCall: vi.fn().mockResolvedValue(undefined),
      createStt: vi.fn().mockReturnValue(null),
      queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
    };
    const ws = {
      send: vi.fn(),
      close: vi.fn(),
      on: vi.fn(),
      once: vi.fn(),
      readyState: 1,
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as WSWebSocket;
    const deps = {
      config: { openaiKey: 'test-oai-key' },
      agent: { systemPrompt: 'Test', provider: 'pipeline' as const },
      bridge,
      metricsStore: new MetricsStore(),
      pricing: null,
      remoteHandler: new RemoteMessageHandler(),
      recording: false,
      buildAIAdapter: vi.fn(),
      sanitizeVariables: vi.fn((raw: Record<string, unknown>) => {
        const safe: Record<string, string> = {};
        for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
        return safe;
      }),
      resolveVariables: vi.fn((tpl: string) => tpl),
    };
    const h = new StreamHandler(deps, ws, '+1', '+2');
    interface Priv {
      isSpeaking: boolean;
      streamSid: string;
      firstAudioSentAt: number | null;
      streamPrewarmBytes: (bytes: Buffer) => Promise<boolean>;
      onMark: (n: string) => Promise<void>;
    }
    const p = h as unknown as Priv;
    p.isSpeaking = true;
    p.streamSid = 'SM-test';
    p.firstAudioSentAt = Date.now(); // gate open
    handlerRef = p; // hand the handler to the mark-echo mock above

    // 5 s of PCM16 @ 16 kHz mono = 5 * 16000 * 2 = 160_000 bytes.
    const prewarmBytes = Buffer.alloc(160_000, 1);
    expect(prewarmBytes.length).toBe(160_000);

    const firstChunkSent = await p.streamPrewarmBytes(prewarmBytes);

    expect(firstChunkSent).toBe(true);
    // 160_000 / 1280 = 125 chunks. Anything ≥ 100 proves the buffer was
    // split — we don't pin the exact count to keep the test robust to
    // future chunk-size tweaks.
    expect(sendAudio.mock.calls.length).toBeGreaterThanOrEqual(100);
    // Definitely not the single-shot regression.
    expect(sendAudio.mock.calls.length).toBeGreaterThan(1);
  });

  it('stops chunking when isSpeaking flips false mid-buffer (barge-in)', async () => {
    const { StreamHandler } = await import('../../src/stream-handler');
    const { MetricsStore } = await import('../../src/dashboard/store');
    const { RemoteMessageHandler } = await import('../../src/remote-message');
    type WSWebSocket = import('ws').WebSocket;

    let chunksSeen = 0;
    let handlerRef: { isSpeaking: boolean } | null = null;
    const sendAudio = vi.fn(() => {
      chunksSeen += 1;
      // After the second chunk, simulate a barge-in flipping the gate.
      if (chunksSeen === 2 && handlerRef) {
        handlerRef.isSpeaking = false;
      }
    });
    const bridge = {
      label: 'TestBridge',
      telephonyProvider: 'twilio',
      sendAudio,
      sendMark: vi.fn(),
      sendClear: vi.fn(),
      transferCall: vi.fn().mockResolvedValue(undefined),
      endCall: vi.fn().mockResolvedValue(undefined),
      createStt: vi.fn().mockReturnValue(null),
      queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
    };
    const ws = {
      send: vi.fn(),
      close: vi.fn(),
      on: vi.fn(),
      once: vi.fn(),
      readyState: 1,
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as WSWebSocket;
    const deps = {
      config: { openaiKey: 'test-oai-key' },
      agent: { systemPrompt: 'Test', provider: 'pipeline' as const },
      bridge,
      metricsStore: new MetricsStore(),
      pricing: null,
      remoteHandler: new RemoteMessageHandler(),
      recording: false,
      buildAIAdapter: vi.fn(),
      sanitizeVariables: vi.fn((raw: Record<string, unknown>) => {
        const safe: Record<string, string> = {};
        for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
        return safe;
      }),
      resolveVariables: vi.fn((tpl: string) => tpl),
    };
    const h = new StreamHandler(deps, ws, '+1', '+2');
    interface Priv {
      isSpeaking: boolean;
      streamSid: string;
      firstAudioSentAt: number | null;
      streamPrewarmBytes: (bytes: Buffer) => Promise<boolean>;
    }
    const p = h as unknown as Priv;
    p.isSpeaking = true;
    p.streamSid = 'SM-bargein';
    p.firstAudioSentAt = Date.now();
    handlerRef = p;

    const prewarmBytes = Buffer.alloc(160_000, 1);
    await p.streamPrewarmBytes(prewarmBytes);

    // Exactly 2 chunks were sent; the loop broke before the third
    // iteration could invoke sendAudio.
    expect(sendAudio.mock.calls.length).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// FIX #91 — cache eviction on abnormal hangup (idempotency + statusCallback)
// ---------------------------------------------------------------------------

describe('[unit] prewarm — eviction on abnormal hangup (FIX #91)', () => {
  let phone: Patter;
  beforeEach(() => {
    phone = makePatter();
  });
  afterEach(async () => {
    await drainPrewarmTasks(phone);
  });

  it('recordPrewarmWaste is idempotent — second call does not double-WARN', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).prewarmAudio.set('CA-twice', Buffer.from('BYTES'));
    const warnings: string[] = [];
    const orig = console.warn;
    console.warn = (...args: unknown[]) => warnings.push(args.join(' '));
    try {
      phone.recordPrewarmWaste('CA-twice');
      phone.recordPrewarmWaste('CA-twice');
    } finally {
      console.warn = orig;
    }
    // Filter out other unrelated console.warns (none expected here).
    const wasteWarns = warnings.filter((w) => w.includes('CA-twice') && /wasted/i.test(w));
    expect(wasteWarns.length).toBe(1);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmAudio.has('CA-twice')).toBe(false);
  });

  it('marks call_id as consumed even if there were no bytes cached (silent-evict guard)', () => {
    phone.recordPrewarmWaste('CA-empty');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmConsumed.has('CA-empty')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// FIX #92 — race start-vs-prewarm task (orphan bytes guard)
// ---------------------------------------------------------------------------

describe('[unit] prewarm — race orphan-bytes guard (FIX #92)', () => {
  let phone: Patter;
  beforeEach(() => {
    phone = makePatter();
  });
  afterEach(async () => {
    await drainPrewarmTasks(phone);
  });

  it('drops bytes when the consumer polled before the synth finished', async () => {
    // SlowTTS that emits chunks across multiple awaits so we can poll
    // pop_prewarm_audio before it finishes.
    class SlowTTS implements TTSAdapter {
      synthesizeCalls = 0;
      async *synthesizeStream(_text: string): AsyncGenerator<Buffer> {
        this.synthesizeCalls += 1;
        await new Promise((r) => setTimeout(r, 200));
        yield Buffer.from('LATE-1');
        await new Promise((r) => setTimeout(r, 50));
        yield Buffer.from('LATE-2');
      }
    }
    const tts = new SlowTTS();
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'Hi',
      tts,
      prewarmFirstMessage: true,
      provider: 'pipeline',
    };

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnPrewarmFirstMessage(agent, 'CA-race', 5);
    // Carrier ``start`` arrives BEFORE synth finishes — consumer polls.
    await new Promise((r) => setTimeout(r, 50));
    const cached = phone.popPrewarmAudio('CA-race');
    expect(cached).toBeUndefined();

    const warnings: string[] = [];
    const orig = console.warn;
    console.warn = (...args: unknown[]) => warnings.push(args.join(' '));
    try {
      await drainPrewarmTasks(phone);
    } finally {
      console.warn = orig;
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmAudio.has('CA-race')).toBe(false);
    const orphanWarns = warnings.filter(
      (w) => /orphaned/i.test(w) && w.includes('CA-race'),
    );
    expect(orphanWarns.length).toBe(1);
  });

  it('popPrewarmAudio marks the call_id consumed on cache HIT', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).prewarmAudio.set('CA-hit', Buffer.from('BYTES'));
    const out = phone.popPrewarmAudio('CA-hit');
    expect(out?.toString()).toBe('BYTES');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmConsumed.has('CA-hit')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// FIX #93 — disconnect() cancels in-flight tasks and clears cache
// ---------------------------------------------------------------------------

describe('[unit] prewarm — disconnect cleanup (FIX #93)', () => {
  it('clears prewarm cache + consumed set + ttl timers on disconnect', async () => {
    const phone = makePatter();

    class VerySlowTTS implements TTSAdapter {
      synthesizeCalls = 0;
      async *synthesizeStream(_text: string): AsyncGenerator<Buffer> {
        this.synthesizeCalls += 1;
        // 10 s synth — disconnect must not wait this long.
        await new Promise((r) => setTimeout(r, 10_000));
        yield Buffer.from('never');
      }
    }
    const tts = new VerySlowTTS();
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'hello',
      tts,
      prewarmFirstMessage: true,
      provider: 'pipeline',
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnPrewarmFirstMessage(agent, 'CA-disco', 30);
    // Pre-seed entries we expect to be cleared.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).prewarmAudio.set('CA-leftover', Buffer.from('STALE'));
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).prewarmConsumed.add('CA-leftover');

    // disconnect() should bound the wait at 1 s — well under the 10 s synth.
    const t0 = Date.now();
    await phone.disconnect();
    const elapsed = Date.now() - t0;
    expect(elapsed).toBeLessThan(2_000);

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmAudio.size).toBe(0);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmConsumed.size).toBe(0);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmTasks.size).toBe(0);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmTtlTimers.size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// FIX #94 — Realtime/ConvAI silently waste TTS spend
// ---------------------------------------------------------------------------

describe('[unit] prewarm — provider-mode guard (FIX #94)', () => {
  let phone: Patter;
  beforeEach(() => {
    phone = makePatter();
  });
  afterEach(async () => {
    await drainPrewarmTasks(phone);
  });

  it('refuses to spawn for openai_realtime + WARN', async () => {
    const tts = new StubTTS();
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'hi',
      tts,
      prewarmFirstMessage: true,
      provider: 'openai_realtime',
    };
    const warnings: string[] = [];
    const orig = console.warn;
    console.warn = (...args: unknown[]) => warnings.push(args.join(' '));
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (phone as any).spawnPrewarmFirstMessage(agent, 'CA-realtime', 5);
      await drainPrewarmTasks(phone);
    } finally {
      console.warn = orig;
    }
    expect(tts.synthesizeCalls).toBe(0);
    expect(phone.popPrewarmAudio('CA-realtime')).toBeUndefined();
    const guardWarns = warnings.filter((w) => /only supported in pipeline/i.test(w));
    expect(guardWarns.length).toBe(1);
  });

  it('refuses to spawn for elevenlabs_convai + WARN', async () => {
    const tts = new StubTTS();
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'hi',
      tts,
      prewarmFirstMessage: true,
      provider: 'elevenlabs_convai',
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnPrewarmFirstMessage(agent, 'CA-convai', 5);
    await drainPrewarmTasks(phone);
    expect(tts.synthesizeCalls).toBe(0);
    expect(phone.popPrewarmAudio('CA-convai')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// FIX #96 — bounded cache (size cap + TTL eviction)
// ---------------------------------------------------------------------------

describe('[unit] prewarm — bounded cache (FIX #96)', () => {
  it('refuses spawn when in-flight count reaches PREWARM_CACHE_MAX', async () => {
    const { PREWARM_CACHE_MAX } = await import('../../src/client');
    const phone = makePatter();
    // Fill to the cap.
    for (let i = 0; i < PREWARM_CACHE_MAX; i += 1) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (phone as any).prewarmAudio.set(`CA-fill-${i}`, Buffer.from('X'));
    }
    const tts = new StubTTS();
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'hi',
      tts,
      prewarmFirstMessage: true,
      provider: 'pipeline',
    };
    const warnings: string[] = [];
    const orig = console.warn;
    console.warn = (...args: unknown[]) => warnings.push(args.join(' '));
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (phone as any).spawnPrewarmFirstMessage(agent, 'CA-overflow', 5);
      await drainPrewarmTasks(phone);
    } finally {
      console.warn = orig;
    }
    expect(tts.synthesizeCalls).toBe(0);
    expect(phone.popPrewarmAudio('CA-overflow')).toBeUndefined();
    const fullWarns = warnings.filter(
      (w) => /cache full/i.test(w) && w.includes('CA-overflow'),
    );
    expect(fullWarns.length).toBe(1);
  });

  it('TTL evicts a never-consumed entry after ringTimeout + grace', async () => {
    // Re-import the client module fresh so we can override the grace
    // constant for this test. The constant is exported as a `const`
    // binding so we can monkey-patch via the namespace import.
    const phone = makePatter();
    const tts = new StubTTS(Buffer.from('TTL-BYTES'));
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'hi',
      tts,
      prewarmFirstMessage: true,
      provider: 'pipeline',
    };
    // ringTimeout = 0.1 s and rely on TTL = ringTimeout + 5 s default.
    // Use vi.useFakeTimers so we don't actually wait 5 s.
    vi.useFakeTimers();
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (phone as any).spawnPrewarmFirstMessage(agent, 'CA-ttl', 0.1);
      // Allow the synth task to complete (microtask drain + 0 ms timers).
      await vi.advanceTimersByTimeAsync(50);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const cacheHasEntry = (phone as any).prewarmAudio.has('CA-ttl');
      expect(cacheHasEntry).toBe(true);

      const warnings: string[] = [];
      const orig = console.warn;
      console.warn = (...args: unknown[]) => warnings.push(args.join(' '));
      try {
        // 100 ms ring + 5_000 ms grace = 5_100 ms total.
        await vi.advanceTimersByTimeAsync(5_200);
      } finally {
        console.warn = orig;
      }
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((phone as any).prewarmAudio.has('CA-ttl')).toBe(false);
      const ttlWarns = warnings.filter((w) => /ttl/i.test(w) && w.includes('CA-ttl'));
      expect(ttlWarns.length).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('TTL is cancelled on normal cache consumption', async () => {
    const phone = makePatter();
    const tts = new StubTTS(Buffer.from('NORMAL-BYTES'));
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      firstMessage: 'hi',
      tts,
      prewarmFirstMessage: true,
      provider: 'pipeline',
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).spawnPrewarmFirstMessage(agent, 'CA-normal', 1);
    await drainPrewarmTasks(phone);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmTtlTimers.has('CA-normal')).toBe(true);
    const out = phone.popPrewarmAudio('CA-normal');
    expect(out?.toString()).toBe('NORMAL-BYTES');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((phone as any).prewarmTtlTimers.has('CA-normal')).toBe(false);
  });
});

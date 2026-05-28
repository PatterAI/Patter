/**
 * Tests for the PatterTool integration adapter.
 *
 * Mirrors `libraries/python/tests/unit/test_patter_tool.py` so the cross-SDK
 * contract stays in lockstep. The full call flow needs a live carrier+webhook,
 * so these tests focus on the deterministic surface: schema shape, option
 * validation, the delegation to `Patter.call({ wait: true })` (using a fake
 * Patter that honours the `CallResult` contract), and the Hermes handler
 * envelope.
 */

import { describe, expect, it, vi } from 'vitest';
import { PatterTool } from '../src/integrations/patter-tool';
import type { CallResult } from '../src/types';

/** Build a realistic CallResult like the SDK's completion registry emits. */
function fakeCallResult(callId: string, outcome: CallResult['outcome'] = 'answered'): CallResult {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const metrics: any = {
    call_id: callId,
    duration_seconds: 12.3,
    turns: [],
    cost: { stt: 0, tts: 0, llm: 0, telephony: 0, total: 0.0123 },
    latency_avg: { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0 },
    latency_p95: { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0 },
    provider_mode: 'pipeline',
    stt_provider: '',
    tts_provider: '',
    llm_provider: '',
    telephony_provider: 'twilio',
  };
  return {
    callId,
    outcome,
    status: 'completed',
    durationSeconds: 12.3,
    transcript: [
      { role: 'agent', text: 'Hello!' },
      { role: 'user', text: 'Hi.' },
    ],
    cost: { stt: 0, tts: 0, llm: 0, telephony: 0, total: 0.0123 },
    metrics,
  };
}

/**
 * In-memory Patter double that honours the `call({ wait: true })` contract.
 *
 * `call({ wait: true })` resolves to a CallResult (what PatterTool now
 * consumes); `wait: false` resolves to void like the real SDK. Set
 * `neverEnd = true` to simulate a call that never reaches a terminal signal so
 * the `execute()` timeout path can be exercised.
 */
class FakePatter {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  serveOpts: any = null;
  callsIssued: Array<{ to: string; agent: unknown; call_id: string; wait: boolean }> = [];
  private counter = 0;
  neverEnd = false;
  // PatterTool never touches this, but a real served Patter has a server.
  private server: object | null = {};

  constructor(private readonly outcome: CallResult['outcome'] = 'answered') {}

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  agent(opts: any): any {
    return { __agent: true, ...opts };
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async serve(opts: any): Promise<void> {
    this.serveOpts = opts;
  }

  async disconnect(): Promise<void> {
    this.server = null;
  }

  async call(opts: {
    to: string;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    agent: any;
    wait?: boolean;
  }): Promise<CallResult | void> {
    // No await between increment and read → each call() gets a distinct id
    // even under Promise.all (single-threaded event loop).
    this.counter += 1;
    const callId = `CA-${this.counter}`;
    this.callsIssued.push({
      to: opts.to,
      agent: opts.agent,
      call_id: callId,
      wait: Boolean(opts.wait),
    });
    if (!opts.wait) return;
    if (this.neverEnd) {
      // Simulate a call that never reaches a terminal state so the execute()
      // backstop timeout fires.
      await new Promise<never>(() => {});
    }
    return fakeCallResult(callId, this.outcome);
  }
}

// --- Schema exporters ---------------------------------------------------

describe('PatterTool — schema exporters', () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const phone = new FakePatter() as any;

  it('openaiSchema returns an OpenAI-style function tool', () => {
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'be polite' } });
    const s = tool.openaiSchema();
    expect(s.type).toBe('function');
    expect(s.function.name).toBe('make_phone_call');
    expect(s.function.parameters.type).toBe('object');
    expect(s.function.parameters.required).toEqual(['to']);
    expect(Object.keys(s.function.parameters.properties)).toEqual([
      'to',
      'goal',
      'first_message',
      'max_duration_sec',
    ]);
  });

  it('anthropicSchema returns the Anthropic input_schema variant', () => {
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });
    const s = tool.anthropicSchema();
    expect(s.name).toBe('make_phone_call');
    expect(s.input_schema.type).toBe('object');
    expect(s.input_schema.required).toEqual(['to']);
  });

  it('hermesSchema returns the same JSON-schema under `parameters`', () => {
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });
    const s = tool.hermesSchema();
    expect(s.name).toBe('make_phone_call');
    expect(s.parameters.required).toEqual(['to']);
  });

  it('honours custom name + description', () => {
    const t = new PatterTool({ phone, agent: { systemPrompt: 'x' }, name: 'dial', description: 'ring it' });
    expect(t.openaiSchema().function.name).toBe('dial');
    expect(t.openaiSchema().function.description).toBe('ring it');
  });
});

// --- execute() ---------------------------------------------------------

describe('PatterTool — execute()', () => {
  it('rejects when `to` is missing or not E.164', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new FakePatter() as any;
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await expect(tool.execute({} as any)).rejects.toThrow(/E\.164/);
    await expect(tool.execute({ to: '5551234567' })).rejects.toThrow(/E\.164/);
  });

  it('dials and returns the structured result envelope', async () => {
    const phone = new FakePatter();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tool = new PatterTool({ phone: phone as any, agent: { systemPrompt: 'x' } });
    const result = await tool.execute({ to: '+15551234567', goal: 'book dentist' });

    expect(phone.callsIssued).toHaveLength(1);
    expect(phone.callsIssued[0].to).toBe('+15551234567');
    expect(result.call_id).toBe('CA-1');
    expect(result.status).toBe('completed');
    expect(result.duration_seconds).toBe(12.3);
    expect(result.cost_usd).toBe(0.0123);
    expect(result.transcript).toHaveLength(2);
  });

  it('hermesHandler returns a JSON string on success', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new FakePatter() as any;
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });
    const handler = tool.hermesHandler();
    const out = await handler({ to: '+15551234567' });
    expect(typeof out).toBe('string');
    const parsed = JSON.parse(out);
    expect(parsed.call_id).toBe('CA-1');
    expect(parsed.status).toBe('completed');
    expect(parsed.error).toBeUndefined();
  });

  it('hermesHandler returns {error} on failure (matches Hermes contract)', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new FakePatter() as any;
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });
    const handler = tool.hermesHandler();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const out = await handler({ to: 'not-e164' } as any);
    const parsed = JSON.parse(out);
    expect(parsed.error).toMatch(/E\.164/);
    expect(parsed.call_id).toBeUndefined();
  });

  it('times out when the call never reaches a terminal state', async () => {
    const phone = new FakePatter();
    phone.neverEnd = true;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tool = new PatterTool({ phone: phone as any, agent: { systemPrompt: 'x' }, maxDurationSec: 5 });
    // Start with real timers so serve()'s async resolves cleanly, then switch
    // to fake timers to drive the execute() backstop deterministically.
    await tool.start();

    vi.useFakeTimers();
    // `execute()` clamps the timeout to a 5 s floor (same as Python's
    // `max(5, ...)`), so advance just past that to fire the backstop.
    const promise = tool.execute({ to: '+15551234567', max_duration_sec: 1 });
    // Attach the rejection handler synchronously, BEFORE advancing the fake
    // timers — otherwise vitest flags the rejection as "unhandled".
    const captured = promise.catch((err) => err);
    await vi.advanceTimersByTimeAsync(6000);
    const err = await captured;
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toMatch(/timeout/);
    vi.useRealTimers();
  });
});

// --- start/stop --------------------------------------------------------

describe('PatterTool — start/stop', () => {
  it('start is idempotent', async () => {
    const phone = new FakePatter();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tool = new PatterTool({ phone: phone as any, agent: { systemPrompt: 'x' } });
    await tool.start();
    await tool.start(); // no-op
    const result = await tool.execute({ to: '+15551234567' });
    expect(result.call_id).toBe('CA-1');
  });

  it('start without agent throws a helpful error', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new FakePatter() as any;
    const tool = new PatterTool({ phone });
    await expect(tool.start()).rejects.toThrow(/agent/);
  });
});

// --- Hermes registry helper -------------------------------------------

describe('PatterTool — register helper', () => {
  it('hermesHandler is callable and returns a function', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new FakePatter() as any;
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });
    expect(typeof tool.hermesHandler()).toBe('function');
  });
});

// --- delegation to call({ wait: true }) -------------------------------

describe('PatterTool — delegation to call({ wait: true })', () => {
  it('execute() delegates to call({ wait: true })', async () => {
    // The whole point of the refactor: the SDK owns dial→completion
    // correlation now, so execute() must request wait:true.
    const phone = new FakePatter();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tool = new PatterTool({ phone: phone as any, agent: { systemPrompt: 'x' } });
    await tool.execute({ to: '+15551234567' });
    expect(phone.callsIssued[0].wait).toBe(true);
  });

  it('surfaces the CallResult outcome (voicemail vs answered)', async () => {
    const phone = new FakePatter('voicemail');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tool = new PatterTool({ phone: phone as any, agent: { systemPrompt: 'x' } });
    const result = await tool.execute({ to: '+15551234567' });
    expect(result.outcome).toBe('voicemail');
  });

  it('concurrent execute() calls each get their own CallResult', async () => {
    // Each dial is correlated to its own completion by the SDK — no shared
    // mutable slot, so two parallel execute() calls get distinct results.
    const phone = new FakePatter();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tool = new PatterTool({ phone: phone as any, agent: { systemPrompt: 'x' } });
    const [a, b] = await Promise.all([
      tool.execute({ to: '+15551111111' }),
      tool.execute({ to: '+15552222222' }),
    ]);
    expect(a.call_id).not.toBe(b.call_id);
    expect(new Set([a.call_id, b.call_id])).toEqual(new Set(['CA-1', 'CA-2']));
  });
});

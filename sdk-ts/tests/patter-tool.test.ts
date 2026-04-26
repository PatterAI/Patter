/**
 * Tests for the PatterTool integration adapter.
 *
 * The full call flow needs a live Patter+carrier+webhook setup, so these
 * tests focus on the deterministic surface: schema shape, option validation,
 * and the call_id dispatcher / promise lifecycle (using a fake Patter).
 */

import { describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PatterTool } from '../src/integrations/patter-tool';

class FakePatter {
  private readonly metrics = new MetricsStub();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private serveOpts: any = null;
  callsIssued: Array<{ to: string }> = [];

  get metricsStore(): MetricsStub {
    return this.metrics;
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  agent(opts: any): any {
    return { __agent: true, ...opts };
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async serve(opts: any): Promise<void> {
    this.serveOpts = opts;
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async call(opts: any): Promise<void> {
    this.callsIssued.push({ to: opts.to });
    // Simulate the recordCallInitiated → SSE event Patter normally fires.
    const callId = `CA-${this.callsIssued.length}`;
    this.metrics.emit('sse', {
      type: 'call_initiated',
      data: { call_id: callId, callee: opts.to },
    });
    // Defer the call_end to a real macrotask so execute() has time to
    // register its pending waiter (microtask would race with the await chain).
    setTimeout(() => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const onEnd = this.serveOpts?.onCallEnd as (d: any) => Promise<void>;
      if (onEnd) {
        void onEnd({
          call_id: callId,
          status: 'completed',
          transcript: [
            { role: 'agent', text: 'Hello!' },
            { role: 'user', text: 'Hi.' },
          ],
          metrics: { duration_seconds: 12.3, cost: { total: 0.0123 } },
        });
      }
    }, 0);
  }
}

class MetricsStub extends EventEmitter {}

describe('PatterTool — schema exporters', () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const phone = new FakePatter() as any;
  const tool = new PatterTool({ phone, agent: { systemPrompt: 'be polite' } });

  it('openaiSchema returns an OpenAI-style function tool', () => {
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
    const s = tool.anthropicSchema();
    expect(s.name).toBe('make_phone_call');
    expect(s.input_schema.type).toBe('object');
    expect(s.input_schema.required).toEqual(['to']);
  });

  it('hermesSchema returns the same JSON-schema under `parameters`', () => {
    const s = tool.hermesSchema();
    expect(s.name).toBe('make_phone_call');
    expect(s.parameters.required).toEqual(['to']);
  });

  it('honours custom name + description', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const t = new PatterTool({ phone, agent: { systemPrompt: 'x' }, name: 'dial', description: 'pick up the phone' });
    expect(t.openaiSchema().function.name).toBe('dial');
    expect(t.openaiSchema().function.description).toBe('pick up the phone');
  });
});

describe('PatterTool — execute()', () => {
  it('rejects when `to` is missing or not E.164', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new FakePatter() as any;
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await expect(tool.execute({} as any)).rejects.toThrow(/E\.164/);
    await expect(tool.execute({ to: '5551234567' })).rejects.toThrow(/E\.164/);
  });

  it('dials, awaits onCallEnd, and resolves with structured result', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new FakePatter() as any;
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });

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

  it('times out when onCallEnd never arrives', async () => {
    class SilentPatter extends FakePatter {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      async call(opts: any): Promise<void> {
        this.callsIssued.push({ to: opts.to });
        // Emit call_initiated so the dispatcher resolves, but never call_end.
        const callId = `CA-${this.callsIssued.length}`;
        this.metricsStore.emit('sse', {
          type: 'call_initiated',
          data: { call_id: callId, callee: opts.to },
        });
      }
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new SilentPatter() as any;
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' }, maxDurationSec: 5 });

    vi.useFakeTimers();
    const promise = tool.execute({ to: '+15551234567', max_duration_sec: 5 });
    // Advance past the timeout
    await vi.advanceTimersByTimeAsync(6000);
    await expect(promise).rejects.toThrow(/timeout/);
    vi.useRealTimers();
  });
});

describe('PatterTool — start/stop', () => {
  it('start is idempotent', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const phone = new FakePatter() as any;
    const tool = new PatterTool({ phone, agent: { systemPrompt: 'x' } });
    await tool.start();
    await tool.start(); // should be a no-op
    // dial should still work after double-start
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

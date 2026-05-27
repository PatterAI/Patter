/**
 * Regression for upstream issue #110 (Python PR #115) — TypeScript parity.
 *
 * Pipeline mode previously passed only the user-provided tools to `LLMLoop`
 * — the built-in `transfer_call` / `end_call` tools that the Realtime path
 * injects were missing, so pipeline LLMs could never initiate a handoff or
 * hangup regardless of the system prompt.
 *
 * These tests exercise the `augmentWithBuiltinHandoffTools` helper that
 * bolts the built-ins onto the tool list with handler closures wired to the
 * telephony-level transfer / hangup callbacks.
 */
import { describe, it, expect } from 'vitest';
import { augmentWithBuiltinHandoffTools } from '../src/stream-handler';
import type { ToolDefinition } from '../src/types';

describe('augmentWithBuiltinHandoffTools', () => {
  it('appends transfer_call and end_call when both callbacks present', () => {
    const tools = augmentWithBuiltinHandoffTools(null, {
      transferCall: async () => {},
      endCall: async () => {},
    });
    expect(tools.map((t) => t.name)).toEqual(['transfer_call', 'end_call']);
    expect(typeof tools[0].handler).toBe('function');
    expect(typeof tools[1].handler).toBe('function');
  });

  it('preserves user tools order with built-ins appended', () => {
    const userTools: ToolDefinition[] = [
      { name: 'lookup_customer', description: '', parameters: { type: 'object' } },
      { name: 'send_sms', description: '', parameters: { type: 'object' } },
    ];
    const tools = augmentWithBuiltinHandoffTools(userTools, {
      transferCall: async () => {},
      endCall: async () => {},
    });
    expect(tools.map((t) => t.name)).toEqual([
      'lookup_customer',
      'send_sms',
      'transfer_call',
      'end_call',
    ]);
  });

  it('skips built-ins when callbacks are missing', () => {
    const userTools: ToolDefinition[] = [
      { name: 'lookup_customer', description: '', parameters: {} },
    ];
    const tools = augmentWithBuiltinHandoffTools(userTools, {});
    expect(tools.map((t) => t.name)).toEqual(['lookup_customer']);
  });

  it('skips only the built-in whose callback is missing', () => {
    const tools = augmentWithBuiltinHandoffTools(null, {
      endCall: async () => {},
    });
    expect(tools.map((t) => t.name)).toEqual(['end_call']);
  });

  it('transfer handler dispatches the number to transferCall', async () => {
    const captured: string[] = [];
    const tools = augmentWithBuiltinHandoffTools(null, {
      transferCall: async (n) => {
        captured.push(n);
      },
    });
    const handler = tools[0].handler;
    if (typeof handler !== 'function') throw new Error('handler missing');
    const result = await (handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
      { number: '+14155551234' },
      { call_id: 'CAtest' },
    );
    expect(captured).toEqual(['+14155551234']);
    expect(result).toContain('transferring');
    expect(result).toContain('+14155551234');
  });

  it('transfer handler rejects invalid E.164 without dispatching', async () => {
    const captured: string[] = [];
    const tools = augmentWithBuiltinHandoffTools(null, {
      transferCall: async (n) => {
        captured.push(n);
      },
    });
    const handler = tools[0].handler;
    if (typeof handler !== 'function') throw new Error('handler missing');
    const result = await (handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
      { number: 'not-a-number' },
      { call_id: 'CAtest' },
    );
    expect(captured).toEqual([]);
    expect(result).toContain('rejected');
  });

  it('end_call handler dispatches with default reason', async () => {
    const reasons: string[] = [];
    const tools = augmentWithBuiltinHandoffTools(null, {
      endCall: async (r) => {
        reasons.push(r);
      },
    });
    const handler = tools[0].handler;
    if (typeof handler !== 'function') throw new Error('handler missing');
    const result = await (handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
      {},
      { call_id: 'CAtest' },
    );
    expect(reasons).toEqual(['conversation_complete']);
    expect(result).toContain('ending');
  });

  it('end_call handler passes through user-supplied reason', async () => {
    const reasons: string[] = [];
    const tools = augmentWithBuiltinHandoffTools(null, {
      endCall: async (r) => {
        reasons.push(r);
      },
    });
    const handler = tools[0].handler;
    if (typeof handler !== 'function') throw new Error('handler missing');
    await (handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
      { reason: 'user_requested' },
      { call_id: 'CAtest' },
    );
    expect(reasons).toEqual(['user_requested']);
  });
});

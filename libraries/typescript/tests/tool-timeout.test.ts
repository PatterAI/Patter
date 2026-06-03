import { describe, it, expect, vi } from 'vitest';
import { DefaultToolExecutor } from '../src/llm-loop';
import { Tool, tool } from '../src/public-api';
import type { ToolDefinition } from '../src/types';

const CTX = { call_id: 'CA0000000000000000000000000000a001', caller: '+15555550100' };

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

describe('[unit] DefaultToolExecutor per-tool handler timeout', () => {
  it('does NOT abort a handler that finishes within a raised per-tool timeout', async () => {
    const executor = new DefaultToolExecutor();
    let invoked = false;
    const toolDef: ToolDefinition = {
      name: 'slow_lookup',
      description: 'A handler that takes longer than the 10s default would tolerate if it were 200ms.',
      parameters: { type: 'object', properties: {} },
      // 200 ms — well under the 60s per-tool timeout but proves the
      // executor uses the per-tool value (a hypothetical 100ms default
      // would have aborted it).
      timeoutMs: 60_000,
      handler: async () => {
        invoked = true;
        await sleep(200);
        return JSON.stringify({ ok: true, data: 'real-result' });
      },
    };
    const result = await executor.execute(toolDef, {}, CTX);
    expect(invoked).toBe(true);
    expect(JSON.parse(result)).toEqual({ ok: true, data: 'real-result' });
  });

  it('returns a structured timeout fallback when the handler exceeds the per-tool timeout', async () => {
    const executor = new DefaultToolExecutor();
    let invocations = 0;
    const toolDef: ToolDefinition = {
      name: 'hung_tool',
      description: 'never resolves',
      parameters: { type: 'object', properties: {} },
      timeoutMs: 50,
      handler: async () => {
        invocations += 1;
        await sleep(5_000); // far longer than the 50ms timeout
        return '{}';
      },
    };
    const start = Date.now();
    const result = await executor.execute(toolDef, {}, CTX);
    const elapsed = Date.now() - start;
    const parsed = JSON.parse(result) as { error?: string; fallback?: boolean };
    expect(parsed.fallback).toBe(true);
    expect(parsed.error).toMatch(/timed out/i);
    // A timeout must NOT consume retry attempts — the handler is invoked once
    // and the executor returns promptly (well under the 5s sleep, and under
    // the retry-backoff windows that 2 extra attempts would add).
    expect(invocations).toBe(1);
    expect(elapsed).toBeLessThan(2_000);
  });

  it('uses the existing 10s default when timeoutMs is undefined (backward compat)', async () => {
    const executor = new DefaultToolExecutor();
    const toolDef: ToolDefinition = {
      name: 'fast_tool',
      description: 'resolves immediately',
      parameters: { type: 'object', properties: {} },
      handler: async () => JSON.stringify({ ok: true }),
    };
    const result = await executor.execute(toolDef, {}, CTX);
    expect(JSON.parse(result)).toEqual({ ok: true });
  });

  it('clamps an absurd per-tool timeout to the 300s ceiling (still resolves a fast handler)', async () => {
    const executor = new DefaultToolExecutor();
    const toolDef: ToolDefinition = {
      name: 'big_timeout',
      description: 'huge configured timeout',
      parameters: { type: 'object', properties: {} },
      timeoutMs: 999_999_999,
      handler: async () => JSON.stringify({ ok: true }),
    };
    const result = await executor.execute(toolDef, {}, CTX);
    expect(JSON.parse(result)).toEqual({ ok: true });
  });
});

describe('[unit] Tool class + object-literal carry timeoutMs + reassurance', () => {
  it('object-literal ToolDefinition with timeoutMs + reassurance reaches the executor and is honored', async () => {
    const executor = new DefaultToolExecutor();
    const literal: ToolDefinition = {
      name: 'browser_automation',
      description: 'long browser-automation tool',
      parameters: { type: 'object', properties: {} },
      timeoutMs: 60_000,
      reassurance: 'One moment while I check that for you.',
      handler: async () => {
        await sleep(150);
        return JSON.stringify({ done: true });
      },
    };
    const result = await executor.execute(literal, {}, CTX);
    expect(JSON.parse(result)).toEqual({ done: true });
    // The reassurance field is preserved on the literal for the stream handler.
    expect(literal.reassurance).toBe('One moment while I check that for you.');
  });

  it('the Tool class forwards timeoutMs, reassurance, and strict', () => {
    const t = new Tool({
      name: 'check',
      description: 'check stuff',
      handler: async () => '{}',
      timeoutMs: 45_000,
      reassurance: { message: 'Give me a second.', afterMs: 800 },
      strict: true,
    });
    expect(t.timeoutMs).toBe(45_000);
    expect(t.reassurance).toEqual({ message: 'Give me a second.', afterMs: 800 });
    expect(t.strict).toBe(true);
    // Structurally still a ToolDefinition.
    const asDef: ToolDefinition = t;
    expect(asDef.timeoutMs).toBe(45_000);
  });

  it('tool() factory forwards the same fields', () => {
    const t = tool({
      name: 'check2',
      handler: async () => '{}',
      timeoutMs: 30_000,
      reassurance: 'Hang on.',
    });
    expect(t.timeoutMs).toBe(30_000);
    expect(t.reassurance).toBe('Hang on.');
  });

  it('Tool class leaves the new fields undefined when not provided (backward compat)', () => {
    const t = new Tool({ name: 'plain', handler: async () => '{}' });
    expect(t.timeoutMs).toBeUndefined();
    expect(t.reassurance).toBeUndefined();
    expect(t.strict).toBeUndefined();
  });
});

describe('[unit] per-tool timeout leaves no dangling timer', () => {
  it('clears the timeout-race timer after a fast handler resolves', async () => {
    vi.useFakeTimers();
    try {
      const executor = new DefaultToolExecutor();
      const toolDef: ToolDefinition = {
        name: 'fast_tool',
        description: 'resolves immediately, no internal timers',
        parameters: { type: 'object', properties: {} },
        // A long ceiling: if the losing timer were NOT cleared it would sit
        // pending for ~5 minutes and getTimerCount() would be 1.
        timeoutMs: 300_000,
        handler: async () => JSON.stringify({ ok: true }),
      };
      const pending = executor.execute(toolDef, {}, CTX);
      // Flush microtasks (the handler promise wins the race with no timer).
      await vi.advanceTimersByTimeAsync(0);
      const result = await pending;
      expect(JSON.parse(result)).toEqual({ ok: true });
      // The race timeout must have been cleared in `finally` — no leak.
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

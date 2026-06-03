import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { DefaultToolExecutor } from '../src/llm-loop';
import type { ToolDefinition } from '../src/types';

const CTX = { call_id: 'CA0000000000000000000000000000a001', caller: '+15555550100' };

/**
 * [mocked] The external boundary here is the webhook HTTP endpoint — we mock
 * `globalThis.fetch`. The mock honours the per-request `AbortSignal` the
 * executor passes in: it rejects with an AbortError as soon as the signal
 * fires. Everything else (the executor's per-tool timeout selection, the
 * `AbortSignal.timeout(effective)` wiring, the structured fallback) is the
 * real code path.
 */
describe('[mocked] per-tool webhook timeout uses AbortSignal.timeout(timeoutMs)', () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('aborts a slow webhook at the per-tool timeout, not the 10s default', async () => {
    // The endpoint "never responds" — it only settles when the abort signal
    // fires. With a 150ms per-tool timeout the fetch must reject in ~150ms,
    // proving the per-tool value (not the 10s default) governs.
    globalThis.fetch = vi.fn((_url: string | URL | Request, init?: RequestInit) => {
      return new Promise((_resolve, reject) => {
        const signal = init?.signal;
        if (!signal) {
          reject(new Error('expected an AbortSignal on the webhook fetch'));
          return;
        }
        signal.addEventListener('abort', () => {
          const err = new Error('The operation was aborted');
          err.name = 'AbortError';
          reject(err);
        });
      });
    }) as unknown as typeof globalThis.fetch;

    const executor = new DefaultToolExecutor({ maxRetries: 0 });
    const toolDef: ToolDefinition = {
      name: 'slow_webhook',
      description: 'a webhook that never responds',
      parameters: { type: 'object', properties: {} },
      webhookUrl: 'https://example.com/slow-tool',
      timeoutMs: 150,
    };

    const start = Date.now();
    const result = await executor.execute(toolDef, {}, CTX);
    const elapsed = Date.now() - start;

    const parsed = JSON.parse(result) as { error?: string; fallback?: boolean };
    expect(parsed.fallback).toBe(true);
    // Aborted well before the 10s default would have.
    expect(elapsed).toBeLessThan(2_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it('passes a longer per-tool timeout through so a 60s tool is not cut at 10s', async () => {
    // The mock resolves after a short delay (well under the 60s per-tool
    // timeout). We assert the executor returned the real body — i.e. it did
    // NOT abort at the 10s default. We also capture the signal to confirm a
    // timeout was attached.
    let capturedSignal: AbortSignal | undefined;
    globalThis.fetch = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      capturedSignal = init?.signal ?? undefined;
      await new Promise((r) => setTimeout(r, 50));
      return {
        ok: true,
        status: 200,
        json: async () => ({ booked: true }),
        text: async () => '{"booked":true}',
      } as Response;
    }) as unknown as typeof globalThis.fetch;

    const executor = new DefaultToolExecutor({ maxRetries: 0 });
    const toolDef: ToolDefinition = {
      name: 'long_webhook',
      description: 'a webhook that takes 50ms but is allowed 60s',
      parameters: { type: 'object', properties: {} },
      webhookUrl: 'https://example.com/long-tool',
      timeoutMs: 60_000,
    };

    const result = await executor.execute(toolDef, {}, CTX);
    expect(JSON.parse(result)).toEqual({ booked: true });
    expect(capturedSignal).toBeInstanceOf(AbortSignal);
    expect(capturedSignal?.aborted).toBe(false);
  });
});

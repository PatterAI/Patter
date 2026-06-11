/**
 * Regression tests for the Cerebras provider default model + 404 handling.
 *
 * Why: the previous default ``llama-3.3-70b`` returned a silent 404 on
 * Cerebras free tier (model gated to paid plans). The fix lowers the default
 * to ``llama3.1-8b`` (free-tier available, sub-100ms TTFT) and translates
 * 404 model_not_found into a recovery hint that names override candidates.
 */

import { describe, expect, it, vi, afterEach } from 'vitest';
import { CerebrasLLMProvider } from '../src/providers/cerebras-llm';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe('CerebrasLLMProvider default model', () => {
  it('uses gpt-oss-120b by default (highest throughput on WSE-3)', () => {
    const provider = new CerebrasLLMProvider({ apiKey: 'csk-test' });
    expect(provider.model).toBe('gpt-oss-120b');
  });

  it('honours explicit model override', () => {
    const provider = new CerebrasLLMProvider({
      apiKey: 'csk-test',
      model: 'llama3.1-8b',
    });
    expect(provider.model).toBe('llama3.1-8b');
  });
});

describe('CerebrasLLMProvider 404 model_not_found handling', () => {
  it('logs a recovery hint with override candidates on 404 model_not_found', async () => {
    const provider = new CerebrasLLMProvider({
      apiKey: 'csk-test',
      model: 'gated-model',
    });

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          message: 'Model gated-model does not exist or you do not have access to it.',
          type: 'not_found_error',
          param: 'model',
          code: 'model_not_found',
        }),
        { status: 404 },
      ),
    ) as unknown as typeof fetch;

    // Capture stderr/console output from the SDK logger.
    const logs: string[] = [];
    const errSpy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      logs.push(args.map((a) => String(a)).join(' '));
    });

    // The provider now THROWS on a non-recoverable error so the LLM fallback
    // chain can fail over and the spoken error fallback can fire (a silent
    // return looked like a successful, empty turn). Draining must reject; the
    // recovery-hint log still fires as a side effect before the throw.
    await expect(
      (async () => {
        for await (const _ of provider.stream([{ role: 'user', content: 'hi' }])) {
          // no-op
        }
      })(),
    ).rejects.toThrow();

    const combined = logs.join('\n');
    expect(combined).toContain('gated-model');
    expect(combined).toContain('not available on your tier');
    expect(combined).toContain('llama3.1-8b'); // override hint
    expect(combined).toContain('/v1/models'); // discovery hint
    errSpy.mockRestore();
  });

  it('uses generic error log for non-model 404s', async () => {
    const provider = new CerebrasLLMProvider({ apiKey: 'csk-test' });
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('Not Found', { status: 404 }),
    ) as unknown as typeof fetch;

    const logs: string[] = [];
    const errSpy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      logs.push(args.map((a) => String(a)).join(' '));
    });

    await expect(
      (async () => {
        for await (const _ of provider.stream([{ role: 'user', content: 'hi' }])) {
          // no-op
        }
      })(),
    ).rejects.toThrow();

    const combined = logs.join('\n');
    expect(combined).toContain('Cerebras API error: 404');
    expect(combined).not.toContain('not available on your tier');
    errSpy.mockRestore();
  });
});

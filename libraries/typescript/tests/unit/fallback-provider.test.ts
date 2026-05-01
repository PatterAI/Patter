import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  FallbackLLMProvider,
  AllProvidersFailedError,
  PartialStreamError,
} from '../../src/fallback-provider';
import type { LLMProvider, LLMChunk } from '../../src/llm-loop';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Create a provider that yields the given chunks successfully. */
function succeedingProvider(chunks: LLMChunk[]): LLMProvider {
  return {
    async *stream() {
      for (const chunk of chunks) {
        yield chunk;
      }
    },
  };
}

/** Create a provider that always throws on stream. */
function failingProvider(error?: Error): LLMProvider {
  return {
    async *stream() {
      throw error ?? new Error('provider failed');
    },
  };
}

/** Create a provider that yields some chunks then throws. */
function partialThenFailProvider(
  chunks: LLMChunk[],
  error?: Error,
): LLMProvider {
  return {
    async *stream() {
      for (const chunk of chunks) {
        yield chunk;
      }
      throw error ?? new Error('mid-stream failure');
    },
  };
}

/** Create a provider that fails N times, then succeeds. */
function failNTimesThenSucceed(
  failCount: number,
  chunks: LLMChunk[],
): LLMProvider {
  let calls = 0;
  return {
    async *stream() {
      calls++;
      if (calls <= failCount) {
        throw new Error(`fail #${calls}`);
      }
      for (const chunk of chunks) {
        yield chunk;
      }
    },
  };
}

/** Collect all chunks from a fallback provider stream. */
async function collectChunks(
  provider: FallbackLLMProvider,
  messages: Array<Record<string, unknown>> = [{ role: 'user', content: 'hi' }],
): Promise<LLMChunk[]> {
  const chunks: LLMChunk[] = [];
  for await (const chunk of provider.stream(messages)) {
    chunks.push(chunk);
  }
  return chunks;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('FallbackLLMProvider', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // 1. Primary provider succeeds -> returns its output
  it('returns output from the primary provider when it succeeds', async () => {
    const primary = succeedingProvider([
      { type: 'text', content: 'Hello' },
      { type: 'text', content: ' world' },
    ]);
    const fallback = succeedingProvider([
      { type: 'text', content: 'Fallback' },
    ]);

    const provider = new FallbackLLMProvider([primary, fallback]);
    const chunks = await collectChunks(provider);

    expect(chunks).toEqual([
      { type: 'text', content: 'Hello' },
      { type: 'text', content: ' world' },
    ]);

    provider.destroy();
  });

  // 2. Primary fails -> fallback succeeds -> returns fallback output
  it('falls back to the next provider when the primary fails', async () => {
    const primary = failingProvider();
    const fallback = succeedingProvider([
      { type: 'text', content: 'Fallback response' },
    ]);

    const provider = new FallbackLLMProvider([primary, fallback]);
    const chunks = await collectChunks(provider);

    expect(chunks).toEqual([
      { type: 'text', content: 'Fallback response' },
    ]);

    provider.destroy();
  });

  // 3. Both fail -> throws error
  it('throws AllProvidersFailedError when all providers fail', async () => {
    const primary = failingProvider(new Error('primary down'));
    const fallback = failingProvider(new Error('fallback down'));

    const provider = new FallbackLLMProvider([primary, fallback]);

    await expect(collectChunks(provider)).rejects.toThrow(
      AllProvidersFailedError,
    );

    provider.destroy();
  });

  // 4. Primary fails after yielding tokens -> throws (no retry)
  it('throws PartialStreamError when provider fails after yielding tokens', async () => {
    const primary = partialThenFailProvider([
      { type: 'text', content: 'partial' },
    ]);
    const fallback = succeedingProvider([
      { type: 'text', content: 'Fallback' },
    ]);

    const provider = new FallbackLLMProvider([primary, fallback]);

    await expect(collectChunks(provider)).rejects.toThrow(PartialStreamError);

    provider.destroy();
  });

  // 5. Primary recovers after being marked unavailable
  it('recovers a provider via background probing', async () => {
    const recoveringProvider = failNTimesThenSucceed(1, [
      { type: 'text', content: 'recovered' },
    ]);
    const fallback = succeedingProvider([
      { type: 'text', content: 'Fallback' },
    ]);

    const provider = new FallbackLLMProvider(
      [recoveringProvider, fallback],
      { recoveryIntervalMs: 100 },
    );

    // First call: primary fails, fallback succeeds
    const firstChunks = await collectChunks(provider);
    expect(firstChunks).toEqual([
      { type: 'text', content: 'Fallback' },
    ]);

    // Provider 0 should be unavailable
    expect(provider.getAvailability()).toEqual([false, true]);

    // Advance time to trigger recovery probe
    await vi.advanceTimersByTimeAsync(150);

    // Provider 0 should now be available
    expect(provider.getAvailability()).toEqual([true, true]);

    provider.destroy();
  });

  // 6. maxRetryPerProvider = 2 -> retries twice before fallback
  it('retries each provider up to maxRetryPerProvider times', async () => {
    let primaryCalls = 0;
    const primary: LLMProvider = {
      async *stream() {
        primaryCalls++;
        throw new Error('primary fails');
      },
    };

    const fallback = succeedingProvider([
      { type: 'text', content: 'Fallback' },
    ]);

    const provider = new FallbackLLMProvider([primary, fallback], {
      maxRetryPerProvider: 2,
    });

    const chunks = await collectChunks(provider);

    expect(primaryCalls).toBe(2);
    expect(chunks).toEqual([{ type: 'text', content: 'Fallback' }]);

    provider.destroy();
  });

  // 7. All providers unavailable -> retries all
  it('retries all providers when all are marked unavailable', async () => {
    let primaryCalls = 0;
    let fallbackCalls = 0;

    const primary: LLMProvider = {
      async *stream() {
        primaryCalls++;
        if (primaryCalls <= 1) {
          throw new Error('primary fails');
        }
        yield { type: 'text' as const, content: 'Primary recovered' };
      },
    };

    const fallback: LLMProvider = {
      async *stream() {
        fallbackCalls++;
        throw new Error('fallback fails');
      },
    };

    const provider = new FallbackLLMProvider([primary, fallback]);
    const chunks = await collectChunks(provider);

    // Primary failed once (first pass), fallback failed once (first pass),
    // then retry-all: primary succeeds on second overall call
    expect(primaryCalls).toBe(2);
    expect(chunks).toEqual([
      { type: 'text', content: 'Primary recovered' },
    ]);

    provider.destroy();
  });

  // 8. Provider returns empty -> succeeds (not a failure)
  it('treats an empty stream as a success', async () => {
    const emptyProvider = succeedingProvider([]);
    const fallback = succeedingProvider([
      { type: 'text', content: 'Should not reach' },
    ]);

    const provider = new FallbackLLMProvider([emptyProvider, fallback]);
    const chunks = await collectChunks(provider);

    expect(chunks).toEqual([]);

    provider.destroy();
  });

  // --- Additional edge cases ---

  it('throws if constructed with zero providers', () => {
    expect(() => new FallbackLLMProvider([])).toThrow(
      'FallbackLLMProvider requires at least one provider',
    );
  });

  it('passes messages and tools through to the underlying provider', async () => {
    const messages = [{ role: 'user', content: 'test' }];
    const tools = [{ type: 'function', function: { name: 'foo' } }];
    let receivedMessages: unknown;
    let receivedTools: unknown;

    const spy: LLMProvider = {
      async *stream(msgs, tls) {
        receivedMessages = msgs;
        receivedTools = tls;
        yield { type: 'text', content: 'ok' };
      },
    };

    const provider = new FallbackLLMProvider([spy]);
    const chunks: LLMChunk[] = [];
    for await (const chunk of provider.stream(messages, tools)) {
      chunks.push(chunk);
    }

    expect(receivedMessages).toEqual(messages);
    expect(receivedTools).toEqual(tools);

    provider.destroy();
  });

  it('destroy() clears all recovery timers', async () => {
    const primary = failingProvider();
    const fallback = failingProvider();

    const provider = new FallbackLLMProvider([primary, fallback], {
      recoveryIntervalMs: 100,
    });

    // Both fail -> triggers recovery timers for both
    await expect(collectChunks(provider)).rejects.toThrow(
      AllProvidersFailedError,
    );

    provider.destroy();

    // Advancing timers should not cause issues after destroy
    await vi.advanceTimersByTimeAsync(500);
  });

  it('yields tool_call chunks from the successful provider', async () => {
    const primary = succeedingProvider([
      { type: 'tool_call', index: 0, id: 'tc_1', name: 'greet', arguments: '{}' },
      { type: 'text', content: 'done' },
    ]);

    const provider = new FallbackLLMProvider([primary]);
    const chunks = await collectChunks(provider);

    expect(chunks).toEqual([
      { type: 'tool_call', index: 0, id: 'tc_1', name: 'greet', arguments: '{}' },
      { type: 'text', content: 'done' },
    ]);

    provider.destroy();
  });
});

/**
 * Fallback LLM provider that tries multiple providers in sequence.
 *
 * If the primary provider fails, the next provider is tried, and so on.
 * Each provider gets a configurable number of retries before being skipped.
 * Failed providers are marked unavailable and periodically re-checked in the
 * background.
 */

import type { LLMProvider, LLMChunk } from './llm-loop';
import { getLogger } from './logger';

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

export interface FallbackLLMProviderOptions {
  /** Number of retry attempts per provider before moving to the next (default 1). */
  readonly maxRetryPerProvider?: number;
  /** Interval in ms between background recovery probes (default 30_000). */
  readonly recoveryIntervalMs?: number;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** Thrown when all providers have been exhausted. */
export class AllProvidersFailedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AllProvidersFailedError';
  }
}

/** Thrown when a provider fails after already yielding partial output. */
export class PartialStreamError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PartialStreamError';
  }
}

// ---------------------------------------------------------------------------
// FallbackLLMProvider
// ---------------------------------------------------------------------------

export class FallbackLLMProvider implements LLMProvider {
  private readonly providers: ReadonlyArray<LLMProvider>;
  private readonly availability: boolean[];
  private readonly maxRetryPerProvider: number;
  private readonly recoveryIntervalMs: number;
  private readonly recoveryTimers: Array<ReturnType<typeof setInterval> | null>;

  constructor(
    providers: ReadonlyArray<LLMProvider>,
    options?: FallbackLLMProviderOptions,
  ) {
    if (providers.length === 0) {
      throw new Error('FallbackLLMProvider requires at least one provider');
    }

    this.providers = providers;
    this.availability = providers.map(() => true);
    this.maxRetryPerProvider = options?.maxRetryPerProvider ?? 1;
    this.recoveryIntervalMs = options?.recoveryIntervalMs ?? 30_000;
    this.recoveryTimers = providers.map(() => null);
  }

  // -----------------------------------------------------------------------
  // Public helpers
  // -----------------------------------------------------------------------

  /** Returns a snapshot of per-provider availability. */
  getAvailability(): ReadonlyArray<boolean> {
    return [...this.availability];
  }

  /** Clears all background recovery timers. Call this when shutting down. */
  destroy(): void {
    for (let i = 0; i < this.recoveryTimers.length; i++) {
      if (this.recoveryTimers[i] !== null) {
        clearInterval(this.recoveryTimers[i]!);
        this.recoveryTimers[i] = null;
      }
    }
  }

  // -----------------------------------------------------------------------
  // LLMProvider implementation
  // -----------------------------------------------------------------------

  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    const errors: Error[] = [];

    // First pass: try available providers
    const result = yield* this.tryProviders(
      messages,
      tools,
      /* availableOnly */ true,
      errors,
    );
    if (result === 'done') return;

    // All-failed fallback: retry every provider once more
    getLogger().warn(
      'FallbackLLMProvider: all providers unavailable, retrying all once',
    );
    const retryResult = yield* this.tryProviders(
      messages,
      tools,
      /* availableOnly */ false,
      errors,
    );
    if (retryResult === 'done') return;

    throw new AllProvidersFailedError(
      `All ${this.providers.length} LLM providers failed. Last error: ${errors.at(-1)?.message ?? 'unknown'}`,
    );
  }

  // -----------------------------------------------------------------------
  // Internals
  // -----------------------------------------------------------------------

  private async *tryProviders(
    messages: Array<Record<string, unknown>>,
    tools: Array<Record<string, unknown>> | null | undefined,
    availableOnly: boolean,
    errors: Error[],
  ): AsyncGenerator<LLMChunk, 'done' | 'exhausted', unknown> {
    for (let i = 0; i < this.providers.length; i++) {
      if (availableOnly && !this.availability[i]) continue;

      for (let attempt = 0; attempt < this.maxRetryPerProvider; attempt++) {
        try {
          getLogger().info(
            `FallbackLLMProvider: trying provider ${i}${attempt > 0 ? ` (retry ${attempt})` : ''}`,
          );

          let yieldedTokens = false;
          const gen = this.providers[i].stream(messages, tools);

          // Consume and re-yield every chunk from the provider
          while (true) {
            let iterResult: IteratorResult<LLMChunk, void>;
            try {
              iterResult = await gen.next();
            } catch (err) {
              // Error during iteration — check if partial output was consumed
              if (yieldedTokens) {
                const msg = `FallbackLLMProvider: provider ${i} failed after yielding tokens — cannot retry`;
                getLogger().warn(msg);
                throw new PartialStreamError(msg);
              }
              throw err;
            }

            if (iterResult.done) break;

            yield iterResult.value;
            yieldedTokens = true;
          }

          // If we reach here, the provider succeeded — restore availability
          if (!this.availability[i]) {
            this.availability[i] = true;
            this.stopRecovery(i);
            getLogger().info(
              `FallbackLLMProvider: provider ${i} recovered`,
            );
          }

          return 'done';
        } catch (err) {
          if (err instanceof PartialStreamError) {
            throw err; // Do not retry
          }

          const error =
            err instanceof Error ? err : new Error(String(err));
          errors.push(error);
          getLogger().warn(
            `FallbackLLMProvider: provider ${i} attempt ${attempt + 1} failed — ${error.message}`,
          );
        }
      }

      // Mark provider as unavailable and start background recovery
      this.markUnavailable(i);
    }

    return 'exhausted';
  }

  private markUnavailable(index: number): void {
    if (!this.availability[index]) return; // already marked
    this.availability[index] = false;
    getLogger().warn(
      `FallbackLLMProvider: marking provider ${index} as unavailable`,
    );
    this.startRecovery(index);
  }

  private startRecovery(index: number): void {
    if (this.recoveryTimers[index] !== null) return; // already running

    this.recoveryTimers[index] = setInterval(async () => {
      try {
        getLogger().debug(
          `FallbackLLMProvider: probing provider ${index} for recovery`,
        );
        const gen = this.providers[index].stream(
          [{ role: 'user', content: 'ping' }],
          null,
        );
        // Drain one chunk to verify the provider responds
        await gen.next();

        this.availability[index] = true;
        this.stopRecovery(index);
        getLogger().info(
          `FallbackLLMProvider: provider ${index} recovered`,
        );
      } catch {
        // Still unavailable — keep probing
      }
    }, this.recoveryIntervalMs);
  }

  private stopRecovery(index: number): void {
    if (this.recoveryTimers[index] !== null) {
      clearInterval(this.recoveryTimers[index]!);
      this.recoveryTimers[index] = null;
    }
  }
}

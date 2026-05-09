/**
 * Barge-in confirmation strategies.
 *
 * When a caller starts speaking while the agent's TTS is in flight, the SDK
 * has to decide whether the speech is a real interruption or just a brief
 * backchannel ("uh-huh", "okay") / room noise / cough. The default
 * behaviour is to treat any VAD speech_start as a confirmed barge-in and
 * cancel the agent immediately. That is fine for clean inputs but
 * produces frequent false positives on PSTN: the agent gets cut
 * mid-sentence by background chatter, breath, or filler words and never
 * recovers the conversational thread.
 *
 * Each ``BargeInStrategy`` is consulted on every STT transcript while a
 * barge-in is *pending* (VAD fired, but the agent has not yet been
 * cancelled). The first strategy that returns ``true`` confirms the
 * barge-in; if none do within the configured timeout the pending state
 * is dropped and the agent resumes streaming TTS as if nothing happened.
 * With an empty ``bargeInStrategies`` array the SDK falls back to the
 * legacy "interrupt immediately on VAD" path, so adding strategies is
 * a strict opt-in.
 */

import { getLogger } from '../logger.js';

export interface EvaluateContext {
  /** Latest STT output text (interim or final). */
  readonly transcript: string;
  /** ``true`` for interim partials, ``false`` for finals. */
  readonly isInterim: boolean;
  /** Whether the agent's TTS is currently in flight. */
  readonly agentSpeaking: boolean;
}

/**
 * Decides whether a pending barge-in should be confirmed.
 *
 * Implementations must be safe to call from any number of evaluations
 * per turn. ``reset`` is invoked when the agent finishes speaking
 * naturally and when a pending barge-in times out without
 * confirmation.
 */
export interface BargeInStrategy {
  evaluate(ctx: EvaluateContext): Promise<boolean> | boolean;
  reset?(): Promise<void> | void;
}

export interface MinWordsStrategyOptions {
  /**
   * Minimum word count required while the agent is speaking. Reasonable
   * values are 2-5; 3 is a good starting point for production phone
   * agents. Must be ``>= 1``.
   */
  readonly minWords: number;
  /**
   * When ``true`` (default), interim STT partials are evaluated as soon
   * as they arrive. Set to ``false`` to wait for finals only — slower
   * but free of partial-word noise on jittery STT providers.
   */
  readonly useInterim?: boolean;
}

/**
 * Confirm barge-in only after the caller has spoken ``minWords`` words.
 *
 * Filters short backchannels, single-word utterances, and stray
 * transcription fragments that VAD picked up but were not real
 * interruptions. While the agent is silent the strategy permits any
 * speech to count (one word is enough), so the first user turn is not
 * delayed.
 */
export class MinWordsStrategy implements BargeInStrategy {
  private readonly minWords: number;
  private readonly useInterim: boolean;

  constructor(options: MinWordsStrategyOptions) {
    if (!Number.isFinite(options.minWords) || options.minWords < 1) {
      throw new Error(
        `minWords must be >= 1 (got ${String(options.minWords)})`,
      );
    }
    this.minWords = Math.floor(options.minWords);
    this.useInterim = options.useInterim ?? true;
  }

  evaluate(ctx: EvaluateContext): boolean {
    if (ctx.isInterim && !this.useInterim) {
      return false;
    }
    const threshold = ctx.agentSpeaking ? this.minWords : 1;
    const wordCount = (ctx.transcript ?? '').trim().split(/\s+/).filter(Boolean).length;
    return wordCount >= threshold;
  }

  async reset(): Promise<void> {
    /* stateless */
  }
}

/**
 * Short-circuit-OR composition: first strategy that confirms wins.
 * Returns ``false`` for an empty array so callers can use the empty
 * default to mean "no opt-in confirmation, fall back to legacy
 * interrupt-on-VAD".
 */
export async function evaluateStrategies(
  strategies: readonly BargeInStrategy[],
  ctx: EvaluateContext,
): Promise<boolean> {
  if (!strategies || strategies.length === 0) {
    return false;
  }
  const safeCtx: EvaluateContext = {
    transcript: ctx.transcript ?? '',
    isInterim: ctx.isInterim,
    agentSpeaking: ctx.agentSpeaking,
  };
  for (const strategy of strategies) {
    try {
      const result = await strategy.evaluate(safeCtx);
      if (result === true) return true;
    } catch (err) {
      getLogger().warn(
        `BargeInStrategy ${strategy.constructor?.name ?? 'unknown'} threw; treating as 'do not confirm': ${String(err)}`,
      );
    }
  }
  return false;
}

/** Call ``reset()`` on every strategy, swallowing per-strategy errors. */
export async function resetStrategies(
  strategies: readonly BargeInStrategy[],
): Promise<void> {
  for (const strategy of strategies) {
    if (typeof strategy.reset !== 'function') continue;
    try {
      await strategy.reset();
    } catch (err) {
      getLogger().debug(
        `BargeInStrategy ${strategy.constructor?.name ?? 'unknown'}.reset() threw: ${String(err)}`,
      );
    }
  }
}

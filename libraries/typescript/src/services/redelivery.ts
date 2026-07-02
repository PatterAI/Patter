/**
 * Re-delivery policy for opt-in barge-in re-delivery.
 *
 * When a caller interrupts (barges in on) the agent mid-answer, the un-heard
 * remainder of the reply is normally dropped: the turn is cancelled and the
 * agent answers the caller's new message from scratch. Sometimes the un-heard
 * remainder still mattered (a price, a caveat, the second half of an answer).
 *
 * With ``AgentOptions.redeliverInterrupted`` on, the SDK captures the
 * ``{heard, unsaid}`` split at the moment of a confirmed barge-in and, on the
 * NEXT user turn, injects a one-shot system nudge that asks the LLM to answer
 * the caller's new message first and then resume the unfinished idea naturally
 * (never mechanically repeat it, drop it if the caller has moved on).
 *
 * A ``RedeliveryPolicy`` decides whether a given interruption is *worth*
 * resuming. The default ``MinUnsaidWordsPolicy`` arms re-delivery only when the
 * caller had NOT heard everything and the un-heard tail is at least
 * ``DEFAULT_MIN_UNSAID_WORDS`` words — below that the remainder is a trailing
 * word or two and resuming it adds more noise than value. Users can supply
 * their own policy via ``AgentOptions.redeliveryPolicy``. Mirrors Python
 * ``services/redelivery.py``.
 *
 * This is a pipeline-mode feature: the heard/unsaid split is derived from the
 * pipeline's per-sentence playback timeline. Realtime / ConvAI modes have no
 * such split and never arm re-delivery.
 */

/**
 * Minimum number of un-heard words for a re-delivery to be worth arming.
 * Below this the unfinished tail is trivial (a trailing word or two) and
 * resuming it would add more noise than value. Exported so the threshold is
 * documented and overridable via a custom ``RedeliveryPolicy``.
 */
export const DEFAULT_MIN_UNSAID_WORDS = 3;

/** The heard/unsaid split passed to a ``RedeliveryPolicy``. */
export interface RedeliveryDecisionContext {
  /** Prefix of the reply the caller actually heard (may be empty). */
  readonly heard: string;
  /** Un-heard remainder of the reply (word-boundary split). */
  readonly unsaid: string;
  /** ``true`` when the caller heard the whole reply (nothing to resume). */
  readonly heardEverything: boolean;
}

/**
 * Decides whether an interrupted turn's un-heard remainder is worth resuming
 * on the next turn. Implementations are pure and stateless — the decision is a
 * function of the current split only. Mirror of the Python ``RedeliveryPolicy``
 * protocol.
 */
export interface RedeliveryPolicy {
  shouldRedeliver(ctx: RedeliveryDecisionContext): boolean;
}

export interface MinUnsaidWordsPolicyOptions {
  /**
   * Minimum un-heard word count required to arm re-delivery. Must be ``>= 1``.
   * Defaults to ``DEFAULT_MIN_UNSAID_WORDS``.
   */
  readonly minWords?: number;
}

/**
 * Arm re-delivery only when a non-trivial remainder went un-heard. Returns
 * ``false`` when the caller heard everything (nothing to resume) and when the
 * un-heard tail is shorter than ``minWords`` words. This is the default
 * policy.
 */
export class MinUnsaidWordsPolicy implements RedeliveryPolicy {
  private readonly minWords: number;

  constructor(options: MinUnsaidWordsPolicyOptions = {}) {
    const minWords = options.minWords ?? DEFAULT_MIN_UNSAID_WORDS;
    if (!Number.isFinite(minWords) || minWords < 1) {
      throw new Error(`minWords must be >= 1 (got ${String(minWords)})`);
    }
    this.minWords = Math.floor(minWords);
  }

  shouldRedeliver(ctx: RedeliveryDecisionContext): boolean {
    if (ctx.heardEverything) return false;
    const words = ctx.unsaid.trim().split(/\s+/).filter(Boolean).length;
    return words >= this.minWords;
  }
}

/** Process-wide default policy instance (stateless, safe to share). */
export const DEFAULT_REDELIVERY_POLICY: RedeliveryPolicy = new MinUnsaidWordsPolicy();

/**
 * Build the one-shot system nudge injected on the turn after a barge-in.
 * ``heard`` / ``unsaid`` are the caller-heard prefix and un-heard remainder
 * captured at the interruption. When ``heard`` is empty the nudge phrases it as
 * nothing-yet-heard. Mirrors Python ``build_redelivery_nudge``.
 */
export function buildRedeliveryNudge(heard: string, unsaid: string): string {
  const heardClause = heard.trim()
    ? `Recently heard by the user: ${heard.trim()}.`
    : 'The user had not yet heard any of it.';
  return (
    'You were interrupted while speaking. ' +
    `${heardClause} ` +
    `Not yet heard by the user: ${unsaid.trim()}. ` +
    "Answer the user's latest message first. Then, only if it is still " +
    'relevant, resume the unfinished idea naturally — weave it in, do not ' +
    'mechanically repeat the unheard text, and drop it entirely if the ' +
    'caller has moved on.'
  );
}

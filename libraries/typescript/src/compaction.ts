/**
 * Token-aware conversation-history compaction for pipeline mode.
 *
 * Adds two capabilities around the existing per-call history representation
 * (the array owned by {@link createHistoryManager} in the stream handler):
 *
 * 1. **Token counting** — {@link estimateTokens} / {@link estimateMessagesTokens}
 *    estimate prompt size in the canonical OpenAI `chars / 4` unit (the same
 *    baseline the SDK already uses for fallback billing in `llm-loop.ts`). No
 *    heavyweight tokenizer dependency is pulled in.
 *
 * 2. **Hybrid compaction** — {@link ContextCompactor} keeps the most recent
 *    `keepLastTurns` turns VERBATIM and folds the older turns into a rolling
 *    summary once the estimated size of the summarizable portion crosses a
 *    token budget. The summary is produced by an injected async *summarizer*
 *    (the agent's own LLM by default) and the summarized entries are pruned
 *    from the working history array so the next turn's prompt stays bounded.
 *
 * Mirrors the Python `getpatter.services.compaction` module.
 */

import type { CompactionConfig } from './types';
import { getLogger } from './logger';

/** Per-message structural overhead (role tag + delimiters) in estimated tokens. */
const PER_MESSAGE_OVERHEAD = 4;

/** Resolved compaction thresholds (defaults applied). Mirrors Python defaults.
 * ``targetTokens`` is consumed by the summarizer (wired in the stream handler),
 * not by the compactor itself, so only the trigger / keep defaults live here. */
const DEFAULT_TRIGGER_TOKENS = 8000;
const DEFAULT_KEEP_LAST_TURNS = 4;

/** A history / chat message that carries text under `content` or `text`. */
type TokenCountable = { content?: unknown; text?: unknown };

/** A summarizer folds the prior summary plus the old messages into a new summary. */
export type Summarizer = (
  priorSummary: string,
  oldMessages: Array<{ role: string; text: string }>,
) => Promise<string>;

/**
 * Estimate the token count of `text` using the `chars / 4` baseline — the
 * canonical rough OpenAI-tokenizer estimate already used for fallback billing.
 * Empty / nullish text is 0 tokens.
 */
export function estimateTokens(text: string | null | undefined): number {
  if (!text) return 0;
  return Math.max(1, Math.floor(text.length / 4));
}

/**
 * Estimate the total token count of a list of chat / history messages. Accepts
 * provider-format messages (`{ content }`) and internal history entries
 * (`{ text }`). Adds a small per-message overhead for role tags / delimiters.
 */
export function estimateMessagesTokens(messages: Iterable<TokenCountable>): number {
  let total = 0;
  for (const msg of messages) {
    let content: unknown = msg.content;
    if (content === undefined || content === null) content = msg.text;
    const text = typeof content === 'string' ? content : content == null ? '' : String(content);
    total += estimateTokens(text) + PER_MESSAGE_OVERHEAD;
  }
  return total;
}

/** Render the to-be-summarized entries as a plain transcript for the LLM. */
export function formatOldMessages(
  oldMessages: ReadonlyArray<{ role: string; text: string }>,
): string {
  return oldMessages.map((m) => `${m.role ?? 'user'}: ${m.text ?? ''}`).join('\n');
}

/**
 * Token-budget-driven rolling summarizer for a per-call history array.
 *
 * Holds the rolling `summary` string and a re-entrancy guard so at most one
 * summarization runs at a time. The summary is read by the LLM loop (via the
 * stream-handler wiring) and prepended to every prompt; the summarized entries
 * are removed from the working array by {@link maybeCompact}.
 */
export class ContextCompactor {
  private readonly triggerTokens: number;
  private readonly keepLastTurns: number;
  private readonly summarizer: Summarizer;
  private _summary = '';
  private _compacting = false;

  constructor(config: CompactionConfig, summarizer: Summarizer) {
    this.triggerTokens = config.triggerTokens ?? DEFAULT_TRIGGER_TOKENS;
    this.keepLastTurns = Math.max(1, config.keepLastTurns ?? DEFAULT_KEEP_LAST_TURNS);
    this.summarizer = summarizer;
  }

  /** The current rolling summary ("" until the first compaction). */
  get summary(): string {
    return this._summary;
  }

  /** True while a background summarization is in flight. */
  get isCompacting(): boolean {
    return this._compacting;
  }

  /** Number of trailing history entries kept verbatim (>= 2). */
  private keepMessages(): number {
    // A "turn" is approximated as a user+assistant pair; floor at 1 turn.
    return this.keepLastTurns * 2;
  }

  /**
   * Whether the summarizable portion has crossed `triggerTokens`. The
   * summarizable portion is everything EXCEPT the trailing `keepLastTurns`
   * turns, plus the prior summary (which is re-folded).
   */
  shouldCompact(history: ReadonlyArray<{ role: string; text: string }>): boolean {
    const keep = this.keepMessages();
    if (history.length <= keep) return false;
    const old = history.slice(0, history.length - keep);
    if (old.length === 0) return false;
    const oldTokens = estimateMessagesTokens(old);
    return oldTokens + estimateTokens(this._summary) >= this.triggerTokens;
  }

  /**
   * Summarize and prune the oldest entries when over budget.
   *
   * Non-blocking by design: intended to run as a background task so the live
   * turn never waits on the summarizer. Resolves to the new summary when a
   * compaction ran, else `null` (short call / already running). The trailing
   * `keepLastTurns` turns stay verbatim; older entries are folded into the
   * rolling summary and removed from `history` in place (`splice` from the
   * front). Entries appended while the summarizer awaits stay untouched.
   */
  async maybeCompact(history: Array<{ role: string; text: string }>): Promise<string | null> {
    if (this._compacting) return null;
    if (!this.shouldCompact(history)) return null;

    this._compacting = true;
    try {
      const keep = this.keepMessages();
      const numOld = Math.max(0, history.length - keep);
      if (numOld <= 0) return null;
      const old = history.slice(0, numOld);

      const raw = await this.summarizer(this._summary, old);
      const newSummary = (raw ?? '').trim();
      if (!newSummary) {
        // Summarizer produced nothing usable — do NOT prune, or the turns
        // would be lost with no summary to replace them.
        getLogger().warn(`context_compaction_empty_summary kept=${keep} old=${numOld}`);
        return null;
      }

      // Prune exactly the entries we summarized. Appends during the await
      // landed at the right end of the array, so the leading `numOld` are
      // still the summarized ones.
      history.splice(0, Math.min(numOld, history.length));

      this._summary = newSummary;
      getLogger().info(
        `context_compaction summarized=${numOld} kept=${history.length} ` +
          `summary_tokens=${estimateTokens(newSummary)}`,
      );
      return newSummary;
    } finally {
      this._compacting = false;
    }
  }
}

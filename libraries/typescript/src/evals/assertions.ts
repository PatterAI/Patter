/**
 * Fluent assertions for {@link TurnResult}.
 *
 * Chainable expectations against the outcome of one real pipeline turn
 * driven by {@link EvalSession}:
 *
 * ```ts
 * const result = await session.userSays('book me a table for two');
 * expect(result)
 *   .toolCalled('book_table', { partySize: 2 })
 *   .agentTextContains('booked');
 *
 * // Semantic check via the LLMJudge (async, ends the chain):
 * await expect(result).judge(new LLMJudge(), {
 *   intent: 'The agent confirms the booking and offers help.',
 * });
 * ```
 *
 * Every failed expectation throws Node's ``AssertionError`` with the
 * observed values, so plain vitest reports are actionable without extra
 * plumbing. Mirrors the Python `getpatter.evals.assertions` module.
 */

import { AssertionError } from 'node:assert';
import { getLogger } from '../logger';
import type { EvalCase, JudgeResult } from './case';
import type { LLMJudge } from './llm-judge';
import { historyTranscript } from './session';
import type { TurnResult } from './session';

/** Wrap a {@link TurnResult} in a chainable expectation object. */
export function expect(result: TurnResult): TurnExpectation {
  return new TurnExpectation(result);
}

/** Deep structural equality for the non-object branch of {@link isSubset}. */
function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => deepEqual(v, b[i]));
  }
  if (
    a !== null &&
    b !== null &&
    typeof a === 'object' &&
    typeof b === 'object' &&
    !Array.isArray(a) &&
    !Array.isArray(b)
  ) {
    const ak = Object.keys(a as Record<string, unknown>);
    const bk = Object.keys(b as Record<string, unknown>);
    return (
      ak.length === bk.length &&
      ak.every((k) =>
        deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]),
      )
    );
  }
  return false;
}

/**
 * True when ``subset`` is recursively contained in ``actual``.
 *
 * Plain objects match when every key in ``subset`` exists in ``actual`` and
 * its value matches recursively; every other type (including arrays)
 * matches by deep equality — same semantics as the Python helper.
 */
function isSubset(subset: unknown, actual: unknown): boolean {
  if (subset !== null && typeof subset === 'object' && !Array.isArray(subset)) {
    if (actual === null || typeof actual !== 'object' || Array.isArray(actual)) {
      return false;
    }
    const actualRecord = actual as Record<string, unknown>;
    return Object.entries(subset as Record<string, unknown>).every(
      ([key, value]) => key in actualRecord && isSubset(value, actualRecord[key]),
    );
  }
  return deepEqual(subset, actual);
}

/** Options for {@link TurnExpectation.agentTextContains}. */
export interface AgentTextContainsOptions {
  /** Compare needles case-sensitively. Default: false (Python parity). */
  readonly caseSensitive?: boolean;
}

/** Chainable assertions over one turn. See {@link expect}. */
export class TurnExpectation {
  private readonly turnResult: TurnResult;

  constructor(result: TurnResult) {
    this.turnResult = result;
  }

  /** The wrapped {@link TurnResult} (escape hatch for ad-hoc asserts). */
  get result(): TurnResult {
    return this.turnResult;
  }

  // -- tools -----------------------------------------------------------------

  /**
   * Assert that tool ``name`` ran this turn.
   *
   * ``argsSubset`` (optional) must be recursively contained in the args of
   * at least one matching invocation — extra argument keys are allowed,
   * listed keys must match exactly.
   */
  toolCalled(name: string, argsSubset?: Record<string, unknown>): TurnExpectation {
    const matches = this.turnResult.toolCalls.filter((tc) => tc.name === name);
    if (matches.length === 0) {
      const called = this.turnResult.toolCalls.map((tc) => tc.name);
      throw new AssertionError({
        message:
          `expected tool ${JSON.stringify(name)} to be called this turn; ` +
          `tools called: ${called.length > 0 ? JSON.stringify(called) : 'none'}`,
      });
    }
    if (argsSubset !== undefined && !matches.some((tc) => isSubset(argsSubset, tc.arguments))) {
      throw new AssertionError({
        message:
          `tool ${JSON.stringify(name)} was called, but no invocation matched ` +
          `argsSubset=${JSON.stringify(argsSubset)}; observed args: ` +
          JSON.stringify(matches.map((tc) => tc.arguments)),
      });
    }
    return this;
  }

  /** Assert that no tool ran this turn (or that ``name`` did not). */
  noToolCalled(name?: string): TurnExpectation {
    if (name === undefined) {
      if (this.turnResult.toolCalls.length > 0) {
        throw new AssertionError({
          message:
            'expected no tool calls this turn; tools called: ' +
            JSON.stringify(this.turnResult.toolCalls.map((tc) => tc.name)),
        });
      }
      return this;
    }
    const offenders = this.turnResult.toolCalls.filter((tc) => tc.name === name);
    if (offenders.length > 0) {
      throw new AssertionError({
        message:
          `expected tool ${JSON.stringify(name)} NOT to be called this turn; ` +
          `it ran ${offenders.length} time(s) with args ` +
          JSON.stringify(offenders.map((tc) => tc.arguments)),
      });
    }
    return this;
  }

  // -- text ------------------------------------------------------------------

  /**
   * Assert that every needle appears in the spoken agent text.
   *
   * Variadic form is case-insensitive (Python default); pass an array plus
   * an options object for ``caseSensitive: true``.
   */
  agentTextContains(...needles: string[]): TurnExpectation;
  agentTextContains(
    needles: string | ReadonlyArray<string>,
    options?: AgentTextContainsOptions,
  ): TurnExpectation;
  agentTextContains(
    first: string | ReadonlyArray<string>,
    ...rest: Array<string | AgentTextContainsOptions | undefined>
  ): TurnExpectation {
    let needles: string[];
    let caseSensitive = false;
    if (Array.isArray(first)) {
      needles = [...first];
      const options = rest[0] as AgentTextContainsOptions | undefined;
      caseSensitive = options?.caseSensitive ?? false;
    } else {
      needles = [first as string, ...(rest as string[])].filter(
        (n): n is string => typeof n === 'string',
      );
    }
    const haystack = this.turnResult.agentText;
    const cmpHaystack = caseSensitive ? haystack : haystack.toLowerCase();
    const missing = needles.filter(
      (n) => !cmpHaystack.includes(caseSensitive ? n : n.toLowerCase()),
    );
    if (missing.length > 0) {
      throw new AssertionError({
        message:
          `agent text is missing ${JSON.stringify(missing)}; agent said: ` +
          JSON.stringify(haystack),
      });
    }
    return this;
  }

  // -- semantic judge ----------------------------------------------------------

  /**
   * Score this turn against ``intent`` with the LLM judge.
   *
   * Builds a synthetic {@link EvalCase} whose ``expectedBehavior`` is
   * ``intent`` and judges the turn's full history snapshot. Throws
   * ``AssertionError`` when the judge fails the turn; returns the
   * {@link JudgeResult} otherwise (chain-ending, async).
   */
  async judge(
    llmJudge: LLMJudge,
    options: { readonly intent: string; readonly rubric?: string },
  ): Promise<JudgeResult> {
    const { intent, rubric } = options;
    const evalCase: EvalCase = {
      name: 'inline-judge',
      turns: [],
      expectedBehavior: intent,
      rubric: rubric ?? `Pass when the agent's behavior matches: ${intent}`,
    };
    const transcript = historyTranscript(this.turnResult.historySnapshot);
    const verdict = await llmJudge.judgeCase(evalCase, transcript);
    getLogger().info(
      `judge intent=${JSON.stringify(intent)} score=${verdict.score.toFixed(2)} passed=${verdict.passed}`,
    );
    if (!verdict.passed) {
      throw new AssertionError({
        message:
          `LLM judge failed the turn (score=${verdict.score.toFixed(2)}): ` +
          `${verdict.reasoning} — intent was ${JSON.stringify(intent)}; agent said ` +
          JSON.stringify(this.turnResult.agentText),
      });
    }
    return verdict;
  }
}

/**
 * Eval case data model.
 *
 * An {@link EvalCase} is a scripted scenario: a sequence of user turns, an
 * expected-behavior description, and a rubric used by the judge LLM.
 *
 * Designed to be loaded from a YAML/JSON suite file — see
 * {@link loadSuite} in `runner.ts`. Mirrors the Python
 * `getpatter.evals.case` module (frozen dataclasses → readonly interfaces).
 */

import type { AgentOptions } from '../types';
import type { LLMProvider } from '../llm-loop';

/** A single user utterance in a scripted conversation. */
export interface EvalTurn {
  readonly user: string;
  /**
   * Optional substrings the agent's reply should contain — used as a cheap
   * pre-filter (logged, never fatal) before invoking the LLM judge.
   */
  readonly expectedContains?: ReadonlyArray<string>;
}

/** A complete evaluation scenario. */
export interface EvalCase {
  readonly name: string;
  readonly turns: ReadonlyArray<EvalTurn>;
  readonly expectedBehavior: string;
  readonly rubric: string;
  /** Optional metadata for reporting/filtering. */
  readonly tags?: ReadonlyArray<string>;
  /**
   * Optional first-message the agent should emit before any user turn.
   * On the real-pipeline path (``agent`` set) it overrides the agent's own
   * ``firstMessage`` so the REAL handler speaks it; on the legacy
   * ``reply()`` path it is prepended to the transcript display-only.
   */
  readonly firstMessage?: string;
  /**
   * Optional REAL-pipeline target. When ``agent`` is set, the runner drives
   * the case through {@link EvalSession} — the real `StreamHandler`
   * pipeline call loop (tools, hooks, guardrails, history) — instead of the
   * legacy ``reply()``-callable factory. ``llmProvider`` optionally
   * overrides ``agent.llm`` (e.g. a {@link ScriptedLLMProvider} for CI).
   * Both default to ``undefined`` so existing suites are unaffected.
   */
  readonly agent?: AgentOptions;
  readonly llmProvider?: LLMProvider;
}

/** The judge's verdict on one case. */
export interface JudgeResult {
  /** Score in [0.0, 1.0]. */
  readonly score: number;
  readonly passed: boolean;
  readonly reasoning: string;
}

/** One line of a judge-facing transcript. */
export interface TranscriptEntry {
  readonly role: string;
  readonly text: string;
}

/** The result of running a single {@link EvalCase}. */
export interface EvalResult {
  readonly caseName: string;
  readonly transcript: ReadonlyArray<TranscriptEntry>;
  readonly judge: JudgeResult;
  readonly durationS: number;
  readonly error: string | null;
}

/**
 * Render an {@link EvalResult} as the JSON-report row shape — mirrors the
 * Python `EvalResult.to_dict()` (snake_case keys, stable across SDKs so CI
 * artefacts are interchangeable).
 */
export function evalResultToDict(result: EvalResult): Record<string, unknown> {
  return {
    case: result.caseName,
    score: result.judge.score,
    passed: result.judge.passed,
    reasoning: result.judge.reasoning,
    transcript: result.transcript.map((t) => ({ role: t.role, text: t.text })),
    duration_s: Math.round(result.durationS * 1000) / 1000,
    error: result.error,
  };
}

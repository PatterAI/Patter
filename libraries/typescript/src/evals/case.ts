/**
 * Eval case data model.
 *
 * An {@link EvalCase} is a scripted scenario: a sequence of user turns, an
 * expected-behavior description, and a rubric used by the judge LLM.
 *
 * Designed to be loaded from a YAML/JSON suite file — see `loadSuite` in
 * `./runner`. Parity with `getpatter.evals.case` (Python).
 */

/** A single user utterance in a scripted conversation. */
export interface EvalTurn {
  readonly user: string;
  /**
   * Optional substrings the agent's reply should contain — used as a cheap,
   * case-insensitive pre-filter (logged on miss) before invoking the LLM judge.
   */
  readonly expectedContains?: readonly string[];
}

/** A complete evaluation scenario. */
export interface EvalCase {
  readonly name: string;
  readonly turns: readonly EvalTurn[];
  readonly expectedBehavior: string;
  readonly rubric: string;
  /** Optional metadata for reporting/filtering. */
  readonly tags?: readonly string[];
  /**
   * Optional first-message the agent emits before any user turn. On the
   * legacy `reply()` path it is prepended to the transcript (display-only).
   */
  readonly firstMessage?: string;
}

/** The judge's verdict on one case. */
export interface JudgeResult {
  /** Score in the inclusive range 0.0–1.0. */
  readonly score: number;
  readonly passed: boolean;
  readonly reasoning: string;
}

/** One line of a conversation transcript: who spoke and what they said. */
export interface TranscriptEntry {
  /** `"user"` or `"agent"`. */
  readonly role: string;
  readonly text: string;
}

/** The result of running a single {@link EvalCase}. */
export class EvalResult {
  constructor(
    readonly caseName: string,
    readonly transcript: readonly TranscriptEntry[],
    readonly judge: JudgeResult,
    readonly durationS: number,
    readonly error: string | null = null,
  ) {}

  /**
   * Serialise to the JSON-report shape — parity with the Python
   * `EvalResult.to_dict()` (snake_case keys for the shared report artefact
   * consumed by CI in either SDK).
   */
  toDict(): Record<string, unknown> {
    return {
      case: this.caseName,
      score: this.judge.score,
      passed: this.judge.passed,
      reasoning: this.judge.reasoning,
      transcript: this.transcript.map((t) => ({ role: t.role, text: t.text })),
      duration_s: Math.round(this.durationS * 1000) / 1000,
      error: this.error,
    };
  }
}

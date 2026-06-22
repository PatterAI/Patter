/**
 * Patter evaluation framework — TypeScript twin of `getpatter.evals` (Python).
 *
 * Primitives:
 * - {@link EvalCase} — declarative description of a test case
 * - {@link EvalRunner} — drives one or more cases
 * - {@link LLMJudge} — scores transcripts against a rubric
 * - {@link loadSuite} — load a YAML/JSON suite file
 */

export { EvalResult } from './case';
export type { EvalTurn, EvalCase, JudgeResult, TranscriptEntry } from './case';

export { LLMJudge, OpenAIJudgeBackend, JUDGE_SYSTEM } from './llm-judge';
export type { JudgeBackend, LLMJudgeOptions } from './llm-judge';

export { EvalRunner, loadSuite } from './runner';
export type {
  EvalSuite,
  EvalRunnerOptions,
  AgentReply,
  AgentFactory,
} from './runner';

// Note: the CLI entry `runEval` (in `./cli`) is intentionally NOT re-exported
// here — it mirrors Python's `getpatter.evals`, which exposes the data model /
// runner / judge but not the CLI dispatcher. `src/cli.ts` imports it directly
// from `./evals/cli`.

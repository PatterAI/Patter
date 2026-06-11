/**
 * Patter evaluation framework — mirrors Python `getpatter.evals`.
 *
 * Primitives:
 * - {@link EvalCase} — declarative description of a test case
 * - {@link EvalRunner} — drives one or more cases
 * - {@link LLMJudge} — uses an LLM to score a transcript against a rubric
 *
 * Real-pipeline harness (drives an actual pipeline `StreamHandler` — tools,
 * hooks, guardrails, barge-in state machine, history — with fake telephony /
 * STT / TTS at the boundary):
 * - {@link EvalSession} — `await EvalSession.create({ agent, llmProvider })`
 *   then `await session.userSays('hi')` / `await session.close()`
 * - {@link ScriptedLLMProvider} (+ {@link textTurn} / {@link toolCallTurn})
 *   — deterministic LLM for CI
 * - {@link expect} — chainable assertions over a {@link TurnResult}
 *   (`toolCalled`, `noToolCalled`, `agentTextContains`, async `judge`)
 */

export type {
  EvalCase,
  EvalResult,
  EvalTurn,
  JudgeResult,
  TranscriptEntry,
} from './case';
export { evalResultToDict } from './case';
export { LLMJudge } from './llm-judge';
export type { JudgeBackend, LLMJudgeOptions } from './llm-judge';
export { EvalRunner, loadSuite } from './runner';
export type { AgentCallable, AgentFactory, EvalRunnerOptions, EvalSuite } from './runner';
export { FakeAudioSender, FakeSTT, FakeTTS } from './fakes';
export { ScriptedLLMProvider, textTurn, toolCallTurn } from './scripted-llm';
export type { ScriptedLLMCall } from './scripted-llm';
export { EvalSession, historyTranscript } from './session';
export type { EvalSessionOptions, ToolCallRecord, TurnResult } from './session';
export { expect, TurnExpectation } from './assertions';
export type { AgentTextContainsOptions } from './assertions';

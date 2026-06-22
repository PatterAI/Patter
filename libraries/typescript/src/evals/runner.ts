/**
 * Eval runner — executes an {@link EvalSuite} against a scripted agent and
 * produces a JSON report. Parity with `getpatter.evals.runner`.
 *
 * The caller supplies an `agentFactory` returning an async
 * `reply(text) => string` callable — no SDK machinery involved. This decouples
 * the runner from the real Patter Agent wiring and lets callers plug in any
 * chat-completions client or mock.
 */

import { promises as fs } from 'node:fs';
import { basename } from 'node:path';
import { getLogger } from '../logger';
import { EvalResult } from './case';
import type { EvalCase, EvalTurn, JudgeResult, TranscriptEntry } from './case';
import { LLMJudge } from './llm-judge';

/**
 * An agent factory takes no arguments and returns an async
 * `reply(text: string) => string`. `reply` receives one user turn and returns
 * the agent's final text response. The factory itself may be async.
 */
export type AgentReply = (text: string) => Promise<string>;
export type AgentFactory = () => AgentReply | Promise<AgentReply>;

/** A named collection of {@link EvalCase} to run together. */
export interface EvalSuite {
  readonly name: string;
  readonly cases: readonly EvalCase[];
  readonly metadata?: Readonly<Record<string, unknown>>;
}

export interface EvalRunnerOptions {
  readonly judge?: LLMJudge;
}

/** Drives one or more cases against an agent and produces a JSON report. */
export class EvalRunner {
  readonly judge: LLMJudge;

  constructor(opts: EvalRunnerOptions = {}) {
    this.judge = opts.judge ?? new LLMJudge();
  }

  /** Run every case in `suite` sequentially. */
  async run(suite: EvalSuite, agentFactory?: AgentFactory): Promise<EvalResult[]> {
    const results: EvalResult[] = [];
    for (const evalCase of suite.cases) {
      results.push(await this.runCase(evalCase, agentFactory));
    }
    return results;
  }

  /** Run a single case and return its {@link EvalResult}. */
  async runCase(evalCase: EvalCase, agentFactory?: AgentFactory): Promise<EvalResult> {
    const start = nowSeconds();
    const transcript: TranscriptEntry[] = [];
    let error: string | null = null;

    try {
      if (agentFactory === undefined) {
        throw new Error(
          `case ${JSON.stringify(evalCase.name)} has no agentFactory supplied`,
        );
      }
      await this.runTurnsWithReply(evalCase, agentFactory, transcript);
    } catch (exc) {
      error = `${errName(exc)}: ${errMessage(exc)}`;
      getLogger().error(`case=${JSON.stringify(evalCase.name)} raised: ${error}`);
    }

    // If we failed to produce any transcript, skip the judge.
    if (error && transcript.length === 0) {
      return new EvalResult(
        evalCase.name,
        transcript,
        { score: 0, passed: false, reasoning: error },
        nowSeconds() - start,
        error,
      );
    }

    let judgeResult: JudgeResult;
    try {
      judgeResult = await this.judge.judgeCase(evalCase, transcript);
    } catch (exc) {
      // One transient judge failure (429, timeout, missing key) used to abort
      // the WHOLE suite, discarding every completed case.
      const message = `judge error: ${errMessage(exc)}`;
      return new EvalResult(
        evalCase.name,
        transcript,
        { score: 0, passed: false, reasoning: message },
        nowSeconds() - start,
        message,
      );
    }
    return new EvalResult(
      evalCase.name,
      transcript,
      judgeResult,
      nowSeconds() - start,
      error,
    );
  }

  /**
   * Drives the case against a `reply()` callable. Appends into `transcript`
   * in place so a mid-case exception still leaves the partial transcript for
   * the judge (existing semantics).
   */
  private async runTurnsWithReply(
    evalCase: EvalCase,
    agentFactory: AgentFactory,
    transcript: TranscriptEntry[],
  ): Promise<void> {
    const agent = await agentFactory();
    if (evalCase.firstMessage) {
      transcript.push({ role: 'agent', text: evalCase.firstMessage });
    }
    for (const turn of evalCase.turns) {
      transcript.push({ role: 'user', text: turn.user });
      const reply = (await agent(turn.user)) ?? '';
      transcript.push({ role: 'agent', text: reply });
      logMissingExpected(evalCase, turn, reply);
    }
  }

  /** Render a JSON report suitable for CI artefacts. */
  report(suite: EvalSuite, results: readonly EvalResult[]): string {
    const total = results.length;
    const passed = results.filter((r) => r.judge.passed).length;
    const payload = {
      suite: suite.name,
      total,
      passed,
      failed: total - passed,
      pass_rate: total ? passed / total : 0,
      cases: results.map((r) => r.toDict()),
    };
    return JSON.stringify(payload, null, 2);
  }
}

/**
 * Load a suite from YAML or JSON. The on-disk schema is identical to the
 * Python loader (snake_case keys) so a suite file runs unchanged in either
 * SDK.
 *
 * Schema (YAML):
 *
 * ```yaml
 * name: "customer support v1"
 * cases:
 *   - name: "greeting is warm"
 *     expected_behavior: "Agent greets the caller warmly and asks how it can help."
 *     rubric: "Pass if reply contains a greeting and an open-ended question."
 *     turns:
 *       - user: "hi"
 * ```
 */
export async function loadSuite(path: string): Promise<EvalSuite> {
  const text = await fs.readFile(path, 'utf-8');
  const lower = path.toLowerCase();
  let data: unknown;
  if (lower.endsWith('.yaml') || lower.endsWith('.yml')) {
    let yamlMod: { parse(source: string): unknown };
    try {
      yamlMod = (await import('yaml')) as { parse(source: string): unknown };
    } catch {
      throw new Error(
        "Loading YAML suites requires the 'yaml' package. Install with: npm install yaml",
      );
    }
    data = yamlMod.parse(text);
  } else {
    data = JSON.parse(text);
  }

  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error(`Eval suite ${path} must be a mapping`);
  }
  const obj = data as Record<string, unknown>;

  const casesRaw = obj.cases ?? [];
  if (!Array.isArray(casesRaw)) {
    throw new Error(`Eval suite ${path}: 'cases' must be a list`);
  }

  const cases: EvalCase[] = casesRaw.map((cRaw, i) => {
    if (cRaw === null || typeof cRaw !== 'object' || Array.isArray(cRaw)) {
      throw new Error(`Eval suite ${path}: case ${i} must be a mapping`);
    }
    const c = cRaw as Record<string, unknown>;
    const turnsRaw = Array.isArray(c.turns) ? c.turns : [];
    const turns: EvalTurn[] = turnsRaw
      .filter(
        (t): t is Record<string, unknown> =>
          t !== null && typeof t === 'object' && !Array.isArray(t),
      )
      .map((t) => ({
        user: String(t.user ?? ''),
        expectedContains: toStringArray(t.expected_contains),
      }));
    return {
      name: String(c.name ?? `case_${i}`),
      turns,
      expectedBehavior: String(c.expected_behavior ?? ''),
      rubric: String(c.rubric ?? ''),
      tags: toStringArray(c.tags),
      firstMessage: String(c.first_message ?? ''),
    };
  });

  return {
    name: String(obj.name ?? stem(path)),
    cases,
    metadata: (obj.metadata ?? {}) as Record<string, unknown>,
  };
}

function nowSeconds(): number {
  return performance.now() / 1000;
}

function errName(exc: unknown): string {
  // `exc.name` survives minification and reflects custom error subclasses;
  // fall back to the constructor name, then a generic label. Mirrors Python's
  // `type(exc).__name__`.
  if (exc instanceof Error) return exc.name || exc.constructor.name || 'Error';
  return 'Error';
}

function errMessage(exc: unknown): string {
  return exc instanceof Error ? exc.message : String(exc);
}

/**
 * Cheap pre-filter — if a required substring is missing we still let the judge
 * decide, but log for easier debugging.
 */
function logMissingExpected(evalCase: EvalCase, turn: EvalTurn, reply: string): void {
  for (const needle of turn.expectedContains ?? []) {
    if (!reply.toLowerCase().includes(needle.toLowerCase())) {
      getLogger().info(
        `case=${JSON.stringify(evalCase.name)} expectedContains=${JSON.stringify(
          needle,
        )} missing in reply`,
      );
    }
  }
}

function toStringArray(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return [];
  return value.map((x) => String(x));
}

/** File stem, mirroring Python `Path.stem` (basename without final suffix). */
function stem(path: string): string {
  const base = basename(path);
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(0, dot) : base;
}

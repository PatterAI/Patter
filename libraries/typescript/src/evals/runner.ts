/**
 * Eval runner — executes an {@link EvalSuite} against a scripted agent.
 *
 * Two execution paths per case (mirrors the Python `getpatter.evals.runner`):
 *
 * - **Legacy** (default): the caller supplies an ``agentFactory`` returning
 *   an async ``reply(text) => string`` callable — no SDK machinery involved.
 * - **Real pipeline**: when an {@link EvalCase} carries ``agent`` (an
 *   {@link AgentOptions}, optionally with ``llmProvider``), the runner
 *   drives the case through {@link EvalSession} — the real `StreamHandler`
 *   pipeline call loop with tools, hooks, guardrails, and history handling.
 *
 * ```ts
 * const evalCase: EvalCase = {
 *   name: 'books a table',
 *   turns: [{ user: 'table for two at eight' }],
 *   expectedBehavior: 'Agent books and confirms the table.',
 *   rubric: 'Pass if a booking is confirmed.',
 *   agent: myAgent,                              // real agent under test
 *   llmProvider: new ScriptedLLMProvider([...]), // or a real provider
 * };
 * const results = await new EvalRunner({ judge }).run({ name: 's', cases: [evalCase] });
 * ```
 */

import { readFile } from 'node:fs/promises';
import { extname, basename } from 'node:path';
import { getLogger } from '../logger';
import type { EvalCase, EvalResult, EvalTurn, TranscriptEntry } from './case';
import { evalResultToDict } from './case';
import { LLMJudge } from './llm-judge';

/**
 * An agent callable receives one user turn and returns the agent's final
 * text response. This decouples the runner from the real Patter Agent
 * wiring and lets callers plug in any chat-completions client or mock.
 */
export type AgentCallable = (text: string) => Promise<string>;
/** A factory takes no arguments and returns an {@link AgentCallable}. */
export type AgentFactory = () => AgentCallable | Promise<AgentCallable>;

/** A named collection of {@link EvalCase} to run together. */
export interface EvalSuite {
  readonly name: string;
  readonly cases: ReadonlyArray<EvalCase>;
  readonly metadata?: Readonly<Record<string, unknown>>;
}

/** Options for {@link EvalRunner}. */
export interface EvalRunnerOptions {
  /** Judge used to score each case. Default: ``new LLMJudge()``. */
  readonly judge?: LLMJudge;
}

/** Drives one or more cases against an agent and produces a JSON report. */
export class EvalRunner {
  readonly judge: LLMJudge;

  constructor(options: EvalRunnerOptions = {}) {
    this.judge = options.judge ?? new LLMJudge();
  }

  /**
   * Run every case in ``suite`` sequentially.
   *
   * ``agentFactory`` is required only for cases that do NOT carry their own
   * ``agent`` (the legacy ``reply()`` path).
   */
  async run(suite: EvalSuite, agentFactory?: AgentFactory): Promise<EvalResult[]> {
    const results: EvalResult[] = [];
    for (const evalCase of suite.cases) {
      results.push(await this.runCase(evalCase, agentFactory));
    }
    return results;
  }

  /**
   * Run a single case and return its {@link EvalResult}.
   *
   * Routes through the real-pipeline {@link EvalSession} when
   * ``evalCase.agent`` is set; otherwise uses the legacy ``reply()``-callable
   * ``agentFactory`` (unchanged behaviour).
   */
  async runCase(evalCase: EvalCase, agentFactory?: AgentFactory): Promise<EvalResult> {
    const start = Date.now();
    const transcript: TranscriptEntry[] = [];
    let error: string | null = null;

    try {
      if (evalCase.agent !== undefined) {
        await this.runTurnsWithSession(evalCase, transcript);
      } else {
        if (agentFactory === undefined) {
          throw new Error(
            `case ${JSON.stringify(evalCase.name)} has no agent and no agentFactory was supplied`,
          );
        }
        await this.runTurnsWithReply(evalCase, agentFactory, transcript);
      }
    } catch (exc) {
      error = formatError(exc);
      getLogger().error(`eval case=${JSON.stringify(evalCase.name)} raised: ${error}`);
    }

    // If we failed to produce any transcript, skip the judge.
    if (error !== null && transcript.length === 0) {
      return {
        caseName: evalCase.name,
        transcript,
        judge: { score: 0.0, passed: false, reasoning: error },
        durationS: (Date.now() - start) / 1000,
        error,
      };
    }

    let judgeResult;
    try {
      judgeResult = await this.judge.judgeCase(evalCase, transcript);
    } catch (exc) {
      // One transient judge failure (429, timeout, missing key) must not
      // abort the WHOLE suite, discarding every completed case.
      const judgeError = `judge error: ${exc instanceof Error ? exc.message : String(exc)}`;
      return {
        caseName: evalCase.name,
        transcript,
        judge: { score: 0.0, passed: false, reasoning: judgeError },
        durationS: (Date.now() - start) / 1000,
        error: judgeError,
      };
    }
    return {
      caseName: evalCase.name,
      transcript,
      judge: judgeResult,
      durationS: (Date.now() - start) / 1000,
      error,
    };
  }

  /**
   * Legacy path — drives the case against a ``reply()`` callable.
   *
   * Appends into ``transcript`` in place so a mid-case exception still
   * leaves the partial transcript for the judge (existing semantics).
   */
  private async runTurnsWithReply(
    evalCase: EvalCase,
    agentFactory: AgentFactory,
    transcript: TranscriptEntry[],
  ): Promise<void> {
    // Factories that return a promise are awaited (Python parity with
    // awaitable factories).
    const agent = await agentFactory();

    if (evalCase.firstMessage) {
      transcript.push({ role: 'agent', text: evalCase.firstMessage });
    }

    for (const turn of evalCase.turns) {
      transcript.push({ role: 'user', text: turn.user });
      const reply = typeof agent === 'function' ? await agent(turn.user) : '';
      transcript.push({ role: 'agent', text: reply || '' });
      logMissingExpected(evalCase, turn, reply || '');
    }
  }

  /**
   * Real-pipeline path — drives the case through {@link EvalSession}.
   *
   * The agent's REAL handler emits its own ``firstMessage`` (a
   * ``evalCase.firstMessage`` overrides the agent's), tools/hooks/guardrails
   * run for real, and the transcript mirrors what the pipeline actually
   * said. Appends into ``transcript`` in place (partial-on-error, same as
   * the legacy path).
   */
  private async runTurnsWithSession(
    evalCase: EvalCase,
    transcript: TranscriptEntry[],
  ): Promise<void> {
    // Local import keeps the runner light for the CLI — the session module
    // pulls in the stream handler.
    const { EvalSession } = await import('./session');

    if (!evalCase.agent) {
      throw new Error(`case ${JSON.stringify(evalCase.name)} has no agent — use the reply-factory path`);
    }
    let agent = evalCase.agent;
    if (evalCase.firstMessage) {
      agent = { ...agent, firstMessage: evalCase.firstMessage };
    }

    const session = await EvalSession.create({
      agent,
      llmProvider: evalCase.llmProvider,
    });
    try {
      if (agent.firstMessage) {
        transcript.push({ role: 'agent', text: agent.firstMessage });
      }
      for (const turn of evalCase.turns) {
        transcript.push({ role: 'user', text: turn.user });
        const result = await session.userSays(turn.user);
        transcript.push({ role: 'agent', text: result.agentText });
        logMissingExpected(evalCase, turn, result.agentText);
      }
    } finally {
      await session.close();
    }
  }

  /** Render a JSON report suitable for CI artefacts. */
  report(suite: EvalSuite, results: ReadonlyArray<EvalResult>): string {
    const total = results.length;
    const passed = results.filter((r) => r.judge.passed).length;
    const payload = {
      suite: suite.name,
      total,
      passed,
      failed: total - passed,
      pass_rate: total > 0 ? passed / total : 0.0,
      cases: results.map((r) => evalResultToDict(r)),
    };
    return JSON.stringify(payload, null, 2);
  }
}

function formatError(exc: unknown): string {
  if (exc instanceof Error) {
    return `${exc.name}: ${exc.message}`;
  }
  return String(exc);
}

/**
 * Cheap pre-filter — if a required substring is missing we still let the
 * judge decide, but log for easier debugging.
 */
function logMissingExpected(evalCase: EvalCase, turn: EvalTurn, reply: string): void {
  for (const needle of turn.expectedContains ?? []) {
    if (!reply.toLowerCase().includes(needle.toLowerCase())) {
      getLogger().info(
        `case=${JSON.stringify(evalCase.name)} expectedContains=${JSON.stringify(needle)} missing in reply`,
      );
    }
  }
}

/**
 * Load a suite from YAML or JSON.
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
 *
 * Suite files use snake_case keys (shared byte-for-byte with the Python
 * SDK so one suite file drives both); camelCase aliases are also accepted.
 */
export async function loadSuite(path: string): Promise<EvalSuite> {
  const text = await readFile(path, 'utf-8');
  const ext = extname(path).toLowerCase();
  let data: unknown;
  if (ext === '.yaml' || ext === '.yml') {
    let yaml: { parse(src: string): unknown };
    try {
      // Optional dependency (parity with Python's `getpatter[evals]` pyyaml
      // extra) — resolved through a variable specifier so the SDK compiles
      // and ships without it; JSON suites need nothing extra.
      const moduleName = 'yaml';
      yaml = (await import(moduleName)) as { parse(src: string): unknown };
    } catch {
      throw new Error(
        "Loading YAML suites requires the optional 'yaml' package. " +
          'Install with: npm install yaml — or use a JSON suite file.',
      );
    }
    data = yaml.parse(text);
  } else {
    data = JSON.parse(text);
  }

  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error(`Eval suite ${path} must be a mapping, got ${typeOf(data)}`);
  }
  const record = data as Record<string, unknown>;

  const casesRaw = record.cases ?? [];
  if (!Array.isArray(casesRaw)) {
    throw new Error(`Eval suite ${path}: 'cases' must be a list`);
  }

  const cases: EvalCase[] = casesRaw.map((c, i) => {
    if (c === null || typeof c !== 'object' || Array.isArray(c)) {
      throw new Error(`Eval suite ${path}: case ${i} must be a mapping`);
    }
    const caseRecord = c as Record<string, unknown>;
    const turnsRaw = caseRecord.turns ?? [];
    const turns: EvalTurn[] = (Array.isArray(turnsRaw) ? turnsRaw : [])
      .filter((t): t is Record<string, unknown> => t !== null && typeof t === 'object')
      .map((t) => ({
        user: String(t.user ?? ''),
        expectedContains: toStringArray(t.expected_contains ?? t.expectedContains),
      }));
    return {
      name: String(caseRecord.name ?? `case_${i}`),
      turns,
      expectedBehavior: String(caseRecord.expected_behavior ?? caseRecord.expectedBehavior ?? ''),
      rubric: String(caseRecord.rubric ?? ''),
      tags: toStringArray(caseRecord.tags),
      firstMessage: String(caseRecord.first_message ?? caseRecord.firstMessage ?? ''),
    };
  });

  return {
    name: String(record.name ?? basename(path, extname(path))),
    cases,
    metadata: (record.metadata as Record<string, unknown> | undefined) ?? {},
  };
}

function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((v) => String(v));
}

function typeOf(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

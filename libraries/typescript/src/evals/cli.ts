/**
 * Command-line entry point for Patter evals.
 *
 * Invoked as `getpatter eval run suite.yaml` via the main CLI dispatcher
 * (see `src/cli.ts`). Parity with `getpatter.evals.cli` (Python).
 */

import { promises as fs } from 'node:fs';
import { LLMJudge } from './llm-judge';
import { EvalRunner, loadSuite } from './runner';
import type { AgentFactory, AgentReply } from './runner';

interface EvalRunArgs {
  suite: string;
  agent: string;
  judgeModel: string;
  passThreshold: number;
  output: string | null;
}

/**
 * Entry for `getpatter eval ...`. `args` is `process.argv` already sliced past
 * the `eval` command word. Loads the suite, runs it, writes the report, and
 * returns a process exit code (0 all-pass / 1 any-fail / 2 bad usage).
 */
export async function runEval(args: readonly string[]): Promise<number> {
  const sub = args[0];
  if (sub === '--help' || sub === '-h') {
    printEvalHelp();
    return 0;
  }
  if (sub !== 'run') {
    console.log('Usage: getpatter eval run <suite>');
    return 2;
  }

  let parsed: EvalRunArgs;
  try {
    parsed = parseRunArgs(args.slice(1));
  } catch (err) {
    console.error(err instanceof Error ? err.message : String(err));
    return 2;
  }
  if (!parsed.suite) {
    printEvalHelp();
    return 2;
  }

  const suite = await loadSuite(parsed.suite);
  const judge = new LLMJudge({
    model: parsed.judgeModel,
    passThreshold: parsed.passThreshold,
  });
  const runner = new EvalRunner({ judge });

  const factory = await loadAgentFactory(parsed.agent);
  const results = await runner.run(suite, factory);
  const report = runner.report(suite, results);

  if (parsed.output) {
    await fs.writeFile(parsed.output, report, 'utf-8');
    console.log(`Wrote report to ${parsed.output}`);
  } else {
    console.log(report);
  }

  const total = results.length;
  const passed = results.filter((r) => r.judge.passed).length;
  console.error(`\n${passed}/${total} cases passed`);
  // 0 = all passed (an empty suite is vacuously all-pass, matching Python),
  // 1 = at least one case failed. Bad usage returns 2 earlier.
  return passed === total ? 0 : 1;
}

function printEvalHelp(): void {
  console.log(
    'Usage: getpatter eval run <suite> [options]\n\n' +
      'Run an evaluation suite (YAML or JSON) against a scripted agent.\n\n' +
      'Arguments:\n' +
      '  <suite>                 Path to a YAML or JSON suite file\n\n' +
      'Options:\n' +
      '  --agent module:export   Dotted import of an async agent factory\n' +
      '  --judge-model M         Model the LLM judge should use (default gpt-4o-mini)\n' +
      '  --pass-threshold T      Score threshold for pass (default 0.7)\n' +
      '  --output FILE           Write the JSON report to FILE instead of stdout',
  );
}

function parseRunArgs(args: readonly string[]): EvalRunArgs {
  const out: EvalRunArgs = {
    suite: '',
    agent: '',
    judgeModel: 'gpt-4o-mini',
    passThreshold: 0.7,
    output: null,
  };
  const positionals: string[] = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '--agent') {
      out.agent = requireValue(args, ++i, '--agent');
    } else if (a === '--judge-model') {
      out.judgeModel = requireValue(args, ++i, '--judge-model');
    } else if (a === '--pass-threshold') {
      const v = Number(requireValue(args, ++i, '--pass-threshold'));
      if (!Number.isFinite(v)) throw new Error('--pass-threshold must be a number');
      out.passThreshold = v;
    } else if (a === '--output') {
      out.output = requireValue(args, ++i, '--output');
    } else if (a.startsWith('--')) {
      throw new Error(`unknown option ${a}`);
    } else {
      positionals.push(a);
    }
  }
  if (positionals.length > 0) out.suite = positionals[0];
  return out;
}

function requireValue(args: readonly string[], i: number, flag: string): string {
  const v = args[i];
  if (v === undefined) throw new Error(`${flag} requires a value`);
  return v;
}

/**
 * Resolve a `module:export` string to an agent factory.
 *
 * Falls back to an echo-style mock agent when no factory is provided — lets
 * developers sanity-check their suite shape before wiring the real agent.
 */
async function loadAgentFactory(dotted: string): Promise<AgentFactory> {
  if (!dotted) {
    const echo: AgentReply = async (text: string) => `echo: ${text}`;
    return () => echo;
  }
  if (!dotted.includes(':')) {
    throw new Error("--agent must be of the form 'module:export'");
  }
  const idx = dotted.indexOf(':');
  const modulePath = dotted.slice(0, idx);
  const attr = dotted.slice(idx + 1);
  // `modulePath` is developer-supplied CLI config (the operator chooses which
  // agent factory to load), not untrusted network input — same trust model as
  // Python's `importlib.import_module(args.agent)`.
  const mod = (await import(modulePath)) as Record<string, unknown>;
  const obj = mod[attr];
  if (typeof obj !== 'function') {
    throw new Error(`--agent target ${JSON.stringify(dotted)} is not a function`);
  }
  return obj as AgentFactory;
}

/**
 * Unit tests for the Patter evals framework (TypeScript).
 *
 * These tests inject a fake judge backend so every inward code path — prompt
 * building, code-fence stripping, JSON parsing, score clamping, threshold
 * logic, runner orchestration — runs for real with no network and no module
 * mocks. The ONLY external boundary (the OpenAI HTTP endpoint) is covered
 * separately in `evals.mocked.test.ts`. Mirrors `tests/test_evals.py` (Python).
 */

import { promises as fs } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  EvalRunner,
  LLMJudge,
  loadSuite,
  type AgentReply,
  type EvalCase,
  type EvalSuite,
  type JudgeBackend,
  type TranscriptEntry,
} from '../src/index';

/** Test double for the judge backend — returns a canned JSON string. */
class FakeBackend implements JudgeBackend {
  calls = 0;
  constructor(private readonly payload: unknown) {}
  async judge(_prompt: string): Promise<string> {
    this.calls += 1;
    return JSON.stringify(this.payload);
  }
}

const tmpDirs: string[] = [];
async function makeTmpDir(): Promise<string> {
  const dir = await fs.mkdtemp(join(tmpdir(), 'patter-evals-'));
  tmpDirs.push(dir);
  return dir;
}

afterEach(async () => {
  while (tmpDirs.length) {
    const dir = tmpDirs.pop();
    if (dir) await fs.rm(dir, { recursive: true, force: true });
  }
});

// Probe `yaml` once so the YAML test runs for real when installed (CI) and
// skips cleanly when it is not (JSON-only environments).
let yamlAvailable = false;
try {
  await import('yaml');
  yamlAvailable = true;
} catch {
  yamlAvailable = false;
}

describe('[unit] LLMJudge', () => {
  const minimalCase: EvalCase = {
    name: 't',
    turns: [],
    expectedBehavior: '',
    rubric: '',
  };

  it('parses score and reasoning from the judge JSON', async () => {
    const backend = new FakeBackend({ score: 0.9, passed: true, reasoning: 'great' });
    const judge = new LLMJudge({ backend });
    const evalCase: EvalCase = {
      name: 'test',
      turns: [{ user: 'hi' }],
      expectedBehavior: 'reply politely',
      rubric: 'pass if polite',
    };
    const result = await judge.judgeCase(evalCase, [{ role: 'user', text: 'hi' }]);
    expect(result.score).toBeCloseTo(0.9);
    expect(result.passed).toBe(true);
    expect(result.reasoning).toBe('great');
    expect(backend.calls).toBe(1);
  });

  it('tolerates ```code fences``` around the JSON', async () => {
    const fenced =
      '```json\n{"score": 0.5, "passed": false, "reasoning": "meh"}\n```';
    const judge = new LLMJudge({
      backend: { judge: async () => fenced },
    });
    const result = await judge.judgeCase(minimalCase, []);
    expect(result.score).toBeCloseTo(0.5);
    expect(result.passed).toBe(false);
  });

  it('fails safely (score 0, not passed) on invalid JSON', async () => {
    const judge = new LLMJudge({
      backend: { judge: async () => 'not json at all' },
    });
    const result = await judge.judgeCase(minimalCase, []);
    expect(result.score).toBe(0);
    expect(result.passed).toBe(false);
  });

  it('clamps an out-of-range score to the unit interval', async () => {
    const judge = new LLMJudge({
      backend: new FakeBackend({ score: 1.5, passed: true, reasoning: '' }),
    });
    const result = await judge.judgeCase(minimalCase, []);
    expect(result.score).toBe(1);
  });

  it('computes the verdict locally — a hallucinated passed=true with a low score fails', async () => {
    const judge = new LLMJudge({
      passThreshold: 0.7,
      backend: new FakeBackend({ score: 0.2, passed: true, reasoning: 'lies' }),
    });
    const result = await judge.judgeCase(minimalCase, []);
    expect(result.score).toBeCloseTo(0.2);
    expect(result.passed).toBe(false);
  });
});

describe('[unit] EvalRunner', () => {
  it('runs a case end to end and produces a transcript and report', async () => {
    const evalCase: EvalCase = {
      name: 'greeting',
      turns: [{ user: 'hello' }, { user: 'how are you?' }],
      expectedBehavior: 'greet and respond',
      rubric: 'pass if reply is non-empty',
      firstMessage: 'Hi, how can I help?',
    };
    const suite: EvalSuite = { name: 'demo', cases: [evalCase] };
    const judge = new LLMJudge({
      backend: new FakeBackend({ score: 1.0, passed: true, reasoning: 'looks good' }),
    });
    const runner = new EvalRunner({ judge });

    const reply: AgentReply = async (text) => `echo:${text}`;
    const results = await runner.run(suite, () => reply);

    expect(results).toHaveLength(1);
    const result = results[0];
    expect(result.caseName).toBe('greeting');
    expect(result.judge.passed).toBe(true);
    // first_message (agent) + user + agent + user + agent = 5 entries.
    expect(result.transcript).toHaveLength(5);
    expect(result.transcript[0]).toEqual<TranscriptEntry>({
      role: 'agent',
      text: 'Hi, how can I help?',
    });
    expect(result.transcript[1]).toEqual<TranscriptEntry>({
      role: 'user',
      text: 'hello',
    });
    expect(result.transcript[2].role).toBe('agent');
    expect(result.transcript[2].text).toBe('echo:hello');

    const report = JSON.parse(runner.report(suite, results)) as {
      suite: string;
      total: number;
      passed: number;
      pass_rate: number;
    };
    expect(report.suite).toBe('demo');
    expect(report.total).toBe(1);
    expect(report.passed).toBe(1);
    expect(report.pass_rate).toBe(1.0);
  });

  it('handles an agent exception without aborting the suite', async () => {
    const evalCase: EvalCase = {
      name: 'boom',
      turns: [{ user: 'hi' }],
      expectedBehavior: '',
      rubric: '',
    };
    const suite: EvalSuite = { name: 's', cases: [evalCase] };
    const judge = new LLMJudge({
      backend: new FakeBackend({ score: 0, passed: false, reasoning: '' }),
    });
    const runner = new EvalRunner({ judge });

    const broken: AgentReply = async () => {
      throw new Error('agent died');
    };
    const results = await runner.run(suite, () => broken);

    expect(results).toHaveLength(1);
    expect(results[0].error).not.toBeNull();
    expect(results[0].error).toContain('agent died');
    expect(results[0].judge.passed).toBe(false);
  });
});

describe('[unit] loadSuite', () => {
  it('loads a JSON suite', async () => {
    const dir = await makeTmpDir();
    const file = join(dir, 'suite.json');
    await fs.writeFile(
      file,
      JSON.stringify({
        name: 'json-suite',
        cases: [
          {
            name: 't1',
            expected_behavior: 'b',
            rubric: 'r',
            turns: [{ user: 'hey' }],
          },
        ],
      }),
      'utf-8',
    );
    const suite = await loadSuite(file);
    expect(suite.name).toBe('json-suite');
    expect(suite.cases[0].turns[0].user).toBe('hey');
  });

  (yamlAvailable ? it : it.skip)('loads a YAML suite', async () => {
    const dir = await makeTmpDir();
    const file = join(dir, 'suite.yaml');
    await fs.writeFile(
      file,
      [
        'name: test-suite',
        'cases:',
        '  - name: greeting',
        '    expected_behavior: greet',
        '    rubric: pass if greeting',
        '    turns:',
        '      - user: hi',
        '        expected_contains: [hello, hi]',
        '    tags: [smoke]',
        '',
      ].join('\n'),
      'utf-8',
    );
    const suite = await loadSuite(file);
    expect(suite.name).toBe('test-suite');
    expect(suite.cases).toHaveLength(1);
    const evalCase = suite.cases[0];
    expect(evalCase.name).toBe('greeting');
    expect(evalCase.turns[0].user).toBe('hi');
    expect(evalCase.turns[0].expectedContains).toEqual(['hello', 'hi']);
    expect(evalCase.tags).toEqual(['smoke']);
  });

  it('rejects a non-mapping suite', async () => {
    const dir = await makeTmpDir();
    const file = join(dir, 'bad.json');
    await fs.writeFile(file, JSON.stringify(['not', 'a', 'mapping']), 'utf-8');
    await expect(loadSuite(file)).rejects.toThrow(/must be a mapping/);
  });
});

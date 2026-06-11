/**
 * [unit] Patter evals framework — judge parsing, legacy runner, suite loading.
 *
 * Mirrors Python ``tests/test_evals.py``. The judge BACKEND is a canned test
 * double (the judge's only external boundary is a paid OpenAI call);
 * everything else — prompt building, JSON parsing, score clamping, the
 * runner loop, report rendering, suite loading — is real code.
 */
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it, expect } from 'vitest';
import {
  EvalRunner,
  LLMJudge,
  loadSuite,
} from '../../src/evals';
import type { EvalCase, EvalSuite, JudgeBackend } from '../../src/evals';

class FakeBackend implements JudgeBackend {
  calls = 0;
  constructor(private readonly payload: Record<string, unknown>) {}
  async judge(_prompt: string): Promise<string> {
    this.calls += 1;
    return JSON.stringify(this.payload);
  }
}

describe('[unit] LLMJudge', () => {
  it('parses score and reasoning', async () => {
    const judge = new LLMJudge({
      backend: new FakeBackend({ score: 0.9, passed: true, reasoning: 'great' }),
    });
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
  });

  it('tolerates code fences', async () => {
    const fenced = '```json\n{"score": 0.5, "passed": false, "reasoning": "meh"}\n```';
    const judge = new LLMJudge({
      backend: { judge: async () => fenced },
    });
    const evalCase: EvalCase = { name: 't', turns: [], expectedBehavior: '', rubric: '' };
    const result = await judge.judgeCase(evalCase, []);
    expect(result.score).toBeCloseTo(0.5);
    expect(result.passed).toBe(false);
  });

  it('fails safely on invalid JSON', async () => {
    const judge = new LLMJudge({
      backend: { judge: async () => 'not json at all' },
    });
    const evalCase: EvalCase = { name: 't', turns: [], expectedBehavior: '', rubric: '' };
    const result = await judge.judgeCase(evalCase, []);
    expect(result.score).toBe(0.0);
    expect(result.passed).toBe(false);
  });

  it('clamps score to the unit range', async () => {
    const judge = new LLMJudge({
      backend: new FakeBackend({ score: 1.5, passed: true, reasoning: '' }),
    });
    const evalCase: EvalCase = { name: 't', turns: [], expectedBehavior: '', rubric: '' };
    const result = await judge.judgeCase(evalCase, []);
    expect(result.score).toBe(1.0);
  });

  it('computes the verdict locally from the score, not the self-report', async () => {
    // A hallucinated `passed: true` with a failing score must NOT pass.
    const judge = new LLMJudge({
      backend: new FakeBackend({ score: 0.2, passed: true, reasoning: 'hallucinated pass' }),
    });
    const evalCase: EvalCase = { name: 't', turns: [], expectedBehavior: '', rubric: '' };
    const result = await judge.judgeCase(evalCase, []);
    expect(result.passed).toBe(false);
  });
});

describe('[unit] EvalRunner (legacy reply path)', () => {
  it('end-to-end produces transcript and report', async () => {
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

    const reply = async (text: string): Promise<string> => `echo:${text}`;
    const results = await runner.run(suite, () => reply);

    expect(results).toHaveLength(1);
    const result = results[0];
    expect(result.caseName).toBe('greeting');
    expect(result.judge.passed).toBe(true);
    // transcript: firstMessage (agent) + user + agent + user + agent = 5.
    expect(result.transcript).toHaveLength(5);
    expect(result.transcript[0]).toEqual({ role: 'agent', text: 'Hi, how can I help?' });
    expect(result.transcript[1]).toEqual({ role: 'user', text: 'hello' });
    expect(result.transcript[2].role).toBe('agent');
    expect(result.transcript[2].text).toBe('echo:hello');

    // Report should be valid JSON and contain pass-rate.
    const report = JSON.parse(runner.report(suite, results)) as Record<string, unknown>;
    expect(report.suite).toBe('demo');
    expect(report.total).toBe(1);
    expect(report.passed).toBe(1);
    expect(report.pass_rate).toBe(1.0);
  });

  it('handles an agent exception and records the error', async () => {
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

    const broken = async (_: string): Promise<string> => {
      throw new Error('agent died');
    };
    const results = await runner.run(suite, () => broken);

    expect(results).toHaveLength(1);
    expect(results[0].error).not.toBeNull();
    expect(results[0].error).toContain('agent died');
    expect(results[0].judge.passed).toBe(false);
  });

  it('survives a judge failure without aborting the suite', async () => {
    const evalCase: EvalCase = {
      name: 'judge-blip',
      turns: [{ user: 'hi' }],
      expectedBehavior: '',
      rubric: '',
    };
    const judge = new LLMJudge({
      backend: {
        judge: async () => {
          throw new Error('429 too many requests');
        },
      },
    });
    const runner = new EvalRunner({ judge });
    const results = await runner.run(
      { name: 's', cases: [evalCase] },
      () => async (text: string) => `echo:${text}`,
    );
    expect(results[0].judge.passed).toBe(false);
    expect(results[0].error).toContain('judge error');
  });
});

describe('[unit] loadSuite', () => {
  it('loads a JSON suite', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'patter-evals-'));
    const src = join(dir, 'suite.json');
    await writeFile(
      src,
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
    const suite = await loadSuite(src);
    expect(suite.name).toBe('json-suite');
    expect(suite.cases[0].turns[0].user).toBe('hey');
    expect(suite.cases[0].expectedBehavior).toBe('b');
  });

  it('loads a YAML suite (skipped when the optional yaml package is missing)', async (ctx) => {
    const yamlModule = 'yaml';
    const hasYaml = await import(yamlModule).then(
      () => true,
      () => false,
    );
    if (!hasYaml) {
      ctx.skip();
      return;
    }
    const dir = await mkdtemp(join(tmpdir(), 'patter-evals-'));
    const src = join(dir, 'suite.yaml');
    await writeFile(
      src,
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
      ].join('\n'),
      'utf-8',
    );
    const suite = await loadSuite(src);
    expect(suite.name).toBe('test-suite');
    expect(suite.cases).toHaveLength(1);
    const c = suite.cases[0];
    expect(c.name).toBe('greeting');
    expect(c.turns[0].user).toBe('hi');
    expect(c.turns[0].expectedContains).toEqual(['hello', 'hi']);
    expect(c.tags).toEqual(['smoke']);
  });

  it('rejects a non-mapping suite', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'patter-evals-'));
    const src = join(dir, 'bad.json');
    await writeFile(src, JSON.stringify([1, 2, 3]), 'utf-8');
    await expect(loadSuite(src)).rejects.toThrow(/must be a mapping/);
  });
});

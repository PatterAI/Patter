/**
 * [unit] EvalRunner ↔ EvalSession integration (real pipeline, scripted LLM).
 *
 * Mirrors Python ``tests/unit/test_eval_runner_session.py``. Verifies that
 * an ``EvalCase`` carrying ``agent`` / ``llmProvider`` is routed through the
 * real pipeline ``StreamHandler`` harness, while the legacy
 * ``reply()``-factory path keeps working unchanged — including both
 * flavours mixed in one suite.
 */
import { describe, it, expect } from 'vitest';
import {
  EvalRunner,
  LLMJudge,
  ScriptedLLMProvider,
  textTurn,
  toolCallTurn,
} from '../../src/evals';
import type { EvalCase, JudgeBackend } from '../../src/evals';
import { tool } from '../../src/public-api';
import type { AgentOptions } from '../../src/types';

class FakeJudgeBackend implements JudgeBackend {
  readonly prompts: string[] = [];
  constructor(private readonly payload: Record<string, unknown>) {}
  async judge(prompt: string): Promise<string> {
    this.prompts.push(prompt);
    return JSON.stringify(this.payload);
  }
}

function passingJudge(): { judge: LLMJudge; backend: FakeJudgeBackend } {
  const backend = new FakeJudgeBackend({ score: 1.0, passed: true, reasoning: 'ok' });
  return { judge: new LLMJudge({ backend }), backend };
}

describe('[unit] EvalRunner with real-pipeline cases', () => {
  it('drives the real pipeline for a case that carries an agent', async () => {
    const toolRan: Array<Record<string, unknown>> = [];

    const agent: AgentOptions = {
      systemPrompt: 'You are a support agent.',
      provider: 'pipeline',
      tools: [
        tool({
          name: 'cancel_order',
          description: 'Cancel an order',
          parameters: {
            type: 'object',
            properties: { order_id: { type: 'string' } },
          },
          handler: async (args) => {
            toolRan.push(args);
            return JSON.stringify({ cancelled: true });
          },
        }),
      ],
    };
    const provider = new ScriptedLLMProvider([
      toolCallTurn('cancel_order', { order_id: 'A-1' }),
      textTurn('Done — order A-1 is cancelled.'),
    ]);
    const evalCase: EvalCase = {
      name: 'cancels the order',
      turns: [{ user: 'please cancel order A-1' }],
      expectedBehavior: 'Agent cancels the order and confirms.',
      rubric: 'Pass when the cancellation is confirmed.',
      firstMessage: 'Hi, you have reached support.',
      agent,
      llmProvider: provider,
    };
    const { judge, backend } = passingJudge();
    const runner = new EvalRunner({ judge });

    // No agentFactory needed — the case carries its own real agent.
    const results = await runner.run({ name: 's', cases: [evalCase] });

    expect(results).toHaveLength(1);
    const result = results[0];
    expect(result.error).toBeNull();
    expect(result.judge.passed).toBe(true);
    // The REAL pipeline executed the tool through the real executor.
    expect(toolRan).toEqual([{ order_id: 'A-1' }]);
    // Transcript: case.firstMessage (spoken by the REAL start path) +
    // the user turn + what the agent actually said.
    expect(result.transcript[0]).toEqual({
      role: 'agent',
      text: 'Hi, you have reached support.',
    });
    expect(result.transcript[1]).toEqual({
      role: 'user',
      text: 'please cancel order A-1',
    });
    expect(result.transcript[2].role).toBe('agent');
    expect(result.transcript[2].text).toContain('cancelled');
    // The judge saw the real transcript.
    expect(backend.prompts[0]).toContain('order A-1 is cancelled');

    // Report shape is unchanged.
    const report = JSON.parse(
      runner.report({ name: 's', cases: [evalCase] }, results),
    ) as Record<string, unknown>;
    expect(report.passed).toBe(1);
  });

  it('mixes legacy and session cases in one suite', async () => {
    const agent: AgentOptions = { systemPrompt: 'hi', provider: 'pipeline' };
    const sessionCase: EvalCase = {
      name: 'real-pipeline',
      turns: [{ user: 'ping' }],
      expectedBehavior: '',
      rubric: '',
      agent,
      llmProvider: new ScriptedLLMProvider([textTurn('pong from pipeline')]),
    };
    const legacyCase: EvalCase = {
      name: 'legacy-reply',
      turns: [{ user: 'ping' }],
      expectedBehavior: '',
      rubric: '',
    };

    const reply = async (text: string): Promise<string> => `echo:${text}`;

    const { judge } = passingJudge();
    const runner = new EvalRunner({ judge });
    const results = await runner.run(
      { name: 'mixed', cases: [sessionCase, legacyCase] },
      () => reply,
    );

    expect(results.map((r) => r.caseName)).toEqual(['real-pipeline', 'legacy-reply']);
    expect(results[0].transcript[results[0].transcript.length - 1].text).toBe(
      'pong from pipeline',
    );
    expect(results[1].transcript[results[1].transcript.length - 1].text).toBe('echo:ping');
    expect(results.every((r) => r.error === null)).toBe(true);
  });

  it('requires a factory for legacy cases', async () => {
    const legacyCase: EvalCase = {
      name: 'needs-factory',
      turns: [{ user: 'hi' }],
      expectedBehavior: '',
      rubric: '',
    };
    const { judge } = passingJudge();
    const runner = new EvalRunner({ judge });
    const results = await runner.run({ name: 's', cases: [legacyCase] });
    expect(results[0].error).not.toBeNull();
    expect(results[0].error).toContain('agentFactory');
  });

  it('records an error on a mid-case failure but judges the partial transcript', async () => {
    // A failure mid-case keeps the partial transcript and reports the error
    // — same contract as the legacy path.
    const agent: AgentOptions = { systemPrompt: 'hi', provider: 'pipeline' };
    const evalCase: EvalCase = {
      name: 'dup-turn',
      // The second identical turn is dropped by the REAL commit filter,
      // which the session surfaces as an error.
      turns: [{ user: 'same text' }, { user: 'same text' }],
      expectedBehavior: '',
      rubric: '',
      agent,
      llmProvider: new ScriptedLLMProvider([textTurn('first'), textTurn('second')]),
    };
    const { judge } = passingJudge();
    const runner = new EvalRunner({ judge });
    const results = await runner.run({ name: 's', cases: [evalCase] });
    const result = results[0];
    expect(result.error).not.toBeNull();
    expect(result.error).toContain('commit filter');
    // Partial transcript from before the failure is preserved for the judge.
    expect(result.transcript).toContainEqual({ role: 'user', text: 'same text' });
    expect(result.transcript.some((t) => t.role === 'agent' && t.text === 'first')).toBe(
      true,
    );
  });
});

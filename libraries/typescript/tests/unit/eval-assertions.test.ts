/**
 * [unit] Eval assertion helpers (``src/evals/assertions.ts``).
 *
 * Mirrors Python ``tests/unit/test_eval_assertions.py``. These exercise
 * ``expect`` against hand-built ``TurnResult`` objects — the end-to-end
 * pairing with a real session lives in ``eval-session.test.ts``. The
 * ``judge`` helper reuses the real ``LLMJudge`` with an injected fake
 * backend (no network).
 */
import { describe, it, expect } from 'vitest';
import { LLMJudge } from '../../src/evals';
import { expect as expectTurn } from '../../src/evals';
import type { JudgeBackend, ToolCallRecord, TurnResult } from '../../src/evals';

function makeResult(
  overrides: {
    agentText?: string;
    toolCalls?: ToolCallRecord[];
    history?: Array<{ role: string; text: string; timestamp: number }>;
  } = {},
): TurnResult {
  return {
    userText: 'book a table',
    agentText: overrides.agentText ?? 'Booked your table for two.',
    toolCalls: overrides.toolCalls ?? [],
    historySnapshot: overrides.history ?? [],
    interrupted: false,
    metricsTurn: null,
  };
}

class FakeJudgeBackend implements JudgeBackend {
  readonly prompts: string[] = [];
  constructor(private readonly payload: Record<string, unknown>) {}
  async judge(prompt: string): Promise<string> {
    this.prompts.push(prompt);
    return JSON.stringify(this.payload);
  }
}

describe('[unit] eval assertions', () => {
  it('toolCalled passes and chains', () => {
    const record: ToolCallRecord = {
      name: 'book_table',
      arguments: { partySize: 2, time: '20:00' },
      result: '{"ok": true}',
    };
    const result = makeResult({ toolCalls: [record] });
    const chained = expectTurn(result)
      .toolCalled('book_table')
      .toolCalled('book_table', { partySize: 2 })
      .agentTextContains('booked', 'table');
    expect(chained.result).toBe(result);
  });

  it('toolCalled fails with observed tools in the message', () => {
    const result = makeResult({
      toolCalls: [{ name: 'other_tool', arguments: {}, result: null }],
    });
    expect(() => expectTurn(result).toolCalled('book_table')).toThrow(/other_tool/);
  });

  it('toolCalled argsSubset mismatch fails', () => {
    const record: ToolCallRecord = { name: 'book_table', arguments: { partySize: 4 }, result: null };
    expect(() =>
      expectTurn(makeResult({ toolCalls: [record] })).toolCalled('book_table', {
        partySize: 2,
      }),
    ).toThrow(/partySize/);
  });

  it('toolCalled argsSubset is recursive', () => {
    const record: ToolCallRecord = {
      name: 'update',
      arguments: { customer: { id: 'c1', tier: 'gold' }, notify: true },
      result: null,
    };
    expectTurn(makeResult({ toolCalls: [record] })).toolCalled('update', {
      customer: { id: 'c1' },
    });
    expect(() =>
      expectTurn(makeResult({ toolCalls: [record] })).toolCalled('update', {
        customer: { id: 'nope' },
      }),
    ).toThrow();
  });

  it('noToolCalled passes and fails', () => {
    expectTurn(makeResult()).noToolCalled();
    const busy = makeResult({ toolCalls: [{ name: 't', arguments: {}, result: null }] });
    expect(() => expectTurn(busy).noToolCalled()).toThrow(/no tool calls/);
    // Named variant: only the named tool is forbidden.
    expectTurn(busy).noToolCalled('other');
    expect(() => expectTurn(busy).noToolCalled('t')).toThrow();
  });

  it('agentTextContains handles case sensitivity', () => {
    const result = makeResult({ agentText: 'Your Table is Booked.' });
    expectTurn(result).agentTextContains('table is booked');
    expect(() =>
      expectTurn(result).agentTextContains(['table is booked'], { caseSensitive: true }),
    ).toThrow(/missing/);
  });

  it('judge passes and returns the verdict', async () => {
    const backend = new FakeJudgeBackend({
      score: 0.9,
      passed: true,
      reasoning: 'confirms the booking',
    });
    const judge = new LLMJudge({ backend });
    const history = [
      { role: 'user', text: 'book a table', timestamp: 1 },
      { role: 'assistant', text: 'Booked your table for two.', timestamp: 2 },
    ];
    const verdict = await expectTurn(makeResult({ history })).judge(judge, {
      intent: 'The agent confirms the booking.',
    });
    expect(verdict.passed).toBe(true);
    expect(verdict.score).toBeCloseTo(0.9);
    // The judge saw the turn transcript with assistant mapped to 'agent'.
    expect(backend.prompts[0]).toContain('agent: Booked your table for two.');
    expect(backend.prompts[0]).toContain('The agent confirms the booking.');
  });

  it('judge failure raises with the reasoning in the message', async () => {
    const backend = new FakeJudgeBackend({
      score: 0.2,
      passed: false,
      reasoning: 'never confirmed',
    });
    const judge = new LLMJudge({ backend });
    await expect(
      expectTurn(makeResult()).judge(judge, { intent: 'Booking is confirmed.' }),
    ).rejects.toThrow(/never confirmed/);
  });
});

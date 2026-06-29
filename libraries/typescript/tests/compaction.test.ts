import { describe, it, expect } from 'vitest';
import {
  estimateTokens,
  estimateMessagesTokens,
  ContextCompactor,
} from '../src/compaction';
import { LLMLoop } from '../src/llm-loop';
import { createHistoryManager } from '../src/handler-utils';
import type { CompactionConfig } from '../src/types';

function makeTurns(n: number, prefix = 'msg'): Array<{ role: string; text: string }> {
  const out: Array<{ role: string; text: string }> = [];
  for (let i = 0; i < n; i++) {
    out.push({ role: i % 2 === 0 ? 'user' : 'assistant', text: `${prefix} ${i} ${'x'.repeat(60)}` });
  }
  return out;
}

describe('[unit] token estimation', () => {
  it('estimateTokens uses chars/4 baseline', () => {
    expect(estimateTokens('')).toBe(0);
    expect(estimateTokens(undefined)).toBe(0);
    expect(estimateTokens(null)).toBe(0);
    expect(estimateTokens('a'.repeat(40))).toBe(10);
    expect(estimateTokens('hi')).toBe(1); // short non-empty → at least 1
  });

  it('estimateMessagesTokens handles content and text keys with overhead', () => {
    const total = estimateMessagesTokens([
      { content: 'a'.repeat(40) }, // 10 + 4
      { text: 'b'.repeat(80) }, // 20 + 4
    ]);
    expect(total).toBe(10 + 20 + 4 * 2);
  });
});

describe('[unit] ContextCompactor.shouldCompact', () => {
  const noopSummarizer = async () => {
    throw new Error('should not run');
  };

  it('is false when history is short', () => {
    const c = new ContextCompactor({ triggerTokens: 8000, keepLastTurns: 4 }, noopSummarizer);
    expect(c.shouldCompact(makeTurns(4))).toBe(false);
  });

  it('is true once the summarizable portion crosses the trigger', () => {
    // keepLastTurns=2 keeps last 4 entries; the rest must exceed triggerTokens.
    const c = new ContextCompactor({ triggerTokens: 50, keepLastTurns: 2 }, noopSummarizer);
    expect(c.shouldCompact(makeTurns(20))).toBe(true);
  });

  it('applies default thresholds when omitted', () => {
    const c = new ContextCompactor({} as CompactionConfig, noopSummarizer);
    // Default triggerTokens=8000 — 20 short turns stay well under it.
    expect(c.shouldCompact(makeTurns(20))).toBe(false);
  });
});

describe('[unit] compaction OFF — prompt assembly unchanged', () => {
  it('buildMessages has no summary message when none is set', () => {
    const loop = new LLMLoop('', 'gpt-4o-mini', 'You are an agent.', undefined, undefined, true);
    const messages = (
      loop as unknown as {
        buildMessages: (h: Array<{ role: string; text: string }>, t: string) => Array<{ role: string; content: string }>;
      }
    ).buildMessages([{ role: 'user', text: 'hello' }], 'now');
    expect(messages[0]).toEqual({ role: 'system', content: 'You are an agent.' });
    expect(messages[1]).toEqual({ role: 'user', content: 'hello' });
    expect(messages.every((m) => !m.content.includes('Summary of earlier conversation'))).toBe(true);
  });
});

describe('[unit] summary prepended into prompt (fact recoverable)', () => {
  it('setContextSummary inserts a system message after the main prompt', () => {
    const loop = new LLMLoop('', 'gpt-4o-mini', 'agent', undefined, undefined, true);
    loop.setContextSummary("Caller's account number is 12345; wants a refund.");
    const messages = (
      loop as unknown as {
        buildMessages: (h: Array<{ role: string; text: string }>, t: string) => Array<{ role: string; content: string }>;
      }
    ).buildMessages([{ role: 'user', text: 'and my balance?' }], 'hi');
    expect(messages[0].role).toBe('system');
    expect(messages[1].role).toBe('system');
    expect(messages[1].content).toContain('12345');
    expect(messages[1].content).toContain('Summary of earlier conversation');
    expect(messages[messages.length - 1]).toEqual({ role: 'user', content: 'hi' });
  });
});

describe('[unit] history ring honours maxHistory', () => {
  it('createHistoryManager evicts past the configured cap', () => {
    const h = createHistoryManager(7);
    for (let i = 0; i < 20; i++) h.push({ role: 'user', text: String(i), timestamp: 0 });
    expect(h.entries.length).toBe(7);
    expect(h.entries[0].text).toBe('13');
  });
});

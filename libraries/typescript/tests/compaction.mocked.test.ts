import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ContextCompactor } from '../src/compaction';
import { LLMLoop } from '../src/llm-loop';
import type { LLMProvider, LLMChunk } from '../src/llm-loop';
import { CallMetricsAccumulator } from '../src/metrics';
import { getLogger, setLogger, type Logger } from '../src/logger';
import type { CompactionConfig } from '../src/types';

/**
 * A real in-process LLM provider yielding deterministic chunks. Stands in for
 * the paid OpenAI streaming boundary (hence the `.mocked` filename); everything
 * downstream (message assembly, token counting, metrics) runs real code.
 */
class LocalProvider implements LLMProvider {
  static providerKey = 'openai';
  seenMessages: Array<Array<Record<string, unknown>>> = [];
  constructor(private reply = 'Understood.', private emitUsage = true) {}
  async *stream(messages: Array<Record<string, unknown>>): AsyncGenerator<LLMChunk, void, unknown> {
    this.seenMessages.push([...messages]);
    yield { type: 'text', content: this.reply };
    if (this.emitUsage) yield { type: 'usage', inputTokens: 5, outputTokens: 3 };
  }
}

async function drain(gen: AsyncGenerator<string, void, unknown>): Promise<string> {
  let out = '';
  for await (const tok of gen) out += tok;
  return out;
}

function makeTurns(n: number, prefix = 'msg'): Array<{ role: string; text: string }> {
  const out: Array<{ role: string; text: string }> = [];
  for (let i = 0; i < n; i++) {
    out.push({ role: i % 2 === 0 ? 'user' : 'assistant', text: `${prefix} ${i} ${'x'.repeat(60)}` });
  }
  return out;
}

describe('[mocked] compaction summarizes old, keeps last verbatim, preserves fact', () => {
  it('prunes summarized turns and a fact stated early survives in the summary', async () => {
    // A deterministic local summarizer standing in for the agent LLM: it folds
    // the prior summary plus the old turns' text into a new summary, so every
    // fact in the summarized turns survives — what a good LLM summary does, but
    // reproducibly.
    const summarizer = async (prior: string, old: Array<{ role: string; text: string }>) => {
      const body = old.map((m) => m.text).join(' | ');
      return (prior ? prior + ' || ' : '') + body;
    };
    const cfg: CompactionConfig = { triggerTokens: 50, targetTokens: 100, keepLastTurns: 2 };
    const compactor = new ContextCompactor(cfg, summarizer);

    const history: Array<{ role: string; text: string }> = [
      { role: 'user', text: 'My account number is 12345.' },
      { role: 'assistant', text: 'Thanks, noted.' },
      ...makeTurns(8, 'later'),
      { role: 'user', text: 'VERBATIM user line' },
      { role: 'assistant', text: 'VERBATIM assistant line' },
    ];
    const preLen = history.length;
    expect(compactor.shouldCompact(history)).toBe(true);

    const newSummary = await compactor.maybeCompact(history);

    expect(newSummary).not.toBeNull();
    expect(newSummary).toContain('12345');
    expect(compactor.summary).toBe(newSummary);

    const remaining = history.map((e) => e.text);
    expect(remaining).toContain('VERBATIM user line');
    expect(remaining).toContain('VERBATIM assistant line');
    expect(remaining).not.toContain('My account number is 12345.'); // pruned
    expect(history.length).toBeLessThan(preLen);
    expect(history.length).toBe(cfg.keepLastTurns! * 2);
  });

  it('does not prune when the summarizer returns nothing usable', async () => {
    const empty = async () => '   ';
    const compactor = new ContextCompactor({ triggerTokens: 20, keepLastTurns: 2 }, empty);
    const history = makeTurns(10);
    const preLen = history.length;
    const result = await compactor.maybeCompact(history);
    expect(result).toBeNull();
    expect(history.length).toBe(preLen);
    expect(compactor.summary).toBe('');
  });
});

describe('[mocked] summary flows into the assembled LLM prompt', () => {
  it('a compaction summary becomes a system message in the next prompt', async () => {
    const provider = new LocalProvider('ok');
    const loop = new LLMLoop('', 'gpt-4o-mini', 'agent', undefined, provider, true);
    loop.setContextSummary("Caller's account number is 12345; wants a refund.");
    await drain(loop.run('and my balance?', [{ role: 'user', text: 'earlier' }], { call_id: 'c1' }));
    const sent = provider.seenMessages[0];
    expect(sent[0]).toMatchObject({ role: 'system', content: 'agent' });
    expect(sent[1]).toMatchObject({ role: 'system' });
    expect(String(sent[1].content)).toContain('12345');
  });
});

describe('[mocked] context_tokens metric + budget warning', () => {
  it('records a growing context_tokens peak', async () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c1',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      llmProvider: 'openai',
    });
    const loop = new LLMLoop('', 'gpt-4o-mini', 'agent', undefined, new LocalProvider(), true);

    await drain(loop.run('hi', [], { call_id: 'c1' }, acc));
    const small = acc.maxContextTokens;
    expect(small).toBeGreaterThan(0);

    await drain(loop.run('hi again', makeTurns(30), { call_id: 'c1' }, acc));
    expect(acc.maxContextTokens).toBeGreaterThan(small);
  });

  it('surfaces context_tokens on the final CallMetrics', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c2',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
    });
    acc.recordContextTokens(120);
    acc.recordContextTokens(80);
    acc.recordContextTokens(300);
    expect(acc.maxContextTokens).toBe(300);
    expect(acc.endCall().context_tokens).toBe(300);
  });
});

describe('[mocked] budget warning fires once per call', () => {
  let warnings: string[];
  let prev: Logger;

  beforeEach(() => {
    warnings = [];
    prev = getLogger();
    setLogger({
      info: () => {},
      debug: () => {},
      error: () => {},
      warn: (msg: string) => warnings.push(msg),
    });
  });
  afterEach(() => setLogger(prev));

  it('logs exactly one context_tokens_high warning when over budget', async () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c3',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      llmProvider: 'openai',
    });
    // Tiny budget → any real prompt crosses 75%.
    const loop = new LLMLoop('', 'gpt-4o-mini', 'agent', undefined, new LocalProvider(), true, 10);
    await drain(loop.run('a normal sentence that is long enough', [], { call_id: 'c3' }, acc));
    await drain(loop.run('another turn', makeTurns(4), { call_id: 'c3' }, acc));
    const hits = warnings.filter((w) => w.includes('context_tokens_high'));
    expect(hits.length).toBe(1);
  });

  it('does not warn without a budget but still records the metric', async () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c4',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      llmProvider: 'openai',
    });
    const loop = new LLMLoop('', 'gpt-4o-mini', 'agent', undefined, new LocalProvider(), true);
    await drain(loop.run('hello there', makeTurns(20), { call_id: 'c4' }, acc));
    expect(warnings.filter((w) => w.includes('context_tokens_high')).length).toBe(0);
    expect(acc.maxContextTokens).toBeGreaterThan(0);
  });
});

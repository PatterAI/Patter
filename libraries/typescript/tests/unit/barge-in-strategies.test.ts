import { describe, it, expect } from 'vitest';
import {
  MinWordsStrategy,
  evaluateStrategies,
  resetStrategies,
  type BargeInStrategy,
  type EvaluateContext,
} from '../../src/services/barge-in-strategies';

describe('MinWordsStrategy', () => {
  it('rejects minWords < 1 in the constructor', () => {
    expect(() => new MinWordsStrategy({ minWords: 0 })).toThrow();
    expect(() => new MinWordsStrategy({ minWords: -3 })).toThrow();
    expect(() => new MinWordsStrategy({ minWords: NaN })).toThrow();
  });

  it('lets a single word confirm when the agent is silent', () => {
    const s = new MinWordsStrategy({ minWords: 3 });
    expect(
      s.evaluate({ transcript: 'hi', isInterim: false, agentSpeaking: false }),
    ).toBe(true);
  });

  it('does not confirm sub-threshold transcripts during agent speech', () => {
    const s = new MinWordsStrategy({ minWords: 3 });
    expect(
      s.evaluate({ transcript: 'okay', isInterim: false, agentSpeaking: true }),
    ).toBe(false);
    expect(
      s.evaluate({
        transcript: 'uh huh',
        isInterim: false,
        agentSpeaking: true,
      }),
    ).toBe(false);
  });

  it('confirms once the threshold is met during agent speech', () => {
    const s = new MinWordsStrategy({ minWords: 3 });
    expect(
      s.evaluate({
        transcript: 'please stop talking',
        isInterim: false,
        agentSpeaking: true,
      }),
    ).toBe(true);
    expect(
      s.evaluate({
        transcript: 'hold on a moment please',
        isInterim: false,
        agentSpeaking: true,
      }),
    ).toBe(true);
  });

  it('useInterim=false ignores interim partials but still accepts finals', () => {
    const s = new MinWordsStrategy({ minWords: 2, useInterim: false });
    expect(
      s.evaluate({
        transcript: 'please stop',
        isInterim: true,
        agentSpeaking: true,
      }),
    ).toBe(false);
    expect(
      s.evaluate({
        transcript: 'please stop',
        isInterim: false,
        agentSpeaking: true,
      }),
    ).toBe(true);
  });

  it('counts words via whitespace split (collapses runs and tabs)', () => {
    const s = new MinWordsStrategy({ minWords: 2 });
    expect(
      s.evaluate({
        transcript: '   hello   world   ',
        isInterim: false,
        agentSpeaking: true,
      }),
    ).toBe(true);
    expect(
      s.evaluate({
        transcript: '\thello\n',
        isInterim: false,
        agentSpeaking: true,
      }),
    ).toBe(false);
  });

  it('does not confirm an empty transcript during agent speech', () => {
    const s = new MinWordsStrategy({ minWords: 2 });
    expect(
      s.evaluate({ transcript: '', isInterim: false, agentSpeaking: true }),
    ).toBe(false);
  });
});

class RecordingStrategy implements BargeInStrategy {
  calls: EvaluateContext[] = [];
  resets = 0;
  constructor(private readonly returns: boolean) {}
  evaluate(ctx: EvaluateContext): boolean {
    this.calls.push(ctx);
    return this.returns;
  }
  async reset(): Promise<void> {
    this.resets += 1;
  }
}

describe('evaluateStrategies', () => {
  const baseCtx: EvaluateContext = {
    transcript: 'please stop',
    isInterim: false,
    agentSpeaking: true,
  };

  it('returns false on empty array', async () => {
    expect(await evaluateStrategies([], baseCtx)).toBe(false);
  });

  it('short-circuits at the first true', async () => {
    const a = new RecordingStrategy(true);
    const b = new RecordingStrategy(false);
    const result = await evaluateStrategies([a, b], baseCtx);
    expect(result).toBe(true);
    expect(a.calls).toHaveLength(1);
    expect(b.calls).toHaveLength(0);
  });

  it('returns false when every strategy returns false', async () => {
    const a = new RecordingStrategy(false);
    const b = new RecordingStrategy(false);
    const result = await evaluateStrategies([a, b], baseCtx);
    expect(result).toBe(false);
    expect(a.calls).toHaveLength(1);
    expect(b.calls).toHaveLength(1);
  });

  it('swallows a strategy that throws and continues to the next', async () => {
    const boom: BargeInStrategy = {
      evaluate() {
        throw new Error('boom');
      },
    };
    const ok = new RecordingStrategy(true);
    const result = await evaluateStrategies([boom, ok], baseCtx);
    expect(result).toBe(true);
    expect(ok.calls).toHaveLength(1);
  });

  it('coerces null/undefined transcript to empty string before passing through', async () => {
    const seen: EvaluateContext[] = [];
    const recorder: BargeInStrategy = {
      evaluate(ctx) {
        seen.push(ctx);
        return false;
      },
    };
    await evaluateStrategies([recorder], {
      transcript: undefined as unknown as string,
      isInterim: false,
      agentSpeaking: true,
    });
    expect(seen[0]?.transcript).toBe('');
  });
});

describe('resetStrategies', () => {
  it('resets each strategy that exposes a reset() method', async () => {
    const a = new RecordingStrategy(false);
    const b = new RecordingStrategy(false);
    await resetStrategies([a, b]);
    expect(a.resets).toBe(1);
    expect(b.resets).toBe(1);
  });

  it('skips strategies that do not implement reset()', async () => {
    const noReset: BargeInStrategy = { evaluate: () => false };
    const a = new RecordingStrategy(false);
    await resetStrategies([noReset, a]);
    expect(a.resets).toBe(1);
  });

  it('swallows per-strategy reset errors', async () => {
    const boom: BargeInStrategy = {
      evaluate: () => false,
      async reset() {
        throw new Error('boom');
      },
    };
    const ok = new RecordingStrategy(false);
    await resetStrategies([boom, ok]);
    expect(ok.resets).toBe(1);
  });
});

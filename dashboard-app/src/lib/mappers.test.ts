import { describe, expect, it } from 'vitest';
import type { CallRecord } from './api';
import { toUiTranscript } from './mappers';

function record(overrides: Partial<CallRecord> = {}): CallRecord {
  return {
    call_id: 'c1',
    caller: '+1111',
    callee: '+2222',
    direction: 'inbound',
    started_at: 1000,
    status: 'in-progress',
    transcript: [],
    turns: [],
    metrics: null,
    ...overrides,
  };
}

describe('toUiTranscript — FIX-5 (turnIndex, role) ordering (issue #154)', () => {
  it('places a late-arriving user line ABOVE its agent line within the same turn', () => {
    // Realtime ordering: the assistant line was appended live first (the model
    // replied before the slower Whisper user transcript landed), so the raw
    // array is [assistant, user] for turn 0.
    const rec = record({
      transcript: [
        { role: 'assistant', text: 'Hi, how can I help?', timestamp: 2, turnIndex: 0 },
        { role: 'user', text: 'Book a table', timestamp: 3, turnIndex: 0 },
      ],
    });
    const turns = toUiTranscript(rec);
    expect(turns.map((t) => `${t.who}:${t.txt}`)).toEqual([
      'user:Book a table',
      'bot:Hi, how can I help?',
    ]);
  });

  it('orders multiple turns by turnIndex, user before bot within each', () => {
    const rec = record({
      transcript: [
        { role: 'assistant', text: 'A1', timestamp: 2, turnIndex: 0 },
        { role: 'user', text: 'U0', timestamp: 3, turnIndex: 0 },
        { role: 'assistant', text: 'A2', timestamp: 4, turnIndex: 1 },
        { role: 'user', text: 'U1', timestamp: 5, turnIndex: 1 },
      ],
    });
    expect(toUiTranscript(rec).map((t) => `${t.who}:${t.txt}`)).toEqual([
      'user:U0',
      'bot:A1',
      'user:U1',
      'bot:A2',
    ]);
  });

  it('preserves original order for legacy entries without turnIndex (stable)', () => {
    const rec = record({
      transcript: [
        { role: 'user', text: 'first', timestamp: 1 },
        { role: 'assistant', text: 'second', timestamp: 2 },
        { role: 'user', text: 'third', timestamp: 3 },
      ],
    });
    expect(toUiTranscript(rec).map((t) => t.txt)).toEqual(['first', 'second', 'third']);
  });
});

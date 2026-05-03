import { describe, it, expect } from 'vitest';
import { callsToCsv, callsToJson } from '../src/dashboard/export';
import type { CallRecord } from '../src/dashboard/store';

const SAMPLE_CALLS: CallRecord[] = [
  {
    call_id: 'c1',
    caller: '+1111',
    callee: '+2222',
    direction: 'inbound',
    started_at: 1700000000,
    ended_at: 1700000060,
    metrics: {
      duration_seconds: 60,
      cost: { total: 0.05, stt: 0.01, tts: 0.02, llm: 0.01, telephony: 0.01 },
      latency_avg: { total_ms: 450 },
      turns: [{ turn_index: 0 }, { turn_index: 1 }],
      provider_mode: 'pipeline',
    },
  },
];

describe('callsToCsv', () => {
  it('produces valid CSV with headers', () => {
    const csv = callsToCsv(SAMPLE_CALLS);
    const lines = csv.trim().split('\n');
    expect(lines).toHaveLength(2); // header + 1 data row
    expect(lines[0]).toContain('call_id');
    expect(lines[0]).toContain('cost_total');
    expect(lines[1]).toContain('c1');
    expect(lines[1]).toContain('pipeline');
  });

  it('handles empty call list', () => {
    const csv = callsToCsv([]);
    const lines = csv.trim().split('\n');
    expect(lines).toHaveLength(1); // header only
  });

  it('handles calls without metrics', () => {
    const csv = callsToCsv([{
      call_id: 'c2',
      caller: '+3333',
      callee: '+4444',
      direction: 'outbound',
      started_at: 0,
      metrics: null,
    }]);
    const lines = csv.trim().split('\n');
    expect(lines).toHaveLength(2);
    expect(lines[1]).toContain('c2');
  });
});

describe('callsToJson', () => {
  it('produces valid JSON', () => {
    const json = callsToJson(SAMPLE_CALLS);
    const parsed = JSON.parse(json);
    expect(parsed).toHaveLength(1);
    expect(parsed[0].call_id).toBe('c1');
  });

  it('handles empty list', () => {
    expect(callsToJson([])).toBe('[]');
  });
});

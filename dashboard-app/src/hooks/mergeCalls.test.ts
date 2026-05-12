import { describe, expect, it } from 'vitest';
import type { CallRecord } from '../lib/api';
import type { Call } from '../lib/mappers';
import { mergeCallPreserving, mergeCalls } from './mergeCalls';

function record(callId: string, overrides: Partial<CallRecord> = {}): CallRecord {
  return {
    call_id: callId,
    caller: `from-${callId}`,
    callee: `to-${callId}`,
    direction: 'inbound',
    started_at: 1000,
    status: 'in-progress',
    transcript: [],
    turns: [],
    metrics: null,
    ...overrides,
  };
}

function makeCall(id: string, overrides: Partial<Call> = {}): Call {
  return {
    id,
    status: 'live',
    direction: 'inbound',
    from: `from-${id}`,
    to: `to-${id}`,
    carrier: 'twilio',
    cost: {},
    ...overrides,
  };
}

describe('mergeCalls', () => {
  it('returns active before recent and dedupes by call_id', () => {
    const active = [record('a', { status: 'in-progress' })];
    const recent = [record('b', { status: 'completed', ended_at: 1100 })];
    const result = mergeCalls(active, recent);
    expect(result.map((c) => c.id)).toEqual(['a', 'b']);
  });

  it('active wins over recent when call_id appears in both', () => {
    const active = [record('a', { status: 'in-progress', caller: 'live' })];
    const recent = [record('a', { status: 'completed', caller: 'stale' })];
    const result = mergeCalls(active, recent);
    expect(result).toHaveLength(1);
    expect(result[0].status).toBe('live');
    expect(result[0].from).toBe('live');
  });
});

describe('mergeCallPreserving', () => {
  it('regression #124: a second call_start refresh keeps the first call visible', () => {
    // Step 1: call A is live, no recent.
    const stateAfterAStart = mergeCallPreserving(
      [],
      mergeCalls([record('A', { status: 'in-progress' })], []),
    );
    expect(stateAfterAStart.map((c) => c.id)).toEqual(['A']);

    // Step 2: call A ended, snapshot still includes it via /calls.
    const stateAfterAEnd = mergeCallPreserving(
      stateAfterAStart,
      mergeCalls(
        [],
        [record('A', { status: 'completed', ended_at: 1100 })],
      ),
    );
    expect(stateAfterAEnd.map((c) => c.id)).toEqual(['A']);
    expect(stateAfterAEnd[0].status).toBe('ended');

    // Step 3: call B starts. The server SSE for call_start fires the refresh
    // BEFORE the prior call A propagates to /api/dashboard/calls — simulate
    // by having only B in the snapshot. Without the upsert, A would vanish.
    const stateAfterBStart = mergeCallPreserving(
      stateAfterAEnd,
      mergeCalls([record('B', { status: 'in-progress' })], []),
    );
    expect(stateAfterBStart.map((c) => c.id).sort()).toEqual(['A', 'B']);
  });

  it('upserts: next replaces prev for same id but unknown prev calls are kept', () => {
    const prev: Call[] = [
      makeCall('A', { status: 'live', latencyP95: 250 }),
      makeCall('B', { status: 'ended' }),
    ];
    const next: Call[] = [makeCall('A', { status: 'ended', latencyP95: 280 })];
    const result = mergeCallPreserving(prev, next);
    const a = result.find((c) => c.id === 'A')!;
    const b = result.find((c) => c.id === 'B')!;
    expect(a.status).toBe('ended');
    expect(a.latencyP95).toBe(280);
    expect(b.status).toBe('ended');
  });

  it('preserves rich fields the fresh payload omits', () => {
    const prev: Call[] = [
      makeCall('A', {
        latencyP95: 250,
        latencyP50: 180,
        sttAvg: 90,
        ttsAvg: 110,
        llmAvg: 320,
        turnCount: 7,
        agentResponseP50: 420,
        agentResponseP95: 980,
        cost: { llm: 0.01, stt: 0.002 },
      }),
    ];
    const next: Call[] = [
      makeCall('A', {
        status: 'ended',
        cost: { llm: 0.012 },
      }),
    ];
    const merged = mergeCallPreserving(prev, next);
    const a = merged[0];
    expect(a.status).toBe('ended');
    expect(a.latencyP95).toBe(250);
    expect(a.latencyP50).toBe(180);
    expect(a.sttAvg).toBe(90);
    expect(a.ttsAvg).toBe(110);
    expect(a.llmAvg).toBe(320);
    expect(a.turnCount).toBe(7);
    expect(a.agentResponseP50).toBe(420);
    expect(a.agentResponseP95).toBe(980);
    expect(a.cost.llm).toBe(0.012);
    expect(a.cost.stt).toBe(0.002);
  });

  it('two consecutive call_start SSE events for different ids end up with both visible', () => {
    let state: Call[] = [];
    state = mergeCallPreserving(state, mergeCalls([record('one')], []));
    expect(state.map((c) => c.id)).toEqual(['one']);
    state = mergeCallPreserving(state, mergeCalls([record('two')], []));
    expect(state.map((c) => c.id).sort()).toEqual(['one', 'two']);
  });
});

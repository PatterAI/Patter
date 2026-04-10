import { describe, it, expect, vi } from 'vitest';
import { MetricsStore } from '../src/dashboard/store';

describe('MetricsStore', () => {
  it('records call start and tracks active calls', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1111', callee: '+2222', direction: 'inbound' });

    const active = store.getActiveCalls();
    expect(active).toHaveLength(1);
    expect(active[0].call_id).toBe('c1');
    expect(active[0].caller).toBe('+1111');
  });

  it('records call end and moves to completed', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1111', callee: '+2222' });
    store.recordCallEnd({ call_id: 'c1', transcript: [] }, { cost: { total: 0.05 } });

    expect(store.getActiveCalls()).toHaveLength(0);
    const calls = store.getCalls();
    expect(calls).toHaveLength(1);
    expect(calls[0].call_id).toBe('c1');
    expect(calls[0].metrics).toEqual({ cost: { total: 0.05 } });
  });

  it('records turns on active calls', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1111', callee: '+2222' });
    store.recordTurn({ call_id: 'c1', turn: { turn_index: 0, user_text: 'Hello' } });

    const active = store.getActiveCalls();
    expect(active[0].turns).toHaveLength(1);
  });

  it('enforces max calls limit', () => {
    const store = new MetricsStore(3);
    for (let i = 0; i < 5; i++) {
      store.recordCallStart({ call_id: `c${i}` });
      store.recordCallEnd({ call_id: `c${i}` });
    }
    expect(store.callCount).toBe(3);
  });

  it('getCalls returns newest first', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' });
    store.recordCallStart({ call_id: 'c2' });
    store.recordCallEnd({ call_id: 'c2' });

    const calls = store.getCalls();
    expect(calls[0].call_id).toBe('c2');
    expect(calls[1].call_id).toBe('c1');
  });

  it('getCall returns specific call', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' });

    expect(store.getCall('c1')).not.toBeNull();
    expect(store.getCall('nonexistent')).toBeNull();
  });

  it('computes aggregates', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' }, {
      cost: { total: 0.10, stt: 0.01, tts: 0.05, llm: 0.02, telephony: 0.02 },
      duration_seconds: 60,
      latency_avg: { total_ms: 500 },
    } as Record<string, unknown>);

    const agg = store.getAggregates();
    expect(agg.total_calls).toBe(1);
    expect(agg.total_cost).toBe(0.1);
    expect(agg.avg_duration).toBe(60);
    expect(agg.avg_latency_ms).toBe(500);
  });

  it('returns empty aggregates for no calls', () => {
    const store = new MetricsStore();
    const agg = store.getAggregates();
    expect(agg.total_calls).toBe(0);
    expect(agg.total_cost).toBe(0);
  });

  it('emits SSE events', () => {
    const store = new MetricsStore();
    const events: unknown[] = [];
    store.on('sse', (event) => events.push(event));

    store.recordCallStart({ call_id: 'c1', caller: '+1' });
    store.recordTurn({ call_id: 'c1', turn: { text: 'hello' } });
    store.recordCallEnd({ call_id: 'c1' });

    expect(events).toHaveLength(3);
    expect((events[0] as { type: string }).type).toBe('call_start');
    expect((events[1] as { type: string }).type).toBe('turn_complete');
    expect((events[2] as { type: string }).type).toBe('call_end');
  });

  it('filters calls by time range', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' });

    const now = Date.now() / 1000;
    const result = store.getCallsInRange(now - 10, now + 10);
    expect(result).toHaveLength(1);

    const noResult = store.getCallsInRange(now + 100, now + 200);
    expect(noResult).toHaveLength(0);
  });

  it('getCallsInRange returns all calls when no bounds given', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' });
    store.recordCallStart({ call_id: 'c2' });
    store.recordCallEnd({ call_id: 'c2' });

    const result = store.getCallsInRange();
    expect(result).toHaveLength(2);
  });

  it('SSE subscriber receives correct event data', () => {
    const store = new MetricsStore();
    const events: Array<{ type: string; data: Record<string, unknown> }> = [];
    store.on('sse', (event) => events.push(event));

    store.recordCallStart({ call_id: 'x1', caller: '+100', callee: '+200', direction: 'outbound' });

    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('call_start');
    expect(events[0].data.caller).toBe('+100');
    expect(events[0].data.direction).toBe('outbound');
  });

  it('max calls cap keeps only the newest entries', () => {
    const store = new MetricsStore(2);
    for (let i = 0; i < 4; i++) {
      store.recordCallStart({ call_id: `c${i}` });
      store.recordCallEnd({ call_id: `c${i}` });
    }

    expect(store.callCount).toBe(2);
    const calls = store.getCalls();
    expect(calls[0].call_id).toBe('c3');
    expect(calls[1].call_id).toBe('c2');
  });

  it('ignores recordCallStart with empty call_id', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: '' });
    expect(store.getActiveCalls()).toHaveLength(0);
  });

  it('ignores recordTurn with missing turn data', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordTurn({ call_id: 'c1' }); // no turn field
    const active = store.getActiveCalls();
    expect(active[0].turns).toHaveLength(0);
  });
});

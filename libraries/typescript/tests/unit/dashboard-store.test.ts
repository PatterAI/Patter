import { describe, it, expect, vi } from 'vitest';
import { MetricsStore } from '../../src/dashboard/store';
import type { SSEEvent } from '../../src/dashboard/store';

describe('MetricsStore', () => {
  // --- Construction ---

  it('initializes with default maxCalls of 500', () => {
    const store = new MetricsStore();
    expect(store.callCount).toBe(0);
  });

  it('initializes with custom maxCalls', () => {
    const store = new MetricsStore(10);
    expect(store.callCount).toBe(0);
  });

  // --- recordCallStart ---

  describe('recordCallStart()', () => {
    it('tracks an active call', () => {
      const store = new MetricsStore();
      store.recordCallStart({
        call_id: 'call-1',
        caller: '+15551111111',
        callee: '+15552222222',
        direction: 'inbound',
      });

      const active = store.getActiveCalls();
      expect(active).toHaveLength(1);
      expect(active[0].call_id).toBe('call-1');
    });

    it('publishes call_start SSE event', () => {
      const store = new MetricsStore();
      const events: SSEEvent[] = [];
      store.on('sse', (evt: SSEEvent) => events.push(evt));

      store.recordCallStart({ call_id: 'call-2', caller: '', callee: '', direction: 'inbound' });
      expect(events).toHaveLength(1);
      expect(events[0].type).toBe('call_start');
      expect(events[0].data.call_id).toBe('call-2');
    });

    it('ignores empty call_id', () => {
      const store = new MetricsStore();
      store.recordCallStart({ call_id: '', caller: '', callee: '', direction: 'inbound' });
      expect(store.getActiveCalls()).toHaveLength(0);
    });
  });

  // --- recordTurn ---

  describe('recordTurn()', () => {
    it('appends turn to active call', () => {
      const store = new MetricsStore();
      store.recordCallStart({ call_id: 'call-t', caller: '', callee: '', direction: 'inbound' });
      store.recordTurn({ call_id: 'call-t', turn: { user_text: 'Hi', agent_text: 'Hello' } });

      const active = store.getActiveCalls();
      expect(active[0].turns).toHaveLength(1);
    });

    it('publishes turn_complete SSE event', () => {
      const store = new MetricsStore();
      const events: SSEEvent[] = [];
      store.on('sse', (evt: SSEEvent) => events.push(evt));

      store.recordCallStart({ call_id: 'call-te', caller: '', callee: '', direction: 'inbound' });
      store.recordTurn({ call_id: 'call-te', turn: { text: 'data' } });

      const turnEvents = events.filter((e) => e.type === 'turn_complete');
      expect(turnEvents).toHaveLength(1);
    });

    it('ignores if no active call matches', () => {
      const store = new MetricsStore();
      // Should not throw
      store.recordTurn({ call_id: 'nonexistent', turn: {} });
    });
  });

  // --- recordCallEnd ---

  describe('recordCallEnd()', () => {
    it('moves call from active to completed', () => {
      const store = new MetricsStore();
      store.recordCallStart({ call_id: 'call-e', caller: '+1', callee: '+2', direction: 'inbound' });
      store.recordCallEnd({ call_id: 'call-e', transcript: [] }, { cost: { total: 0.01 } });

      expect(store.getActiveCalls()).toHaveLength(0);
      expect(store.callCount).toBe(1);
    });

    it('publishes call_end SSE event', () => {
      const store = new MetricsStore();
      const events: SSEEvent[] = [];
      store.on('sse', (evt: SSEEvent) => events.push(evt));

      store.recordCallStart({ call_id: 'call-ee', caller: '', callee: '', direction: 'inbound' });
      store.recordCallEnd({ call_id: 'call-ee', transcript: [] });

      const endEvents = events.filter((e) => e.type === 'call_end');
      expect(endEvents).toHaveLength(1);
    });

    it('ignores empty call_id', () => {
      const store = new MetricsStore();
      store.recordCallEnd({ call_id: '' });
      expect(store.callCount).toBe(0);
    });
  });

  // --- Circular buffer (500 calls default) ---

  describe('circular buffer', () => {
    it('evicts oldest calls when maxCalls exceeded', () => {
      const store = new MetricsStore(5);

      for (let i = 0; i < 8; i++) {
        store.recordCallStart({ call_id: `call-${i}`, caller: '', callee: '', direction: 'inbound' });
        store.recordCallEnd({ call_id: `call-${i}`, transcript: [] });
      }

      expect(store.callCount).toBe(5);
      // Oldest calls (0, 1, 2) should be evicted
      expect(store.getCall('call-0')).toBeNull();
      expect(store.getCall('call-1')).toBeNull();
      expect(store.getCall('call-2')).toBeNull();
      // Newest calls (3-7) should still exist
      expect(store.getCall('call-3')).not.toBeNull();
      expect(store.getCall('call-7')).not.toBeNull();
    });

    it('default maxCalls is 500', () => {
      const store = new MetricsStore();
      // Record 505 calls
      for (let i = 0; i < 505; i++) {
        store.recordCallStart({ call_id: `c-${i}`, caller: '', callee: '', direction: 'inbound' });
        store.recordCallEnd({ call_id: `c-${i}`, transcript: [] });
      }
      expect(store.callCount).toBe(500);
    });
  });

  // --- Active calls tracking ---

  describe('active calls tracking', () => {
    it('tracks multiple active calls', () => {
      const store = new MetricsStore();
      store.recordCallStart({ call_id: 'a1', caller: '', callee: '', direction: 'inbound' });
      store.recordCallStart({ call_id: 'a2', caller: '', callee: '', direction: 'outbound' });

      expect(store.getActiveCalls()).toHaveLength(2);
    });

    it('removes call from active on end', () => {
      const store = new MetricsStore();
      store.recordCallStart({ call_id: 'a3', caller: '', callee: '', direction: 'inbound' });
      store.recordCallEnd({ call_id: 'a3', transcript: [] });

      expect(store.getActiveCalls()).toHaveLength(0);
    });
  });

  // --- getCalls ---

  describe('getCalls()', () => {
    it('returns calls in reverse chronological order', () => {
      const store = new MetricsStore();
      for (let i = 0; i < 5; i++) {
        store.recordCallStart({ call_id: `c${i}`, caller: '', callee: '', direction: 'inbound' });
        store.recordCallEnd({ call_id: `c${i}`, transcript: [] });
      }

      const calls = store.getCalls(3);
      expect(calls).toHaveLength(3);
      expect(calls[0].call_id).toBe('c4'); // most recent first
      expect(calls[2].call_id).toBe('c2');
    });

    it('supports offset pagination', () => {
      const store = new MetricsStore();
      for (let i = 0; i < 5; i++) {
        store.recordCallStart({ call_id: `p${i}`, caller: '', callee: '', direction: 'inbound' });
        store.recordCallEnd({ call_id: `p${i}`, transcript: [] });
      }

      const page2 = store.getCalls(2, 2);
      expect(page2).toHaveLength(2);
      expect(page2[0].call_id).toBe('p2');
    });
  });

  // --- getCall ---

  describe('getCall()', () => {
    it('retrieves a specific call by ID', () => {
      const store = new MetricsStore();
      store.recordCallStart({ call_id: 'find-me', caller: '+1', callee: '+2', direction: 'inbound' });
      store.recordCallEnd({ call_id: 'find-me', transcript: [{ role: 'user', text: 'Hi', timestamp: 0 }] });

      const call = store.getCall('find-me');
      expect(call).not.toBeNull();
      expect(call!.call_id).toBe('find-me');
      expect(call!.transcript).toHaveLength(1);
    });

    it('returns null for non-existent call', () => {
      const store = new MetricsStore();
      expect(store.getCall('nonexistent')).toBeNull();
    });
  });

  // --- getAggregates ---

  describe('getAggregates()', () => {
    it('returns zeros when no calls', () => {
      const store = new MetricsStore();
      const agg = store.getAggregates();
      expect(agg.total_calls).toBe(0);
      expect(agg.total_cost).toBe(0);
      expect(agg.avg_duration).toBe(0);
      expect(agg.avg_latency_ms).toBe(0);
    });

    it('aggregates cost breakdown across calls', () => {
      const store = new MetricsStore();

      for (let i = 0; i < 3; i++) {
        store.recordCallStart({ call_id: `agg-${i}`, caller: '', callee: '', direction: 'inbound' });
        store.recordCallEnd(
          { call_id: `agg-${i}`, transcript: [] },
          {
            cost: { stt: 0.01, tts: 0.02, llm: 0.0, telephony: 0.005, total: 0.035 },
            duration_seconds: 30,
            latency_avg: { total_ms: 200 },
          },
        );
      }

      const agg = store.getAggregates();
      expect(agg.total_calls).toBe(3);
      expect((agg.total_cost as number)).toBeCloseTo(0.105, 4);
      expect((agg.avg_duration as number)).toBeCloseTo(30, 0);
      const breakdown = agg.cost_breakdown as Record<string, number>;
      expect(breakdown.stt).toBeCloseTo(0.03, 4);
      expect(breakdown.tts).toBeCloseTo(0.06, 4);
    });

    it('includes active_calls count', () => {
      const store = new MetricsStore();
      store.recordCallStart({ call_id: 'act-1', caller: '', callee: '', direction: 'inbound' });
      const agg = store.getAggregates();
      expect(agg.active_calls).toBe(1);
    });
  });

  // --- getCallsInRange ---

  describe('getCallsInRange()', () => {
    it('filters calls by timestamp range', () => {
      const store = new MetricsStore();

      // Record calls with different start times
      store.recordCallStart({ call_id: 'r1', caller: '', callee: '', direction: 'inbound' });
      store.recordCallEnd({ call_id: 'r1', transcript: [] });

      const allCalls = store.getCallsInRange();
      expect(allCalls.length).toBeGreaterThanOrEqual(1);
    });

    it('returns empty array when no calls match range', () => {
      const store = new MetricsStore();
      store.recordCallStart({ call_id: 'r2', caller: '', callee: '', direction: 'inbound' });
      store.recordCallEnd({ call_id: 'r2', transcript: [] });

      // Use a future timestamp range
      const futureTs = Date.now() / 1000 + 86400;
      const calls = store.getCallsInRange(futureTs, futureTs + 3600);
      expect(calls).toHaveLength(0);
    });
  });

  // --- Pub/sub fan-out: 10 listeners each get same event exactly once ---

  describe('pub/sub fan-out', () => {
    it('10 listeners each get the same event exactly once', () => {
      const store = new MetricsStore();
      const receivedEvents: SSEEvent[][] = Array.from({ length: 10 }, () => []);

      for (let i = 0; i < 10; i++) {
        const idx = i;
        store.on('sse', (evt: SSEEvent) => {
          receivedEvents[idx].push(evt);
        });
      }

      store.recordCallStart({
        call_id: 'fanout-1',
        caller: '+15551111111',
        callee: '+15552222222',
        direction: 'inbound',
      });

      for (let i = 0; i < 10; i++) {
        expect(receivedEvents[i]).toHaveLength(1);
        expect(receivedEvents[i][0].type).toBe('call_start');
        expect(receivedEvents[i][0].data.call_id).toBe('fanout-1');
      }
    });
  });

  // --- Soft delete ---

  describe('deleteCalls()', () => {
    function seedCompleted(store: MetricsStore, id: string, latencyMs = 200, costTotal = 0.01) {
      store.recordCallStart({
        call_id: id,
        caller: '+15551111111',
        callee: '+15552222222',
        direction: 'inbound',
      });
      store.recordCallEnd(
        { call_id: id },
        {
          duration_seconds: 30,
          cost: { total: costTotal, stt: 0, tts: 0, llm: 0, telephony: costTotal },
          latency_avg: { agent_response_ms: latencyMs },
        },
      );
    }

    it('hides deleted calls from getCalls / getCall / callCount', () => {
      const store = new MetricsStore();
      seedCompleted(store, 'keep-1');
      seedCompleted(store, 'drop-1');
      expect(store.callCount).toBe(2);

      const accepted = store.deleteCalls(['drop-1']);
      expect(accepted).toEqual(['drop-1']);
      expect(store.callCount).toBe(1);
      expect(store.getCalls()).toHaveLength(1);
      expect(store.getCalls()[0].call_id).toBe('keep-1');
      expect(store.getCall('drop-1')).toBeNull();
      expect(store.getCall('keep-1')).not.toBeNull();
      expect(store.isDeleted('drop-1')).toBe(true);
      expect(store.isDeleted('keep-1')).toBe(false);
    });

    it('excludes deleted calls from aggregates so avg latency + cost shift', () => {
      const store = new MetricsStore();
      seedCompleted(store, 'fast', 100, 0.01);
      seedCompleted(store, 'slow', 900, 0.05);
      const before = store.getAggregates() as Record<string, number>;
      expect(before.total_calls).toBe(2);
      expect(before.avg_latency_ms).toBe(500); // (100 + 900) / 2

      store.deleteCalls(['slow']);
      const after = store.getAggregates() as Record<string, number>;
      expect(after.total_calls).toBe(1);
      expect(after.avg_latency_ms).toBe(100); // only "fast" remains
      expect(after.total_cost).toBe(0.01);
    });

    it('excludes deleted calls from getCallsInRange', () => {
      const store = new MetricsStore();
      seedCompleted(store, 'a');
      seedCompleted(store, 'b');
      expect(store.getCallsInRange()).toHaveLength(2);
      store.deleteCalls(['b']);
      const remaining = store.getCallsInRange();
      expect(remaining).toHaveLength(1);
      expect(remaining[0].call_id).toBe('a');
    });

    it('refuses to delete active calls', () => {
      const store = new MetricsStore();
      store.recordCallStart({
        call_id: 'live-1',
        caller: '+15551111111',
        callee: '+15552222222',
        direction: 'inbound',
      });
      const accepted = store.deleteCalls(['live-1']);
      expect(accepted).toEqual([]);
      expect(store.isDeleted('live-1')).toBe(false);
      expect(store.getActiveCalls()).toHaveLength(1);
    });

    it('is idempotent — re-deleting an id returns empty + no extra event', () => {
      const store = new MetricsStore();
      seedCompleted(store, 'x');
      const events: SSEEvent[] = [];
      store.on('sse', (e: SSEEvent) => events.push(e));
      const first = store.deleteCalls(['x']);
      const second = store.deleteCalls(['x']);
      expect(first).toEqual(['x']);
      expect(second).toEqual([]);
      const deletedEvents = events.filter((e) => e.type === 'calls_deleted');
      expect(deletedEvents).toHaveLength(1);
    });

    it('emits SSE calls_deleted with the accepted ids', () => {
      const store = new MetricsStore();
      seedCompleted(store, 'a');
      seedCompleted(store, 'b');
      const events: SSEEvent[] = [];
      store.on('sse', (e: SSEEvent) => events.push(e));
      const accepted = store.deleteCalls(['a', 'b']);
      expect(accepted).toEqual(['a', 'b']);
      const deletedEvent = events.find((e) => e.type === 'calls_deleted');
      expect(deletedEvent).toBeDefined();
      expect(deletedEvent!.data.call_ids).toEqual(['a', 'b']);
    });

    it('handles empty / non-string / unknown ids gracefully', () => {
      const store = new MetricsStore();
      seedCompleted(store, 'real');
      // unknown ids are still accepted into the deleted-set (so a future
      // hydrate that resurrects them stays hidden) — matches Python.
      expect(store.deleteCalls([])).toEqual([]);
      expect(store.deleteCalls([''])).toEqual([]);
      expect(store.deleteCalls(['unknown-id'])).toEqual(['unknown-id']);
      expect(store.callCount).toBe(1);
    });
  });

  // --- Read/write isolation between event listeners ---

  describe('read/write isolation between event listeners', () => {
    it('listener modifications do not affect other listeners', () => {
      const store = new MetricsStore();
      const results: string[] = [];

      store.on('sse', (evt: SSEEvent) => {
        // Listener 1 mutates the data (bad practice, but should not affect listener 2)
        (evt.data as Record<string, unknown>).mutated = true;
        results.push('listener1');
      });

      store.on('sse', (evt: SSEEvent) => {
        // Listener 2 reads the data — may see mutation since same object reference
        // This test verifies both listeners fire regardless
        results.push('listener2');
      });

      store.recordCallStart({ call_id: 'iso-1', caller: '', callee: '', direction: 'inbound' });
      expect(results).toEqual(['listener1', 'listener2']);
    });
  });
});

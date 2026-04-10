/**
 * In-memory metrics store for the local dashboard.
 *
 * Keeps the last `maxCalls` completed calls and tracks active calls.
 * Supports SSE event subscribers for real-time updates.
 */

import { EventEmitter } from 'events';

export interface CallRecord {
  call_id: string;
  caller: string;
  callee: string;
  direction: string;
  started_at: number;
  ended_at?: number;
  transcript?: Array<{ role: string; text: string; timestamp: number }>;
  turns?: unknown[];
  metrics?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface SSEEvent {
  type: string;
  data: Record<string, unknown>;
}

export class MetricsStore extends EventEmitter {
  private readonly maxCalls: number;
  private calls: CallRecord[] = [];
  private activeCalls: Map<string, CallRecord> = new Map();

  constructor(maxCalls = 500) {
    super();
    this.maxCalls = maxCalls;
  }

  private publish(eventType: string, data: Record<string, unknown>): void {
    this.emit('sse', { type: eventType, data } as SSEEvent);
  }

  recordCallStart(data: Record<string, unknown>): void {
    const callId = (data.call_id as string) || '';
    if (!callId) return;

    const record: CallRecord = {
      call_id: callId,
      caller: (data.caller as string) || '',
      callee: (data.callee as string) || '',
      direction: (data.direction as string) || 'inbound',
      started_at: Date.now() / 1000,
      turns: [],
    };
    this.activeCalls.set(callId, record);

    this.publish('call_start', {
      call_id: callId,
      caller: record.caller,
      callee: record.callee,
      direction: record.direction,
    });
  }

  recordTurn(data: Record<string, unknown>): void {
    const callId = (data.call_id as string) || '';
    const turn = data.turn;
    if (!callId || turn == null) return;

    const active = this.activeCalls.get(callId);
    if (active) {
      if (!active.turns) active.turns = [];
      active.turns.push(turn);
    }

    this.publish('turn_complete', { call_id: callId, turn: turn as Record<string, unknown> });
  }

  recordCallEnd(data: Record<string, unknown>, metrics?: Record<string, unknown> | null): void {
    const callId = (data.call_id as string) || '';
    if (!callId) return;

    const active = this.activeCalls.get(callId);
    this.activeCalls.delete(callId);

    const entry: CallRecord = {
      call_id: callId,
      caller: active?.caller || '',
      callee: active?.callee || '',
      direction: active?.direction || 'inbound',
      started_at: active?.started_at || 0,
      ended_at: Date.now() / 1000,
      transcript: (data.transcript as CallRecord['transcript']) || [],
      metrics: metrics ?? null,
    };

    this.calls.push(entry);
    if (this.calls.length > this.maxCalls) {
      this.calls = this.calls.slice(-this.maxCalls);
    }

    this.publish('call_end', {
      call_id: callId,
      metrics: entry.metrics ?? null,
    });
  }

  getCalls(limit = 50, offset = 0): CallRecord[] {
    const ordered = [...this.calls].reverse();
    return ordered.slice(offset, offset + limit);
  }

  getCall(callId: string): CallRecord | null {
    for (let i = this.calls.length - 1; i >= 0; i--) {
      if (this.calls[i].call_id === callId) return this.calls[i];
    }
    return null;
  }

  getActiveCalls(): CallRecord[] {
    return Array.from(this.activeCalls.values());
  }

  getAggregates(): Record<string, unknown> {
    const totalCalls = this.calls.length;
    if (totalCalls === 0) {
      return {
        total_calls: 0,
        total_cost: 0,
        avg_duration: 0,
        avg_latency_ms: 0,
        cost_breakdown: { stt: 0, tts: 0, llm: 0, telephony: 0 },
        active_calls: this.activeCalls.size,
      };
    }

    let totalCost = 0;
    let totalDuration = 0;
    let totalLatency = 0;
    let latencyCount = 0;
    let costStt = 0;
    let costTts = 0;
    let costLlm = 0;
    let costTel = 0;

    for (const call of this.calls) {
      const m = call.metrics as Record<string, unknown> | null;
      if (!m) continue;
      const cost = (m.cost as Record<string, number>) || {};
      totalCost += cost.total || 0;
      costStt += cost.stt || 0;
      costTts += cost.tts || 0;
      costLlm += cost.llm || 0;
      costTel += cost.telephony || 0;
      totalDuration += (m.duration_seconds as number) || 0;
      const avgLat = (m.latency_avg as Record<string, number>) || {};
      const tMs = avgLat.total_ms || 0;
      if (tMs > 0) {
        totalLatency += tMs;
        latencyCount++;
      }
    }

    return {
      total_calls: totalCalls,
      total_cost: Math.round(totalCost * 1e6) / 1e6,
      avg_duration: Math.round((totalDuration / totalCalls) * 100) / 100,
      avg_latency_ms: latencyCount > 0
        ? Math.round((totalLatency / latencyCount) * 10) / 10
        : 0,
      cost_breakdown: {
        stt: Math.round(costStt * 1e6) / 1e6,
        tts: Math.round(costTts * 1e6) / 1e6,
        llm: Math.round(costLlm * 1e6) / 1e6,
        telephony: Math.round(costTel * 1e6) / 1e6,
      },
      active_calls: this.activeCalls.size,
    };
  }

  getCallsInRange(fromTs = 0, toTs = 0): CallRecord[] {
    return this.calls.filter((call) => {
      const started = call.started_at || 0;
      if (fromTs && started < fromTs) return false;
      if (toTs && started > toTs) return false;
      return true;
    });
  }

  get callCount(): number {
    return this.calls.length;
  }
}

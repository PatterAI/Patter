/**
 * In-memory metrics store for the local dashboard.
 *
 * Keeps the last `maxCalls` completed calls and tracks active calls.
 * Supports SSE event subscribers for real-time updates.
 *
 * Optional disk hydration: when `CallLogger` writes per-call records under
 * `<root>/calls/YYYY/MM/DD/<call_id>/metadata.json`, calling
 * `hydrate(logRoot)` on a fresh store rebuilds the in-memory list from those
 * files so the dashboard survives process restarts (the persistence is in
 * the JSONL/JSON files, the store is just a cache on top).
 */

import { EventEmitter } from 'events';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { getLogger } from '../logger';

export interface CallRecord {
  call_id: string;
  caller: string;
  callee: string;
  direction: string;
  started_at: number;
  ended_at?: number;
  /**
   * Current lifecycle state: ``initiated`` (pre-registered), ``ringing``,
   * ``in-progress``, ``completed``, ``no-answer``, ``busy``, ``failed``,
   * ``canceled``, or ``webhook_error``.
   */
  status?: string;
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

  /**
   * Accepts either a numeric ``maxCalls`` (legacy positional — matches the
   * original TS API) or an options object ``{ maxCalls }`` to align with the
   * Python SDK's keyword-argument style. Plain literals also work:
   * ``new MetricsStore()`` / ``new MetricsStore(100)`` / ``new MetricsStore({ maxCalls: 100 })``.
   */
  constructor(maxCallsOrOpts: number | { maxCalls?: number } = 500) {
    super();
    this.maxCalls =
      typeof maxCallsOrOpts === 'number'
        ? maxCallsOrOpts
        : maxCallsOrOpts.maxCalls ?? 500;
  }

  private publish(eventType: string, data: Record<string, unknown>): void {
    this.emit('sse', { type: eventType, data } as SSEEvent);
  }

  recordCallStart(data: Record<string, unknown>): void {
    const callId = (data.call_id as string) || '';
    if (!callId) return;

    const existing = this.activeCalls.get(callId);
    if (existing) {
      // Upgrade a pre-registered (``initiated``) outbound call to in-progress
      // without losing its from/to metadata. See BUG #06.
      existing.caller = (data.caller as string) || existing.caller;
      existing.callee = (data.callee as string) || existing.callee;
      existing.direction = (data.direction as string) || existing.direction;
      existing.status = 'in-progress';
      existing.turns = existing.turns || [];
    } else {
      const record: CallRecord = {
        call_id: callId,
        caller: (data.caller as string) || '',
        callee: (data.callee as string) || '',
        direction: (data.direction as string) || 'inbound',
        started_at: Date.now() / 1000,
        status: 'in-progress',
        turns: [],
      };
      this.activeCalls.set(callId, record);
    }

    this.publish('call_start', {
      call_id: callId,
      caller: (data.caller as string) || '',
      callee: (data.callee as string) || '',
      direction: (data.direction as string) || 'inbound',
    });
  }

  /**
   * Pre-register an outbound call before any webhook fires. Lets the
   * dashboard surface attempts that never reach media (no-answer, busy,
   * carrier-rejected). Mirrors the Python ``record_call_initiated``.
   */
  recordCallInitiated(data: Record<string, unknown>): void {
    const callId = (data.call_id as string) || '';
    if (!callId) return;
    if (this.activeCalls.has(callId)) return; // first writer wins

    const record: CallRecord = {
      call_id: callId,
      caller: (data.caller as string) || '',
      callee: (data.callee as string) || '',
      direction: (data.direction as string) || 'outbound',
      started_at: Date.now() / 1000,
      status: 'initiated',
      turns: [],
    };
    this.activeCalls.set(callId, record);
    this.publish('call_initiated', {
      call_id: callId,
      caller: record.caller,
      callee: record.callee,
      direction: record.direction,
      status: record.status,
    });
  }

  /**
   * Update the status of an active or completed call. Terminal states
   * (completed, no-answer, busy, failed, canceled, webhook_error) move the
   * row from active to completed so the UI freezes the live duration timer.
   */
  updateCallStatus(callId: string, status: string, extra: Record<string, unknown> = {}): void {
    if (!callId || !status) return;
    const TERMINAL = new Set(['completed', 'no-answer', 'busy', 'failed', 'canceled', 'webhook_error']);
    const active = this.activeCalls.get(callId);
    if (active) {
      active.status = status;
      Object.assign(active, extra);
      if (TERMINAL.has(status)) {
        const entry: CallRecord = {
          call_id: callId,
          caller: active.caller || '',
          callee: active.callee || '',
          direction: active.direction || 'outbound',
          started_at: active.started_at || 0,
          ended_at: Date.now() / 1000,
          status,
          metrics: null,
          ...extra,
        };
        this.activeCalls.delete(callId);
        this.calls.push(entry);
        if (this.calls.length > this.maxCalls) {
          this.calls = this.calls.slice(-this.maxCalls);
        }
      }
    } else {
      for (let i = this.calls.length - 1; i >= 0; i--) {
        if (this.calls[i].call_id === callId) {
          this.calls[i].status = status;
          Object.assign(this.calls[i], extra);
          break;
        }
      }
    }
    this.publish('call_status', { call_id: callId, status, ...extra });
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

    // Preserve explicit status set by a statusCallback during the call
    // (e.g. "no-answer" from Twilio) — fall back to "completed" when the
    // row was still showing the normal "in-progress" state at hang-up.
    const activeStatus = active?.status;
    const resolvedStatus =
      activeStatus && activeStatus !== 'in-progress' ? activeStatus : 'completed';
    const entry: CallRecord = {
      call_id: callId,
      caller: (data.caller as string) || active?.caller || '',
      callee: (data.callee as string) || active?.callee || '',
      direction: active?.direction || (data.direction as string) || 'inbound',
      started_at: active?.started_at || 0,
      ended_at: Date.now() / 1000,
      transcript: (data.transcript as CallRecord['transcript']) || [],
      status: resolvedStatus,
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

  /** Look up an active call by id (returns undefined if not active or unknown). */
  getActive(callId: string): CallRecord | undefined {
    return this.activeCalls.get(callId);
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

  /**
   * Rebuild the in-memory call list from `metadata.json` files written by
   * `CallLogger` under `<logRoot>/calls/YYYY/MM/DD/<call_id>/`. Idempotent:
   * call_ids already in the store are skipped. Errors per file are logged
   * and swallowed so a single corrupt entry doesn't block hydration.
   *
   * Returns the number of calls newly added to the store.
   *
   * Safe to call before any traffic; intended to run once at server startup.
   */
  hydrate(logRoot: string | null | undefined): number {
    if (!logRoot) return 0;
    const callsRoot = path.join(logRoot, 'calls');
    if (!fs.existsSync(callsRoot)) return 0;

    const collected: CallRecord[] = [];
    const seen = new Set<string>(this.calls.map((c) => c.call_id));

    const walk = (dir: string, depth: number): void => {
      let entries: fs.Dirent[];
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        const childPath = path.join(dir, entry.name);
        if (depth < 3) {
          // YYYY / MM / DD layers — descend only when name is numeric to
          // skip stray files (DS_Store, indexes).
          if (entry.isDirectory() && /^\d+$/.test(entry.name)) {
            walk(childPath, depth + 1);
          }
          continue;
        }
        // depth === 3 → this is a per-call directory.
        if (!entry.isDirectory()) continue;
        const metadataPath = path.join(childPath, 'metadata.json');
        if (!fs.existsSync(metadataPath)) continue;
        try {
          const raw = fs.readFileSync(metadataPath, 'utf8');
          const meta = JSON.parse(raw) as Record<string, unknown>;
          const callId = (meta.call_id as string) || entry.name;
          if (!callId || seen.has(callId)) continue;
          collected.push(metadataToCallRecord(callId, meta));
          seen.add(callId);
        } catch (err) {
          getLogger().debug(
            `MetricsStore.hydrate: skipping ${metadataPath}: ${String(err)}`,
          );
        }
      }
    };

    walk(callsRoot, 0);

    // Stable order: oldest first (matches the order recordCallEnd would use).
    collected.sort((a, b) => (a.started_at || 0) - (b.started_at || 0));

    for (const rec of collected) {
      this.calls.push(rec);
      if (this.calls.length > this.maxCalls) {
        this.calls = this.calls.slice(-this.maxCalls);
      }
    }
    return collected.length;
  }
}

/** Translate a CallLogger ``metadata.json`` payload into a ``CallRecord``. */
function metadataToCallRecord(callId: string, meta: Record<string, unknown>): CallRecord {
  const startedAtRaw = meta.started_at;
  const endedAtRaw = meta.ended_at;
  const startedAt =
    typeof startedAtRaw === 'number'
      ? startedAtRaw
      : typeof startedAtRaw === 'string'
        ? Date.parse(startedAtRaw) / 1000
        : 0;
  const endedAt =
    typeof endedAtRaw === 'number'
      ? endedAtRaw
      : typeof endedAtRaw === 'string'
        ? Date.parse(endedAtRaw) / 1000
        : undefined;
  const status = (meta.status as string | undefined) || 'completed';
  const metrics =
    meta.metrics && typeof meta.metrics === 'object'
      ? (meta.metrics as Record<string, unknown>)
      : null;
  const transcript = Array.isArray(meta.transcript)
    ? (meta.transcript as CallRecord['transcript'])
    : [];
  return {
    call_id: callId,
    caller: (meta.caller as string) || '',
    callee: (meta.callee as string) || '',
    direction: (meta.direction as string) || 'inbound',
    started_at: Number.isFinite(startedAt) ? startedAt : 0,
    ended_at: endedAt !== undefined && Number.isFinite(endedAt) ? endedAt : undefined,
    status,
    metrics,
    transcript,
  };
}

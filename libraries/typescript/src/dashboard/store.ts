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
import { VERSION } from '../version';

/** Resolved SDK version (single source of truth: ``package.json``). */
function sdkVersion(): string {
  return VERSION;
}

/** Snapshot of a call as held by the dashboard store. */
export interface CallRecord {
  readonly call_id: string;
  readonly caller: string;
  readonly callee: string;
  readonly direction: string;
  readonly started_at: number;
  readonly ended_at?: number;
  /**
   * Current lifecycle state: ``initiated`` (pre-registered), ``ringing``,
   * ``in-progress``, ``completed``, ``no-answer``, ``busy``, ``failed``,
   * ``canceled``, or ``webhook_error``.
   */
  readonly status?: string;
  readonly transcript?: ReadonlyArray<{ readonly role: string; readonly text: string; readonly timestamp: number }>;
  readonly turns?: readonly unknown[];
  readonly metrics?: Record<string, unknown> | null;
  readonly [key: string]: unknown;
}

/** Mutable internal representation used while a call is in flight. */
interface MutableCallRecord {
  call_id: string;
  caller: string;
  callee: string;
  direction: string;
  started_at: number;
  ended_at?: number;
  status?: string;
  transcript?: Array<{ role: string; text: string; timestamp: number }>;
  turns?: unknown[];
  metrics?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/** Server-Sent-Event payload broadcast by `MetricsStore` for live UI updates. */
export interface SSEEvent {
  readonly type: string;
  readonly data: Readonly<Record<string, unknown>>;
}

/** In-memory bounded ring buffer of recent calls plus active-call tracking. */
export class MetricsStore extends EventEmitter {
  private readonly maxCalls: number;
  private calls: MutableCallRecord[] = [];
  private activeCalls: Map<string, MutableCallRecord> = new Map();
  /**
   * User-driven soft delete: call_ids the operator removed from the
   * dashboard view. The on-disk artefacts written by ``CallLogger``
   * (``metadata.json``, ``transcript.jsonl``) are intentionally NOT
   * touched — they serve as the durable backup. All read paths
   * (``getCalls`` / ``getCall`` / ``getAggregates`` / ``getCallsInRange``
   * / ``hydrate``) filter against this set so the call is invisible
   * to the UI and excluded from rolling metrics. Populated from
   * ``<logRoot>/.deleted_call_ids.json`` on hydrate so deletions
   * survive a process restart. Parity with Python.
   */
  private deletedCallIds: Set<string> = new Set();
  private deletedIdsPath: string | null = null;

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

  /** Mark a call as in-progress (creates the row if it does not yet exist). */
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
      const record: MutableCallRecord = {
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

    const record: MutableCallRecord = {
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
        // Preserve the running transcript and per-turn metrics accumulated
        // on the active record. Without this, a Twilio statusCallback that
        // arrives before the WS ``stop`` frame (and before
        // ``recordCallEnd``) would create a placeholder entry with no
        // transcript and no turns — and any dashboard fetch in that race
        // window would render the live-transcript pane blank. See BUG 2.
        //
        // TODO(0.6.2): updateCallStatus writes synthetic records with
        // metrics:undefined when status callbacks arrive before
        // recordCallEnd. The dashboard masks this via mergeCallPreserving
        // (useDashboardData.ts) but the root cause is here — recordCallEnd
        // should be the only writer to the completed buffer.
        const entry: MutableCallRecord = {
          call_id: callId,
          caller: active.caller || '',
          callee: active.callee || '',
          direction: active.direction || 'outbound',
          started_at: active.started_at || 0,
          ended_at: Date.now() / 1000,
          status,
          metrics: null,
          ...(active.turns && active.turns.length > 0
            ? { turns: active.turns }
            : {}),
          ...(active.transcript && active.transcript.length > 0
            ? { transcript: active.transcript }
            : {}),
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
          this.calls[i] = { ...this.calls[i], status, ...extra };
          break;
        }
      }
    }
    this.publish('call_status', { call_id: callId, status, ...extra });
  }

  /** Append a single conversation turn to an active call and broadcast it via SSE. */
  recordTurn(data: Record<string, unknown>): void {
    const callId = (data.call_id as string) || '';
    const turn = data.turn;
    if (!callId || turn == null) return;

    const active = this.activeCalls.get(callId);
    if (active) {
      if (!active.turns) active.turns = [];
      active.turns.push(turn);

      // Mirror each completed round-trip into a flat ``transcript`` array on
      // the active record so the live-transcript pane (``useTranscript`` in
      // the SPA, primary mapper path) sees an accumulating ``user → assistant
      // → user → assistant → …`` history without depending on the
      // ``TurnMetrics`` shape. The previous implementation only populated
      // ``active.turns`` (the metrics shape) and the SPA's fallback path
      // re-derived a transcript from it — but any consumer that read
      // ``record.transcript`` first (the canonical shape used by completed
      // calls) saw an empty array, so the live pane could blank between
      // round-trips. Mirroring keeps the two paths in sync. See dashboard
      // BUG 1.
      if (!active.transcript) active.transcript = [];
      const turnRecord = turn as {
        user_text?: unknown;
        agent_text?: unknown;
        timestamp?: unknown;
      };
      const userText =
        typeof turnRecord.user_text === 'string' ? turnRecord.user_text : '';
      const agentText =
        typeof turnRecord.agent_text === 'string' ? turnRecord.agent_text : '';
      const ts =
        typeof turnRecord.timestamp === 'number'
          ? turnRecord.timestamp
          : Date.now() / 1000;
      if (userText.length > 0) {
        active.transcript.push({ role: 'user', text: userText, timestamp: ts });
      }
      if (agentText.length > 0 && agentText !== '[interrupted]') {
        active.transcript.push({
          role: 'assistant',
          text: agentText,
          timestamp: ts,
        });
      }
    }

    this.publish('turn_complete', { call_id: callId, turn: turn as Record<string, unknown> });
  }

  /** Move a call from active to completed and persist its final metrics. */
  recordCallEnd(data: Record<string, unknown>, metrics?: Record<string, unknown> | null): void {
    const callId = (data.call_id as string) || '';
    if (!callId) return;

    const active = this.activeCalls.get(callId);
    this.activeCalls.delete(callId);

    // The Twilio ``statusCallback`` for ``CallStatus=completed`` arrives
    // shortly before the WS ``stop`` frame and runs ``updateCallStatus``,
    // which already moved the row from ``activeCalls`` into ``calls[]``.
    // By the time ``recordCallEnd`` runs the active record is gone and the
    // completed entry already exists. Without this lookup we'd push a
    // second row with ``started_at=0`` (no active to copy from) and empty
    // caller/callee — which is then ranked first by ``getCalls`` (newest
    // wins) and the older, well-formed row gets shadowed. End result: the
    // call disappears from the dashboard's 24 h window. See dashboard
    // BUG C.
    let existingIdx = -1;
    if (active === undefined) {
      for (let i = this.calls.length - 1; i >= 0; i--) {
        if (this.calls[i].call_id === callId) {
          existingIdx = i;
          break;
        }
      }
    }
    const existing = existingIdx >= 0 ? this.calls[existingIdx] : undefined;

    // Preserve explicit status set by a statusCallback during the call
    // (e.g. "no-answer" from Twilio) — fall back to "completed" when the
    // row was still showing the normal "in-progress" state at hang-up.
    const priorStatus = active?.status ?? existing?.status;
    const resolvedStatus =
      priorStatus && priorStatus !== 'in-progress' ? priorStatus : 'completed';
    // Resolve the final transcript and turns. ``data.transcript`` from the
    // SDK is the authoritative ``history.entries`` snapshot at hang-up; when
    // it's missing or empty (e.g. webhook-rejected inbound, or the active
    // record was already moved to ``calls[]`` by an earlier statusCallback
    // and the data payload doesn't carry one), fall back to the running
    // transcript we accumulated on the active record via ``recordTurn``.
    // This keeps the live-transcript pane stable across the call_status
    // (``completed``) → call_end gap. See dashboard BUG 2.
    const dataTranscript = data.transcript as MutableCallRecord['transcript'];
    const resolvedTranscript: MutableCallRecord['transcript'] =
      dataTranscript && dataTranscript.length > 0
        ? dataTranscript
        : active?.transcript && active.transcript.length > 0
          ? active.transcript
          : existing?.transcript && existing.transcript.length > 0
            ? existing.transcript
            : [];
    const resolvedTurns: unknown[] | undefined =
      active?.turns && active.turns.length > 0
        ? active.turns
        : existing?.turns && existing.turns.length > 0
          ? existing.turns
          : undefined;
    const entry: MutableCallRecord = {
      call_id: callId,
      caller:
        (data.caller as string) ||
        active?.caller ||
        existing?.caller ||
        '',
      callee:
        (data.callee as string) ||
        active?.callee ||
        existing?.callee ||
        '',
      direction:
        active?.direction ||
        existing?.direction ||
        (data.direction as string) ||
        'inbound',
      started_at: active?.started_at || existing?.started_at || 0,
      ended_at: Date.now() / 1000,
      transcript: resolvedTranscript,
      ...(resolvedTurns ? { turns: resolvedTurns } : {}),
      status: resolvedStatus,
      metrics: metrics ?? existing?.metrics ?? null,
    };

    if (existingIdx >= 0) {
      // Update in place so the buffer doesn't grow a duplicate row.
      this.calls[existingIdx] = entry;
    } else {
      this.calls.push(entry);
      if (this.calls.length > this.maxCalls) {
        this.calls = this.calls.slice(-this.maxCalls);
      }
    }

    this.publish('call_end', {
      call_id: callId,
      metrics: entry.metrics ?? null,
    });
  }

  /**
   * Return a window of completed calls in newest-first order.
   *
   * Soft-deleted call_ids (see ``deleteCalls``) are filtered out so the
   * dashboard never re-shows a row the user removed. The on-disk
   * artefacts are intentionally preserved as a backup.
   */
  getCalls(limit = 50, offset = 0): CallRecord[] {
    const visible = this.calls.filter((c) => !this.deletedCallIds.has(c.call_id));
    const ordered = visible.reverse();
    return ordered.slice(offset, offset + limit);
  }

  /**
   * Look up a completed call by id (newest match wins).
   *
   * Soft-deleted call_ids resolve to ``null`` so the SPA's detail pane
   * cannot render a row the user removed.
   */
  getCall(callId: string): CallRecord | null {
    if (this.deletedCallIds.has(callId)) return null;
    for (let i = this.calls.length - 1; i >= 0; i--) {
      if (this.calls[i].call_id === callId) return { ...this.calls[i] };
    }
    return null;
  }

  /**
   * Soft-delete one or more calls from the dashboard view.
   *
   * Adds each ``call_id`` to an in-memory set. Subsequent reads via
   * ``getCalls`` / ``getCall`` / ``getAggregates`` / ``getCallsInRange``
   * exclude the deleted ids, so rolling metrics (avg latency, total
   * spend) are recomputed without them. The on-disk
   * ``metadata.json`` / ``transcript.jsonl`` files written by
   * ``CallLogger`` are NOT touched — they serve as a durable backup
   * the operator can audit outside the dashboard.
   *
   * Active calls are never deletable. A call_id that is currently
   * in ``activeCalls`` is silently skipped so a mid-call delete
   * from the UI cannot orphan the live transcript pane.
   *
   * Persisted to ``<logRoot>/.deleted_call_ids.json`` (best-effort)
   * when ``hydrate()`` has been called with a log root. Parity with
   * Python ``delete_calls``.
   *
   * @returns The list of call_ids actually accepted as deleted.
   */
  deleteCalls(callIds: readonly string[]): string[] {
    const ids = new Set<string>();
    for (const cid of callIds || []) {
      if (typeof cid === 'string' && cid && !this.activeCalls.has(cid)) {
        ids.add(cid);
      }
    }
    if (ids.size === 0) return [];
    const accepted: string[] = [];
    for (const cid of ids) {
      if (!this.deletedCallIds.has(cid)) {
        this.deletedCallIds.add(cid);
        accepted.push(cid);
      }
    }
    if (accepted.length === 0) return [];
    accepted.sort();
    this.persistDeletedIds().catch((err) =>
      getLogger().debug(`MetricsStore.deleteCalls: persistDeletedIds failed: ${String(err)}`),
    );
    this.publish('calls_deleted', { call_ids: accepted });
    return accepted;
  }

  /** Whether ``callId`` was soft-deleted from the dashboard. */
  isDeleted(callId: string): boolean {
    return this.deletedCallIds.has(callId);
  }

  /** Snapshot of soft-deleted call_ids (sorted). */
  getDeletedCallIds(): string[] {
    return Array.from(this.deletedCallIds).sort();
  }

  /** Atomically persist the deleted-ids set to disk. Best-effort async. */
  private async persistDeletedIds(): Promise<void> {
    if (this.deletedIdsPath === null) return;
    try {
      const dir = path.dirname(this.deletedIdsPath);
      await fs.promises.mkdir(dir, { recursive: true });
      const tmp = this.deletedIdsPath + '.tmp';
      const payload = {
        version: 1,
        deleted_call_ids: Array.from(this.deletedCallIds).sort(),
      };
      await fs.promises.writeFile(tmp, JSON.stringify(payload, null, 2), 'utf8');
      await fs.promises.rename(tmp, this.deletedIdsPath);
    } catch (err) {
      getLogger().debug(
        `MetricsStore.persistDeletedIds: ${String(err)}`,
      );
    }
  }

  /** Look up an active call by id (returns undefined if not active or unknown). */
  getActive(callId: string): CallRecord | undefined {
    const rec = this.activeCalls.get(callId);
    return rec !== undefined ? { ...rec } : undefined;
  }

  /** Return all currently active (not yet ended) calls. */
  getActiveCalls(): CallRecord[] {
    return Array.from(this.activeCalls.values());
  }

  /**
   * Compute summary statistics across the buffered call history.
   *
   * Soft-deleted calls are excluded so rolling metrics (avg latency,
   * total spend) match exactly what the operator sees in the call list.
   */
  getAggregates(): Record<string, unknown> {
    const visible = this.calls.filter(
      (c) => !this.deletedCallIds.has(c.call_id),
    );
    const totalCalls = visible.length;
    if (totalCalls === 0) {
      return {
        total_calls: 0,
        total_cost: 0,
        avg_duration: 0,
        avg_latency_ms: 0,
        cost_breakdown: { stt: 0, tts: 0, llm: 0, telephony: 0 },
        active_calls: this.activeCalls.size,
        sdk_version: sdkVersion(),
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

    for (const call of visible) {
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
      // Prefer the user-perceived wait time (agent_response_ms) — falls
      // back to round-trip total_ms only when the SDK didn't record the
      // breakdown (legacy hydrate path).
      const tMs = avgLat.agent_response_ms || avgLat.total_ms || 0;
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
      sdk_version: sdkVersion(),
    };
  }

  /**
   * Return calls whose `started_at` falls within `[fromTs, toTs]` (Unix
   * seconds). Soft-deleted calls are filtered out.
   */
  getCallsInRange(fromTs = 0, toTs = 0): CallRecord[] {
    return this.calls.filter((call) => {
      if (this.deletedCallIds.has(call.call_id)) return false;
      const started = call.started_at || 0;
      if (fromTs && started < fromTs) return false;
      if (toTs && started > toTs) return false;
      return true;
    });
  }

  /** Number of completed (non-deleted) calls currently in the ring buffer. */
  get callCount(): number {
    let n = 0;
    for (const c of this.calls) {
      if (!this.deletedCallIds.has(c.call_id)) n++;
    }
    return n;
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

    // Wire the deleted-ids persistence path FIRST so any subsequent
    // ``deleteCalls`` call (even before history hydrates) lands in the
    // right file. Restore the set from disk so deletions survive a
    // process restart.
    const deletedIdsPath = path.join(logRoot, '.deleted_call_ids.json');
    this.deletedIdsPath = deletedIdsPath;
    if (fs.existsSync(deletedIdsPath)) {
      try {
        const raw = fs.readFileSync(deletedIdsPath, 'utf8');
        const payload = JSON.parse(raw) as { deleted_call_ids?: unknown };
        const arr = Array.isArray(payload.deleted_call_ids)
          ? (payload.deleted_call_ids as unknown[])
          : [];
        for (const cid of arr) {
          if (typeof cid === 'string' && cid.length > 0) {
            this.deletedCallIds.add(cid);
          }
        }
      } catch (err) {
        getLogger().debug(
          `MetricsStore.hydrate: skipping ${deletedIdsPath}: ${String(err)}`,
        );
      }
    }

    const callsRoot = path.join(logRoot, 'calls');
    if (!fs.existsSync(callsRoot)) return 0;

    const collected: MutableCallRecord[] = [];
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
          const record = metadataToCallRecord(callId, meta);
          if (record === null) {
            // Unparseable started_at → skip rather than insert as epoch 0
            // (which would corrupt sort order forever).
            getLogger().debug(
              `MetricsStore.hydrate: skipping ${metadataPath}: unparseable started_at`,
            );
            continue;
          }
          // CallLogger writes the transcript to a separate ``transcript.jsonl``
          // file (one turn per line) — ``metadata.json`` only carries a turn
          // count. Without this fallback, hydrated past calls render with an
          // empty transcript pane: the SPA polls /api/dashboard/calls/:id,
          // the route serves the hydrated record verbatim, and ``transcript``
          // is ``[]``. Read the sibling file and synthesise the entry list
          // the SPA expects so the pane populates on click.
          if (!record.transcript || record.transcript.length === 0) {
            const fromJsonl = loadTranscriptJsonl(
              path.join(childPath, 'transcript.jsonl'),
            );
            if (fromJsonl.length > 0) record.transcript = fromJsonl;
          }
          collected.push(record);
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

    // Re-check seen against this.calls before each insert. Defends against the
    // (rare) case where hydrate() is invoked concurrently with itself or with
    // live recordCallEnd() traffic — without this guard, the snapshot taken
    // at the top of hydrate() can be stale by the time we reach the writes.
    for (const rec of collected) {
      if (this.calls.some((c) => c.call_id === rec.call_id)) continue;
      this.calls.push(rec);
      if (this.calls.length > this.maxCalls) {
        this.calls = this.calls.slice(-this.maxCalls);
      }
    }
    return collected.length;
  }
}

/**
 * Build a ``metrics`` object from top-level CallLogger fields. ``CallLogger``
 * writes ``cost`` / ``latency`` / ``duration_ms`` / ``telephony_provider`` at
 * the top of ``metadata.json``, but the dashboard UI reads them from
 * ``metrics``. Without this fallback every hydrated call shows ``$0.00`` and
 * ``—`` for cost and latency.
 */
function metricsFromTopLevel(
  meta: Record<string, unknown>,
): Record<string, unknown> | null {
  const cost =
    meta.cost && typeof meta.cost === 'object'
      ? (meta.cost as Record<string, unknown>)
      : null;
  const latency =
    meta.latency && typeof meta.latency === 'object'
      ? (meta.latency as Record<string, unknown>)
      : null;
  const durationMs = meta.duration_ms;
  const telephony = meta.telephony_provider;
  if (cost === null && latency === null && durationMs == null && !telephony) {
    return null;
  }
  const out: Record<string, unknown> = {};
  if (cost !== null) out.cost = cost;
  if (latency !== null) {
    // Prefer the full LatencyBreakdown objects (avg/p50/p95/p99) when the
    // server persisted them. Old metadata.json files only carry flat
    // ``p50_ms/p95_ms/p99_ms`` totals — synthesize a minimal latency_avg
    // from those so the table still shows a number, but no breakdown is
    // available for those historical rows.
    const fullAvg = latency.avg && typeof latency.avg === 'object' ? (latency.avg as Record<string, unknown>) : null;
    const fullP50 = latency.p50 && typeof latency.p50 === 'object' ? (latency.p50 as Record<string, unknown>) : null;
    const fullP95 = latency.p95 && typeof latency.p95 === 'object' ? (latency.p95 as Record<string, unknown>) : null;
    const fullP99 = latency.p99 && typeof latency.p99 === 'object' ? (latency.p99 as Record<string, unknown>) : null;
    if (fullAvg) out.latency_avg = fullAvg;
    if (fullP50) out.latency_p50 = fullP50;
    if (fullP95) out.latency_p95 = fullP95;
    if (fullP99) out.latency_p99 = fullP99;
    if (!fullAvg && !fullP50 && !fullP95) {
      const totalMs =
        (typeof latency.p95_ms === 'number' && latency.p95_ms) ||
        (typeof latency.p50_ms === 'number' && latency.p50_ms) ||
        0;
      out.latency_avg = { total_ms: totalMs };
    }
    out.latency = latency;
  }
  if (typeof durationMs === 'number' && durationMs > 0) {
    out.duration_seconds = durationMs / 1000;
  }
  if (typeof telephony === 'string' && telephony) {
    out.telephony_provider = telephony;
  }
  return Object.keys(out).length > 0 ? out : null;
}

/**
 * Translate a CallLogger ``metadata.json`` payload into a ``CallRecord``.
 * Returns ``null`` when ``started_at`` is missing or unparseable — the record
 * would otherwise be silently inserted with ``started_at = 0`` (Unix epoch),
 * which corrupts every sort/range query that depends on it.
 */
function metadataToCallRecord(
  callId: string,
  meta: Record<string, unknown>,
): MutableCallRecord | null {
  const startedAt = parseTimestamp(meta.started_at);
  if (startedAt === null) return null;
  const endedAt = parseTimestamp(meta.ended_at);
  const status = (meta.status as string | undefined) || 'completed';
  const metrics =
    meta.metrics && typeof meta.metrics === 'object'
      ? (meta.metrics as Record<string, unknown>)
      : metricsFromTopLevel(meta);
  const transcript = Array.isArray(meta.transcript)
    ? (meta.transcript as MutableCallRecord['transcript'])
    : [];
  return {
    call_id: callId,
    caller: (meta.caller as string) || '',
    callee: (meta.callee as string) || '',
    direction: (meta.direction as string) || 'inbound',
    started_at: startedAt,
    ended_at: endedAt ?? undefined,
    status,
    metrics,
    transcript,
  };
}

/**
 * Reconstruct the dashboard ``transcript`` array from a CallLogger
 * ``transcript.jsonl`` file. Each line is a turn record carrying
 * ``user_text`` / ``agent_text`` / ``ts`` (ISO-8601). We expand each
 * non-empty side into a separate ``{role, text, timestamp}`` entry so the
 * SPA's ``toUiTranscript`` mapper can render them in order. Returns an
 * empty array on any IO/parse failure — hydrate is best-effort and a
 * malformed transcript file should not block the call row from showing.
 */
function loadTranscriptJsonl(
  filePath: string,
): NonNullable<MutableCallRecord['transcript']> {
  try {
    if (!fs.existsSync(filePath)) return [];
    const raw = fs.readFileSync(filePath, 'utf8');
    const lines = raw.split('\n').filter((l) => l.trim().length > 0);
    const out: NonNullable<MutableCallRecord['transcript']> = [];
    for (const line of lines) {
      let row: Record<string, unknown>;
      try {
        row = JSON.parse(line) as Record<string, unknown>;
      } catch {
        continue;
      }
      const tsIso = typeof row.ts === 'string' ? Date.parse(row.ts) / 1000 : NaN;
      const tsNumeric =
        typeof row.timestamp === 'number' ? row.timestamp : NaN;
      const timestamp = Number.isFinite(tsIso)
        ? tsIso
        : Number.isFinite(tsNumeric)
          ? tsNumeric
          : 0;
      const userText = typeof row.user_text === 'string' ? row.user_text : '';
      const agentText =
        typeof row.agent_text === 'string' ? row.agent_text : '';
      if (userText.length > 0) {
        out.push({ role: 'user', text: userText, timestamp });
      }
      if (agentText.length > 0 && agentText !== '[interrupted]') {
        out.push({ role: 'assistant', text: agentText, timestamp });
      }
    }
    return out;
  } catch {
    return [];
  }
}

/**
 * Parse a metadata timestamp into Unix seconds. Accepts numbers (seconds)
 * and ISO-8601 strings; returns ``null`` for missing, unrecognized, or
 * unparseable values so callers can decide to skip the record rather than
 * silently insert it as epoch 0.
 */
function parseTimestamp(raw: unknown): number | null {
  if (typeof raw === 'number') {
    return Number.isFinite(raw) ? raw : null;
  }
  if (typeof raw === 'string') {
    const ms = Date.parse(raw);
    return Number.isFinite(ms) ? ms / 1000 : null;
  }
  return null;
}

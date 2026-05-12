// Pure SSE/refresh merge helpers extracted from useDashboardData so they can
// be unit-tested without a React harness. Both functions are immutable —
// callers must treat the returned arrays as readonly.

import type { CallRecord } from '../lib/api';
import { toUiCall, type Call } from '../lib/mappers';

/**
 * Hard cap on the number of calls retained in the SPA after a merge.
 * Mirrors the server-side ``MetricsStore`` ring buffer default (500) so the
 * UI cannot accumulate ``prev_only`` rows for calls the server has already
 * evicted. Without this cap, ``mergeCallPreserving`` would grow the array
 * indefinitely on long-lived sessions: every prior call still pinned by
 * ``prev`` would be re-appended on every refresh even after the server has
 * dropped it from the ring buffer.
 */
const MAX_UI_CALLS = 500;

/**
 * Project the server's active + recent payloads into a single UI list with
 * stable ordering: active calls first (live status surfaces at the top),
 * then completed calls newest-first as the server returned them. Duplicate
 * ``call_id`` is resolved active-wins-over-recent so a still-running call
 * never gets a stale terminal row.
 */
export function mergeCalls(active: CallRecord[], recent: CallRecord[]): Call[] {
  const seen = new Set<string>();
  const merged: Call[] = [];
  for (const record of active) {
    if (seen.has(record.call_id)) continue;
    seen.add(record.call_id);
    merged.push(toUiCall(record));
  }
  for (const record of recent) {
    if (seen.has(record.call_id)) continue;
    seen.add(record.call_id);
    merged.push(toUiCall(record));
  }
  return merged;
}

/**
 * Upsert a fresh snapshot of calls into the previous UI state by ``call_id``.
 *
 * Two reasons this is an upsert rather than a replace:
 *
 * 1. ``MetricsStore.updateCallStatus`` may write a synthetic terminal
 *    record with ``metrics: undefined`` ahead of the canonical
 *    ``recordCallEnd`` write (Twilio statusCallback racing the WS ``stop``
 *    frame). The ``next.field ?? prev.field`` per-critical-field merge
 *    masks the race window so transcripts + latency don't blank out. See
 *    ``store.ts`` TODO(0.6.2) for the root-cause fix.
 *
 * 2. When a second call starts back-to-back with the first, the SSE
 *    ``call_start`` refresh occasionally lands with the freshly-ended call
 *    not yet visible in ``/api/dashboard/calls`` (the server publishes the
 *    SSE event for the new call before the prior call's terminal write
 *    completes, or pagination clips it). Replacing the array verbatim
 *    with the server response would drop the prior call from the UI even
 *    though it is still in the ring buffer — exactly the regression
 *    reported in #124. Treating ``prev`` as the union-anchor keeps the
 *    prior call visible until the server snapshot stabilises.
 *
 * The server's ``maxCalls`` ring buffer (default 500) bounds growth on
 * long-lived sessions; the UI list is naturally bounded by what
 * ``fetchCalls`` paginates plus whatever lives in ``prev`` from the
 * current session.
 */
export function mergeCallPreserving(prev: Call[], next: Call[]): Call[] {
  const prevById = new Map(prev.map((c) => [c.id, c]));
  const nextIds = new Set(next.map((c) => c.id));
  const merged: Call[] = next.map((nc) => {
    const pc = prevById.get(nc.id);
    if (!pc) return nc;
    return {
      ...pc,
      ...nc,
      latencyP95: nc.latencyP95 ?? pc.latencyP95,
      latencyP50: nc.latencyP50 ?? pc.latencyP50,
      sttAvg: nc.sttAvg ?? pc.sttAvg,
      ttsAvg: nc.ttsAvg ?? pc.ttsAvg,
      llmAvg: nc.llmAvg ?? pc.llmAvg,
      turnCount: nc.turnCount ?? pc.turnCount,
      agentResponseP50: nc.agentResponseP50 ?? pc.agentResponseP50,
      agentResponseP95: nc.agentResponseP95 ?? pc.agentResponseP95,
      cost: { ...pc.cost, ...nc.cost },
    };
  });
  for (const pc of prev) {
    if (!nextIds.has(pc.id)) merged.push(pc);
  }
  // Sort by ``startedAtMs`` descending so the newest call always lands at
  // the top, regardless of whether it came from the snapshot or from
  // ``prev``. Without this, ``prev_only`` entries appended after the
  // snapshot block kept ordering non-deterministic (live row first only
  // when the snapshot already contained it). Calls without an
  // ``startedAtMs`` (rare — synthetic terminal rows before the canonical
  // write) sort to the end so they don't outrank a live call.
  merged.sort((a, b) => (b.startedAtMs ?? 0) - (a.startedAtMs ?? 0));
  // Cap to ``MAX_UI_CALLS`` so a long-lived session that has cycled
  // through more than 500 calls cannot grow the UI array unbounded.
  return merged.slice(0, MAX_UI_CALLS);
}

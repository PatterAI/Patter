/**
 * Data export utilities for the dashboard.
 */

import type { CallRecord } from './store';

/**
 * Convert call list to CSV string.
 *
 * Columns: call_id, caller, callee, direction, started_at, ended_at,
 * duration_s, cost_total, cost_stt, cost_tts, cost_llm, cost_telephony,
 * avg_latency_ms, turns_count, provider_mode
 */
export function callsToCsv(calls: CallRecord[]): string {
  const header = [
    'call_id', 'caller', 'callee', 'direction', 'started_at', 'ended_at',
    'duration_s', 'cost_total', 'cost_stt', 'cost_tts', 'cost_llm',
    'cost_telephony', 'avg_latency_ms', 'turns_count', 'provider_mode',
  ];

  const rows = [header.join(',')];

  for (const call of calls) {
    const m = (call.metrics || {}) as Record<string, unknown>;
    const cost = (m.cost as Record<string, number>) || {};
    const latencyAvg = (m.latency_avg as Record<string, number>) || {};
    const turns = m.turns;
    const turnsCount = Array.isArray(turns) ? turns.length : '';

    const row = [
      csvEscape(call.call_id || ''),
      csvEscape(call.caller || ''),
      csvEscape(call.callee || ''),
      csvEscape(call.direction || ''),
      call.started_at ?? '',
      call.ended_at ?? '',
      m.duration_seconds ?? '',
      cost.total ?? '',
      cost.stt ?? '',
      cost.tts ?? '',
      cost.llm ?? '',
      cost.telephony ?? '',
      latencyAvg.total_ms ?? '',
      turnsCount,
      m.provider_mode ?? '',
    ];

    rows.push(row.map(String).join(','));
  }

  return rows.join('\n') + '\n';
}

/**
 * Convert call list to JSON string.
 */
export function callsToJson(calls: CallRecord[]): string {
  return JSON.stringify(calls);
}

function csvEscape(value: string): string {
  if (value.includes(',') || value.includes('"') || value.includes('\n')) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

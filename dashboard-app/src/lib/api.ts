// HTTP client for the Patter dashboard read-only API.
//
// All requests go to relative URLs so the SPA works in both contexts:
//   - dev: Vite proxies /api/* to a local SDK server (see vite.config.ts).
//   - prod: the SDK serves the bundled SPA from the same origin.

export interface TranscriptEntry {
  readonly role: string;
  readonly text: string;
  readonly timestamp: number;
}

export interface CallCost {
  readonly stt?: number;
  readonly tts?: number;
  readonly llm?: number;
  readonly telephony?: number;
  readonly total?: number;
  readonly llm_cached_savings?: number;
}

export interface CallLatency {
  readonly stt_ms?: number;
  readonly llm_ms?: number;
  readonly tts_ms?: number;
  readonly total_ms?: number;
  readonly agent_response_ms?: number;
  readonly endpoint_ms?: number;
  readonly user_speech_duration_ms?: number;
}

export interface CallMetrics {
  readonly duration_seconds?: number;
  readonly provider_mode?: string;
  readonly telephony_provider?: string;
  readonly stt_provider?: string;
  readonly tts_provider?: string;
  readonly llm_provider?: string;
  readonly stt_model?: string;
  readonly tts_model?: string;
  readonly llm_model?: string;
  readonly cost?: CallCost;
  readonly latency_avg?: CallLatency;
  readonly latency_p50?: CallLatency;
  readonly latency_p95?: CallLatency;
  readonly latency_p99?: CallLatency;
  readonly turns?: readonly unknown[];
}

export interface CallRecord {
  readonly call_id: string;
  readonly caller: string;
  readonly callee: string;
  readonly direction: string;
  readonly started_at: number;
  readonly ended_at?: number;
  readonly status?: string;
  readonly transcript?: readonly TranscriptEntry[];
  readonly turns?: readonly unknown[];
  readonly metrics?: CallMetrics | null;
}

export interface CostBreakdown {
  readonly stt: number;
  readonly tts: number;
  readonly llm: number;
  readonly telephony: number;
}

export interface Aggregates {
  readonly total_calls: number;
  readonly total_cost: number;
  readonly avg_duration: number;
  readonly avg_latency_ms: number;
  readonly cost_breakdown: CostBreakdown;
  readonly active_calls: number;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const asString = (value: unknown): string =>
  typeof value === 'string' ? value : '';

const asNumber = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0;

const asOptionalNumber = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined;

const asOptionalString = (value: unknown): string | undefined =>
  typeof value === 'string' && value.length > 0 ? value : undefined;

function parseLatency(raw: unknown): CallLatency | undefined {
  if (!isObject(raw)) return undefined;
  return {
    stt_ms: asOptionalNumber(raw.stt_ms),
    llm_ms: asOptionalNumber(raw.llm_ms),
    tts_ms: asOptionalNumber(raw.tts_ms),
    total_ms: asOptionalNumber(raw.total_ms),
    agent_response_ms: asOptionalNumber(raw.agent_response_ms),
    endpoint_ms: asOptionalNumber(raw.endpoint_ms),
    user_speech_duration_ms: asOptionalNumber(raw.user_speech_duration_ms),
  };
}

function parseCost(raw: unknown): CallCost | undefined {
  if (!isObject(raw)) return undefined;
  return {
    stt: asOptionalNumber(raw.stt),
    tts: asOptionalNumber(raw.tts),
    llm: asOptionalNumber(raw.llm),
    telephony: asOptionalNumber(raw.telephony),
    total: asOptionalNumber(raw.total),
    llm_cached_savings: asOptionalNumber(raw.llm_cached_savings),
  };
}

function parseMetrics(raw: unknown): CallMetrics | null {
  if (!isObject(raw)) return null;
  const turnsRaw = raw.turns;
  return {
    duration_seconds: asOptionalNumber(raw.duration_seconds),
    provider_mode: asOptionalString(raw.provider_mode),
    telephony_provider: asOptionalString(raw.telephony_provider),
    stt_provider: asOptionalString(raw.stt_provider),
    tts_provider: asOptionalString(raw.tts_provider),
    llm_provider: asOptionalString(raw.llm_provider),
    stt_model: asOptionalString(raw.stt_model),
    tts_model: asOptionalString(raw.tts_model),
    llm_model: asOptionalString(raw.llm_model),
    cost: parseCost(raw.cost),
    latency_avg: parseLatency(raw.latency_avg),
    latency_p50: parseLatency(raw.latency_p50),
    latency_p95: parseLatency(raw.latency_p95),
    latency_p99: parseLatency(raw.latency_p99),
    turns: Array.isArray(turnsRaw) ? (turnsRaw as readonly unknown[]) : undefined,
  };
}

function parseTranscript(raw: unknown): readonly TranscriptEntry[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const entries: TranscriptEntry[] = [];
  for (const item of raw) {
    if (!isObject(item)) continue;
    entries.push({
      role: asString(item.role),
      text: asString(item.text),
      timestamp: asNumber(item.timestamp),
    });
  }
  return entries;
}

function parseCallRecord(raw: unknown): CallRecord | null {
  if (!isObject(raw)) return null;
  const callId = asString(raw.call_id);
  if (callId.length === 0) return null;
  const turnsRaw = raw.turns;
  return {
    call_id: callId,
    caller: asString(raw.caller),
    callee: asString(raw.callee),
    direction: asString(raw.direction),
    started_at: asNumber(raw.started_at),
    ended_at: asOptionalNumber(raw.ended_at),
    status: asOptionalString(raw.status),
    transcript: parseTranscript(raw.transcript),
    turns: Array.isArray(turnsRaw) ? (turnsRaw as readonly unknown[]) : undefined,
    metrics: parseMetrics(raw.metrics),
  };
}

function parseCallList(raw: unknown): CallRecord[] {
  if (!Array.isArray(raw)) return [];
  const out: CallRecord[] = [];
  for (const item of raw) {
    const record = parseCallRecord(item);
    if (record) out.push(record);
  }
  return out;
}

function parseCostBreakdown(raw: unknown): CostBreakdown {
  if (!isObject(raw)) {
    return { stt: 0, tts: 0, llm: 0, telephony: 0 };
  }
  return {
    stt: asNumber(raw.stt),
    tts: asNumber(raw.tts),
    llm: asNumber(raw.llm),
    telephony: asNumber(raw.telephony),
  };
}

function parseAggregates(raw: unknown): Aggregates {
  if (!isObject(raw)) {
    return {
      total_calls: 0,
      total_cost: 0,
      avg_duration: 0,
      avg_latency_ms: 0,
      cost_breakdown: { stt: 0, tts: 0, llm: 0, telephony: 0 },
      active_calls: 0,
    };
  }
  return {
    total_calls: asNumber(raw.total_calls),
    total_cost: asNumber(raw.total_cost),
    avg_duration: asNumber(raw.avg_duration),
    avg_latency_ms: asNumber(raw.avg_latency_ms),
    cost_breakdown: parseCostBreakdown(raw.cost_breakdown),
    active_calls: asNumber(raw.active_calls),
  };
}

async function getJson(path: string): Promise<unknown> {
  const response = await fetch(path, {
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    throw new Error(`Request to ${path} failed with status ${response.status}`);
  }
  return response.json() as Promise<unknown>;
}

export async function fetchCalls(
  limit: number = 50,
  offset: number = 0,
): Promise<CallRecord[]> {
  const url = `/api/dashboard/calls?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`;
  const body = await getJson(url);
  return parseCallList(body);
}

export async function fetchActiveCalls(): Promise<CallRecord[]> {
  const body = await getJson('/api/dashboard/active');
  return parseCallList(body);
}

export async function fetchAggregates(): Promise<Aggregates> {
  const body = await getJson('/api/dashboard/aggregates');
  return parseAggregates(body);
}

export async function fetchCall(callId: string): Promise<CallRecord | null> {
  const url = `/api/dashboard/calls/${encodeURIComponent(callId)}`;
  const response = await fetch(url, {
    headers: { Accept: 'application/json' },
  });
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(`Request to ${url} failed with status ${response.status}`);
  }
  const body = (await response.json()) as unknown;
  return parseCallRecord(body);
}

/**
 * Soft-delete a batch of calls. The server keeps the on-disk metadata +
 * transcript files as a backup; the dashboard view and aggregate metrics
 * exclude the ids going forward. Active calls are silently dropped from
 * the request. Idempotent.
 *
 * Returns the call_ids actually accepted (already-deleted / active ids
 * are filtered server-side).
 */
export async function deleteCalls(callIds: readonly string[]): Promise<string[]> {
  if (callIds.length === 0) return [];
  if (callIds.length === 1) {
    const url = `/api/dashboard/calls/${encodeURIComponent(callIds[0])}`;
    const response = await fetch(url, {
      method: 'DELETE',
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) {
      throw new Error(`DELETE ${url} failed with status ${response.status}`);
    }
    const body = (await response.json()) as { deleted?: unknown };
    return Array.isArray(body.deleted)
      ? (body.deleted as unknown[]).filter(
          (v): v is string => typeof v === 'string',
        )
      : [];
  }
  const response = await fetch('/api/dashboard/calls/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ call_ids: callIds }),
  });
  if (!response.ok) {
    throw new Error(
      `POST /api/dashboard/calls/delete failed with status ${response.status}`,
    );
  }
  const body = (await response.json()) as { deleted?: unknown };
  return Array.isArray(body.deleted)
    ? (body.deleted as unknown[]).filter(
        (v): v is string => typeof v === 'string',
      )
    : [];
}

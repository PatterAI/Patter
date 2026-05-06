// Pure mappers: SDK CallRecord -> UI Call / TranscriptTurn.
//
// The UI shapes (`Call`, `TranscriptTurn`) live in components written by a
// parallel agent. Until those land we declare local copies here. After
// integration, replace these locals with imports from
//   ../components/CallTable    (Call)
//   ../components/LiveCallPanel (TranscriptTurn)
// and remove the duplicate declarations below.
// TODO(integration): drop local interfaces once components export theirs.

import type { CallRecord } from './api';

export type CallStatus = 'live' | 'ended' | 'no-answer' | 'queued' | 'fail';
export type CallDirection = 'inbound' | 'outbound';
export type CallCarrier = 'twilio' | 'telnyx';

export interface CallCostUi {
  readonly telco?: number;
  readonly llm?: number;
  readonly sttTts?: number;
  readonly cached?: number;
  readonly total?: number;
}

export interface Call {
  readonly id: string;
  readonly status: CallStatus;
  readonly direction: CallDirection;
  readonly from: string;
  readonly to: string;
  readonly carrier: CallCarrier;
  readonly durationStart?: number;
  readonly duration?: number;
  readonly latencyP95?: number;
  readonly latencyP50?: number;
  readonly sttAvg?: number;
  readonly ttsAvg?: number;
  readonly cost: CallCostUi;
  readonly agent?: string;
  readonly model?: string;
  readonly transcriptKey?: string;
  readonly endedAgo?: number;
}

export interface TranscriptTurnLatency {
  readonly stt?: number;
  readonly llm?: number;
  readonly tts?: number;
  readonly total?: number;
}

export interface TranscriptTurn {
  readonly who: 'user' | 'bot' | 'tool';
  readonly txt?: string;
  readonly args?: Record<string, string | number>;
  readonly typing?: boolean;
  readonly lat?: TranscriptTurnLatency;
}

const LIVE_STATUSES = new Set(['in-progress', 'initiated']);

function mapStatus(raw: string | undefined): CallStatus {
  if (!raw) return 'ended';
  switch (raw) {
    case 'in-progress':
    case 'initiated':
      return 'live';
    case 'completed':
      return 'ended';
    case 'no-answer':
      return 'no-answer';
    case 'busy':
    case 'failed':
    case 'canceled':
    case 'webhook_error':
      return 'fail';
    default:
      return 'ended';
  }
}

function mapDirection(raw: string | undefined): CallDirection {
  return raw === 'outbound' ? 'outbound' : 'inbound';
}

function mapCarrier(provider: string | undefined): CallCarrier {
  if (typeof provider === 'string' && provider.toLowerCase().includes('telnyx')) {
    return 'telnyx';
  }
  return 'twilio';
}

function emptyToDash(value: string): string {
  return value.length === 0 ? '—' : value;
}

function buildAgentLabel(record: CallRecord): string | undefined {
  const mode = record.metrics?.provider_mode;
  if (!mode) return undefined;
  const llm = record.metrics?.llm_provider;
  if (mode.startsWith('pipeline') && llm) {
    return `${mode} · ${llm}`;
  }
  return mode;
}

function computeCost(record: CallRecord): CallCostUi {
  const cost = record.metrics?.cost;
  if (!cost) return {};
  const result: {
    telco?: number;
    llm?: number;
    sttTts?: number;
    cached?: number;
    total?: number;
  } = {};

  if (typeof cost.telephony === 'number') result.telco = cost.telephony;
  if (typeof cost.llm === 'number') result.llm = cost.llm;

  if (typeof cost.stt === 'number' || typeof cost.tts === 'number') {
    result.sttTts = (cost.stt ?? 0) + (cost.tts ?? 0);
  }

  // Only fall back to total when no granular breakdown is available.
  if (
    result.telco === undefined &&
    result.llm === undefined &&
    result.sttTts === undefined &&
    typeof cost.total === 'number'
  ) {
    result.total = cost.total;
  }

  return result;
}

function computeDuration(record: CallRecord, isLive: boolean): number | undefined {
  if (isLive) return undefined;
  const explicit = record.metrics?.duration_seconds;
  if (typeof explicit === 'number') return explicit;
  if (typeof record.ended_at === 'number' && typeof record.started_at === 'number') {
    return Math.max(0, record.ended_at - record.started_at);
  }
  return 0;
}

function computeEndedAgo(record: CallRecord): number | undefined {
  if (typeof record.ended_at !== 'number') return undefined;
  return Math.round(Date.now() / 1000 - record.ended_at);
}

export function toUiCall(record: CallRecord): Call {
  const status = mapStatus(record.status);
  const isLive = status === 'live' || (record.status !== undefined && LIVE_STATUSES.has(record.status));
  const latencyAvg = record.metrics?.latency_avg;
  const latencyP95 = record.metrics?.latency_p95;

  const call: Call = {
    id: record.call_id,
    status,
    direction: mapDirection(record.direction),
    from: emptyToDash(record.caller),
    to: emptyToDash(record.callee),
    carrier: mapCarrier(record.metrics?.telephony_provider),
    durationStart: isLive ? record.started_at * 1000 : undefined,
    duration: computeDuration(record, isLive),
    latencyP95: latencyP95?.total_ms ?? latencyAvg?.total_ms,
    latencyP50: latencyAvg?.total_ms,
    sttAvg: latencyAvg?.stt_ms,
    ttsAvg: latencyAvg?.tts_ms,
    cost: computeCost(record),
    agent: buildAgentLabel(record),
    model: record.metrics?.llm_provider,
    transcriptKey: record.call_id,
    endedAgo: computeEndedAgo(record),
  };
  return call;
}

export function toUiTranscript(record: CallRecord): TranscriptTurn[] {
  const transcript = record.transcript;
  if (!transcript) return [];
  const turns: TranscriptTurn[] = [];
  for (const entry of transcript) {
    const text = entry.text;
    switch (entry.role) {
      case 'user':
        turns.push({ who: 'user', txt: text });
        break;
      case 'assistant':
        turns.push({ who: 'bot', txt: text });
        break;
      case 'tool':
        turns.push({ who: 'tool', txt: text });
        break;
      default:
        turns.push({ who: 'bot', txt: text });
        break;
    }
  }
  return turns;
}

export type SparklineField = 'totalCalls' | 'latency' | 'spend';

function callTimestampSeconds(call: Call): number | undefined {
  if (typeof call.durationStart === 'number') {
    return Math.floor(call.durationStart / 1000);
  }
  if (typeof call.endedAgo === 'number') {
    return Math.floor(Date.now() / 1000 - call.endedAgo);
  }
  return undefined;
}

function callSpend(call: Call): number {
  const c = call.cost;
  const granular = (c.telco ?? 0) + (c.llm ?? 0) + (c.sttTts ?? 0);
  if (granular > 0) return granular;
  return c.total ?? 0;
}

function normalize(values: readonly number[]): number[] {
  const max = values.reduce((acc, v) => (v > acc ? v : acc), 0);
  if (max <= 0) return values.map(() => 0);
  return values.map((v) => Math.round((v / max) * 100));
}

export function bucketSparkline(
  calls: readonly Call[],
  field: SparklineField,
  buckets: number = 9,
): number[] {
  const safeBuckets = Math.max(1, Math.floor(buckets));
  const empty = new Array<number>(safeBuckets).fill(0);
  if (calls.length === 0) return empty;

  const stamps: number[] = [];
  for (const call of calls) {
    const ts = callTimestampSeconds(call);
    if (typeof ts === 'number') stamps.push(ts);
  }
  if (stamps.length === 0) return empty;

  const min = Math.min(...stamps);
  const max = Math.max(...stamps);
  const range = max - min;

  // Single-instant series: pile everything into the last bucket so the user
  // still sees a non-empty bar.
  if (range <= 0) {
    const sums = empty.slice();
    const counts = empty.slice();
    for (const call of calls) {
      const idx = safeBuckets - 1;
      if (field === 'totalCalls') {
        sums[idx] += 1;
      } else if (field === 'latency') {
        if (typeof call.latencyP95 === 'number') {
          sums[idx] += call.latencyP95;
          counts[idx] += 1;
        }
      } else {
        sums[idx] += callSpend(call);
      }
    }
    if (field === 'latency') {
      const avgs = sums.map((s, i) => (counts[i] > 0 ? s / counts[i] : 0));
      return normalize(avgs);
    }
    return normalize(sums);
  }

  const sums = empty.slice();
  const counts = empty.slice();
  const bucketSize = range / safeBuckets;

  for (const call of calls) {
    const ts = callTimestampSeconds(call);
    if (typeof ts !== 'number') continue;
    let idx = Math.floor((ts - min) / bucketSize);
    if (idx >= safeBuckets) idx = safeBuckets - 1;
    if (idx < 0) idx = 0;

    if (field === 'totalCalls') {
      sums[idx] += 1;
    } else if (field === 'latency') {
      if (typeof call.latencyP95 === 'number') {
        sums[idx] += call.latencyP95;
        counts[idx] += 1;
      }
    } else {
      sums[idx] += callSpend(call);
    }
  }

  if (field === 'latency') {
    const avgs = sums.map((s, i) => (counts[i] > 0 ? s / counts[i] : 0));
    return normalize(avgs);
  }
  return normalize(sums);
}

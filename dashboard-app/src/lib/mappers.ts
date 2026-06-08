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
/** Carrier metadata registry — the **single source of truth** for which
 *  carriers the dashboard knows about. {@link CallCarrier} is derived from
 *  these keys, {@link mapCarrier} accepts the same keys for runtime
 *  validation, and the UI components read ``label`` + ``dotClass`` from here.
 *  Adding a fourth carrier is one entry, full stop. */
export const CARRIERS = {
  twilio: { label: 'Twilio', dotClass: 'tw' },
  telnyx: { label: 'Telnyx', dotClass: 'tx' },
  plivo:  { label: 'Plivo',  dotClass: 'pl' },
} as const;

export type CallCarrier = keyof typeof CARRIERS;
export type CallMode = 'realtime' | 'pipeline' | 'convai' | 'unknown';

export interface CallCostUi {
  readonly telco?: number;
  readonly llm?: number;
  readonly stt?: number;
  readonly tts?: number;
  /**
   * @deprecated Sum of stt+tts kept for legacy consumers. New code reads
   * ``stt`` and ``tts`` separately so the dashboard can label each with the
   * actual provider (e.g. "Cartesia STT" / "ElevenLabs TTS").
   */
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
  readonly startedAtMs?: number;
  readonly durationStart?: number;
  readonly duration?: number;
  readonly latencyP95?: number;
  readonly latencyP50?: number;
  /** avg(llm_ms) across this call's turns — for the waterfall llm bar. */
  readonly llmAvg?: number;
  readonly sttAvg?: number;
  readonly ttsAvg?: number;
  /** Number of completed turns. p50/p95 are statistically meaningful only when this is >= 5. */
  readonly turnCount?: number;
  /** p50 of agent_response_ms (wait time after user stops speaking). */
  readonly agentResponseP50?: number;
  /** p95 of agent_response_ms — user-perceived latency outlier. */
  readonly agentResponseP95?: number;
  readonly cost: CallCostUi;
  readonly agent?: string;
  readonly model?: string;
  readonly mode?: CallMode;
  readonly sttProvider?: string;
  readonly ttsProvider?: string;
  /** Model identifier within the provider (e.g. "ink-whisper"). */
  readonly sttModel?: string;
  readonly ttsModel?: string;
  readonly llmModel?: string;
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

/** Runtime view of the carrier registry — derived so adding a new carrier
 *  to {@link CARRIERS} automatically widens what {@link mapCarrier} accepts. */
const KNOWN_CARRIERS: ReadonlySet<string> = new Set(Object.keys(CARRIERS));

function mapCarrier(provider: string | undefined): CallCarrier {
  // The SDK sets `telephonyProvider` to the literal carrier kind. Match
  // exactly so a future carrier value falls through to the fallback
  // instead of silently being labelled Twilio.
  if (typeof provider === 'string' && KNOWN_CARRIERS.has(provider)) {
    return provider as CallCarrier;
  }
  return 'twilio';
}

function mapMode(providerMode: string | undefined): CallMode {
  if (typeof providerMode !== 'string') return 'unknown';
  const m = providerMode.toLowerCase();
  if (m.includes('realtime')) return 'realtime';
  if (m.includes('convai')) return 'convai';
  if (m.includes('pipeline')) return 'pipeline';
  return 'unknown';
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
    stt?: number;
    tts?: number;
    sttTts?: number;
    cached?: number;
    total?: number;
  } = {};

  if (typeof cost.telephony === 'number') result.telco = cost.telephony;
  if (typeof cost.llm === 'number') result.llm = cost.llm;
  if (typeof cost.stt === 'number') result.stt = cost.stt;
  if (typeof cost.tts === 'number') result.tts = cost.tts;
  if (typeof cost.llm_cached_savings === 'number') {
    result.cached = cost.llm_cached_savings;
  }
  if (result.stt !== undefined || result.tts !== undefined) {
    result.sttTts = (result.stt ?? 0) + (result.tts ?? 0);
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
  const latencyP50 = record.metrics?.latency_p50;
  const latencyP95 = record.metrics?.latency_p95;
  // Total turn count from runtime metrics (preferred) — falls back to
  // the persisted transcript length for hydrated rows. Percentile boxes
  // are hidden in the UI when turnCount < 5 (statistical floor).
  const turnCount =
    (Array.isArray(record.metrics?.turns) ? record.metrics?.turns?.length : undefined) ??
    (Array.isArray(record.transcript) ? record.transcript.length : undefined);

  const call: Call = {
    id: record.call_id,
    status,
    direction: mapDirection(record.direction),
    from: emptyToDash(record.caller),
    to: emptyToDash(record.callee),
    carrier: mapCarrier(record.metrics?.telephony_provider),
    startedAtMs: typeof record.started_at === 'number' ? record.started_at * 1000 : undefined,
    durationStart: isLive ? record.started_at * 1000 : undefined,
    duration: computeDuration(record, isLive),
    // User-perceived "latency" on the dashboard means wait-time AFTER the
    // caller stops speaking (a.k.a. agent_response_ms / "response latency"
    // — the metric Pipecat, LiveKit, and OpenAI Realtime all surface).
    // Falls back to total_ms only for legacy rows that don't carry the
    // agent_response_ms breakdown — those rows over-state perceived
    // latency by the user-utterance duration but keep the table populated.
    latencyP95: latencyP95?.agent_response_ms ?? latencyP95?.total_ms ?? latencyAvg?.total_ms,
    latencyP50: latencyP50?.agent_response_ms ?? latencyP50?.total_ms ?? latencyAvg?.total_ms,
    sttAvg: latencyAvg?.stt_ms,
    ttsAvg: latencyAvg?.tts_ms,
    llmAvg: latencyAvg?.llm_ms,
    turnCount,
    agentResponseP50: latencyP50?.agent_response_ms,
    agentResponseP95: latencyP95?.agent_response_ms,
    cost: computeCost(record),
    agent: buildAgentLabel(record),
    model: record.metrics?.llm_provider,
    mode: mapMode(record.metrics?.provider_mode),
    sttProvider: record.metrics?.stt_provider,
    ttsProvider: record.metrics?.tts_provider,
    sttModel: record.metrics?.stt_model,
    ttsModel: record.metrics?.tts_model,
    llmModel: record.metrics?.llm_model,
    transcriptKey: record.call_id,
    endedAgo: computeEndedAgo(record),
  };
  return call;
}

/** Sort rank for transcript ordering within a single turn: user before
 *  assistant before tool. FIX-5 (issue #154): a late-arriving user line
 *  carrying the same ``turnIndex`` as its agent reply must render ABOVE it. */
function roleRank(role: string): number {
  switch (role) {
    case 'user':
      return 0;
    case 'assistant':
      return 1;
    default:
      return 2; // tool / unknown
  }
}

/**
 * Order transcript entries by (turnIndex, role) so live lines that arrive
 * out of order (the Realtime user transcript lands AFTER the agent reply)
 * still render user → assistant within each turn. FIX-5 (issue #154).
 *
 * Entries without a ``turnIndex`` (legacy / hydrated rows) keep their original
 * relative order — the sort is stable and only compares when BOTH entries
 * carry a numeric index. This keeps pre-#154 transcripts byte-identical.
 */
function sortTranscript(
  transcript: readonly NonNullable<CallRecord['transcript']>[number][],
): NonNullable<CallRecord['transcript']>[number][] {
  return transcript
    .map((entry, i) => ({ entry, i }))
    .sort((a, b) => {
      const ai = a.entry.turnIndex;
      const bi = b.entry.turnIndex;
      // Only reorder when both carry an index; otherwise preserve insertion
      // order (stable) so legacy rows are untouched.
      if (typeof ai === 'number' && typeof bi === 'number') {
        if (ai !== bi) return ai - bi;
        const rr = roleRank(a.entry.role) - roleRank(b.entry.role);
        if (rr !== 0) return rr;
      }
      return a.i - b.i;
    })
    .map(({ entry }) => entry);
}

export function toUiTranscript(record: CallRecord): TranscriptTurn[] {
  const transcript = record.transcript;
  if (transcript && transcript.length > 0) {
    const out: TranscriptTurn[] = [];
    for (const entry of sortTranscript([...transcript])) {
      const text = entry.text;
      switch (entry.role) {
        case 'user':
          out.push({ who: 'user', txt: text });
          break;
        case 'assistant':
          out.push({ who: 'bot', txt: text });
          break;
        case 'tool':
          out.push({ who: 'tool', txt: text });
          break;
        default:
          out.push({ who: 'bot', txt: text });
          break;
      }
    }
    return out;
  }
  // Fallback for live calls: completed calls expose ``transcript`` (a flat
  // array of {role,text}) but in-flight calls expose ``turns`` (the
  // ``TurnMetrics`` shape — one entry per round-trip with both
  // ``user_text`` and ``agent_text``). Without this branch the live
  // transcript pane is empty until the call ends. See dashboard BUG A.
  const turns = record.turns;
  if (!turns || turns.length === 0) return [];
  const out: TranscriptTurn[] = [];
  for (const raw of turns) {
    if (typeof raw !== 'object' || raw === null) continue;
    const turn = raw as { user_text?: unknown; agent_text?: unknown };
    const userText = typeof turn.user_text === 'string' ? turn.user_text : '';
    const agentText = typeof turn.agent_text === 'string' ? turn.agent_text : '';
    if (userText.length > 0) out.push({ who: 'user', txt: userText });
    if (agentText.length > 0 && agentText !== '[interrupted]') {
      out.push({ who: 'bot', txt: agentText });
    }
  }
  return out;
}

export type SparklineField = 'totalCalls' | 'latency' | 'spend';

export type RangeKey = '1h' | '24h' | '7d' | 'All';

/**
 * A bucket strategy says: "bucket N slots of size S, ending at T, starting
 * at F = T - N*S". Sizes are aligned to natural boundaries (5-minute
 * marks for 1h, hour marks for 24h, local-midnight for 7d) so the tooltip
 * ranges read as expected (``11:00 → 12:00`` rather than ``11:39 →
 * 12:33``).
 */
export interface BucketStrategy {
  readonly count: number;
  readonly bucketSizeMs: number;
  readonly window: TimeWindow;
}

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

export function bucketStrategyForRange(
  range: RangeKey,
  now: number = Date.now(),
): BucketStrategy {
  switch (range) {
    case '1h': {
      // 12 × 5-min buckets, aligned to the next 5-min mark so the bar
      // boundaries read as 11:35 → 11:40 instead of 11:37 → 11:42.
      const size = 5 * MIN;
      const toMs = Math.ceil(now / size) * size;
      const fromMs = toMs - 12 * size;
      return { count: 12, bucketSizeMs: size, window: { fromMs, toMs } };
    }
    case '24h': {
      // 24 × 1-hour buckets aligned to the top of the hour.
      const size = HOUR;
      const toMs = Math.ceil(now / size) * size;
      const fromMs = toMs - 24 * size;
      return { count: 24, bucketSizeMs: size, window: { fromMs, toMs } };
    }
    case '7d': {
      // 7 × 1-day buckets aligned to local midnight. ``toMs`` is the
      // upcoming midnight so today's calls fall into the last bucket.
      const today = new Date(now);
      today.setHours(0, 0, 0, 0);
      const tomorrowMidnight = today.getTime() + DAY;
      const fromMs = tomorrowMidnight - 7 * DAY;
      return {
        count: 7,
        bucketSizeMs: DAY,
        window: { fromMs, toMs: tomorrowMidnight },
      };
    }
    case 'All':
    default:
      return {
        count: 9,
        bucketSizeMs: 0, // computeSparkline will derive from data extents
        window: { fromMs: 0, toMs: now },
      };
  }
}

/**
 * Backwards-compatible wrapper — returns just the time window. Prefer
 * ``bucketStrategyForRange`` when you also need the bucket count/size.
 */
export function rangeToWindow(range: RangeKey, now: number = Date.now()): TimeWindow {
  return bucketStrategyForRange(range, now).window;
}

export function filterCallsInWindow(calls: readonly Call[], window: TimeWindow): Call[] {
  const { fromMs, toMs } = window;
  return calls.filter((call) => {
    const ts = callTimestampMs(call);
    if (typeof ts !== 'number') return false;
    return ts >= fromMs && ts <= toMs;
  });
}

/** Time window in milliseconds — open intervals coerce both ends to "now". */
export interface TimeWindow {
  readonly fromMs: number;
  readonly toMs: number;
}

function callTimestampMs(call: Call): number | undefined {
  if (typeof call.startedAtMs === 'number') return call.startedAtMs;
  if (typeof call.durationStart === 'number') return call.durationStart;
  if (typeof call.endedAgo === 'number') return Date.now() - call.endedAgo * 1000;
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

export interface SparklineResult {
  /** Per-bucket bar heights, normalized 0-100 (tallest bar = 100). */
  readonly heights: number[];
  /** Calls assigned to each bucket — same length as ``heights``. */
  readonly buckets: ReadonlyArray<readonly Call[]>;
  /** Resolved window the buckets were computed against. */
  readonly window: TimeWindow;
  /** Width of one bucket in milliseconds. */
  readonly bucketSizeMs: number;
}

/**
 * Bucket calls into N equal time slots and report both bar heights AND
 * the calls that fell into each bucket — callers use the latter to drive
 * tooltips and click-to-select interactions on the sparkline.
 *
 * Pass a fully-resolved ``strategy`` from ``bucketStrategyForRange`` to
 * get bucket boundaries aligned to natural marks (5 min / 1 h / 1 day).
 * Falling back to the legacy signature (count + optional window) bucket
 * sizes are derived from the window span and sit on arbitrary boundaries.
 */
export function computeSparkline(
  calls: readonly Call[],
  field: SparklineField,
  strategyOrCount: BucketStrategy | number = 9,
  windowParam?: TimeWindow,
): SparklineResult {
  const isStrategy = typeof strategyOrCount === 'object';
  const requestedCount = isStrategy ? strategyOrCount.count : strategyOrCount;
  const safeBuckets = Math.max(1, Math.floor(requestedCount));
  const strategyWindow = isStrategy ? strategyOrCount.window : windowParam;
  const strategySize = isStrategy ? strategyOrCount.bucketSizeMs : 0;

  let fromMs: number;
  let toMs: number;
  if (strategyWindow) {
    fromMs = strategyWindow.fromMs;
    toMs = strategyWindow.toMs;
  } else {
    const stamps: number[] = [];
    for (const call of calls) {
      const ts = callTimestampMs(call);
      if (typeof ts === 'number') stamps.push(ts);
    }
    if (stamps.length === 0) {
      const now = Date.now();
      return {
        heights: new Array<number>(safeBuckets).fill(0),
        buckets: new Array(safeBuckets).fill(null).map(() => []),
        window: { fromMs: now, toMs: now },
        bucketSizeMs: 0,
      };
    }
    fromMs = Math.min(...stamps);
    toMs = Math.max(...stamps);
  }

  const span = Math.max(1, toMs - fromMs);
  const bucketSizeMs = strategySize > 0 ? strategySize : span / safeBuckets;
  const buckets: Call[][] = new Array(safeBuckets).fill(null).map(() => []);

  const sums = new Array<number>(safeBuckets).fill(0);
  const counts = new Array<number>(safeBuckets).fill(0);

  for (const call of calls) {
    const ts = callTimestampMs(call);
    if (typeof ts !== 'number') continue;
    if (ts < fromMs || ts > toMs) continue;
    let idx = Math.floor((ts - fromMs) / bucketSizeMs);
    if (idx >= safeBuckets) idx = safeBuckets - 1;
    if (idx < 0) idx = 0;

    buckets[idx].push(call);

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

  const rawValues =
    field === 'latency'
      ? sums.map((s, i) => (counts[i] > 0 ? s / counts[i] : 0))
      : sums;

  return {
    heights: normalize(rawValues),
    buckets,
    window: { fromMs, toMs },
    bucketSizeMs,
  };
}

/**
 * Bucket calls across an explicit time window into N=9 equal slots, then
 * normalize so the tallest bar is 100. When ``window`` is omitted the
 * function falls back to the data's own min/max — useful for "All" range.
 *
 * Preserved for backwards-compatible callers that only need bar heights.
 * Prefer ``computeSparkline`` for anything that drives interactivity.
 */
export function bucketSparkline(
  calls: readonly Call[],
  field: SparklineField,
  buckets: number = 9,
  window?: TimeWindow,
): number[] {
  const safeBuckets = Math.max(1, Math.floor(buckets));
  const empty = new Array<number>(safeBuckets).fill(0);

  let fromMs: number;
  let toMs: number;
  if (window) {
    fromMs = window.fromMs;
    toMs = window.toMs;
  } else {
    if (calls.length === 0) return empty;
    const stamps: number[] = [];
    for (const call of calls) {
      const ts = callTimestampMs(call);
      if (typeof ts === 'number') stamps.push(ts);
    }
    if (stamps.length === 0) return empty;
    fromMs = Math.min(...stamps);
    toMs = Math.max(...stamps);
  }

  const span = Math.max(1, toMs - fromMs);
  const bucketSize = span / safeBuckets;

  const sums = empty.slice();
  const counts = empty.slice();

  for (const call of calls) {
    const ts = callTimestampMs(call);
    if (typeof ts !== 'number') continue;
    if (ts < fromMs || ts > toMs) continue;
    let idx = Math.floor((ts - fromMs) / bucketSize);
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

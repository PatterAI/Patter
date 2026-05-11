import { useState } from 'react';
import type { Call } from './CallTable';
import { fmtCostUSD } from './format';

export interface MetricBucket {
  /** Bar height 0-100. */
  readonly height: number;
  /** Calls that fell into this bucket. May be empty. */
  readonly calls: readonly Call[];
  /** Bucket window start (ms epoch). */
  readonly fromMs: number;
  /** Bucket window end (ms epoch). */
  readonly toMs: number;
}

/**
 * Tooltip kind drives the headline aggregate shown above the per-call
 * sample list:
 *   - ``count``   →  "N CALLS"
 *   - ``latency`` →  "AVG LATENCY <P95-mean> MS"
 *   - ``spend``   →  "TOTAL COST $<sum>"
 * The rendered list of recent calls in the bucket is shared across all
 * three so the user can still drill down into a specific call.
 */
export type MetricKind = 'count' | 'latency' | 'spend';

export interface MetricProps {
  label: string;
  value: string | number;
  unit?: string;
  delta?: string;
  deltaTone?: 'up' | 'dn';
  /** Plain bar heights — used when no per-bucket detail is available. */
  spark: number[];
  /** Optional rich bucket data — enables hover tooltip + click-to-select. */
  buckets?: readonly MetricBucket[];
  /** Called when the user clicks a bar that contains at least one call. */
  onSelectCall?: (callId: string) => void;
  /**
   * Which aggregate the tooltip headline reports for this card. Defaults
   * to ``count`` so existing callers (no kind passed) keep their previous
   * "N calls" label.
   */
  kind?: MetricKind;
  peach?: boolean;
  footer?: string;
  badge?: boolean;
}

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

function fmtClock(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtDay(ms: number): string {
  return new Date(ms).toLocaleDateString([], {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
}

function fmtDateTime(ms: number): string {
  return new Date(ms).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function bucketRange(bucket: MetricBucket): string {
  const span = bucket.toMs - bucket.fromMs;
  // 1-day bucket: render as "Mon, May 6"
  if (span >= DAY_MS - MIN_TOLERANCE) {
    return fmtDay(bucket.fromMs);
  }
  // ≥ 1-hour bucket: render as "11:00 → 12:00"
  if (span >= HOUR_MS) {
    return `${fmtClock(bucket.fromMs)} → ${fmtClock(bucket.toMs)}`;
  }
  // sub-hour bucket (5-min slots): render as "11:35 → 11:40"
  if (span >= 60 * 1000) {
    return `${fmtClock(bucket.fromMs)} → ${fmtClock(bucket.toMs)}`;
  }
  // multi-day bucket (All view with sparse data): include date
  return `${fmtDateTime(bucket.fromMs)} → ${fmtDateTime(bucket.toMs)}`;
}

const MIN_TOLERANCE = 5_000; // 5 s slack for floating-point bucket spans

function callCost(c: Call): number {
  return (
    c.cost.total ?? (c.cost.telco ?? 0) + (c.cost.llm ?? 0) + (c.cost.sttTts ?? 0)
  );
}

function newestCallId(bucket: MetricBucket): string | undefined {
  if (bucket.calls.length === 0) return undefined;
  const sorted = [...bucket.calls].sort(
    (a, b) => (b.startedAtMs ?? 0) - (a.startedAtMs ?? 0),
  );
  return sorted[0]?.id;
}

/**
 * Compute the per-bucket headline for the sparkline tooltip. Returns the
 * uppercase label (e.g. "AVG LATENCY") and the formatted value (e.g.
 * "3048 ms") for the chosen ``kind``. Cleanly separated from the
 * presentation so the same card-level numbers shown on the metric tile
 * itself can be re-used by tests.
 */
export function bucketHeadline(
  bucket: MetricBucket,
  kind: MetricKind,
): { label: string; value: string } {
  const calls = bucket.calls;
  const count = calls.length;
  if (kind === 'spend') {
    const sum = calls.reduce((acc, c) => acc + callCost(c), 0);
    return { label: 'TOTAL COST', value: fmtCostUSD(sum) };
  }
  if (kind === 'latency') {
    const withLat = calls.filter((c) => typeof c.latencyP95 === 'number');
    const avg =
      withLat.length > 0
        ? Math.round(
            withLat.reduce((acc, c) => acc + (c.latencyP95 ?? 0), 0) /
              withLat.length,
          )
        : 0;
    return { label: 'AVG LATENCY', value: `${avg} ms` };
  }
  return { label: count === 1 ? 'CALL' : 'CALLS', value: `${count}` };
}

interface SparkTooltipProps {
  bucket: MetricBucket;
  kind: MetricKind;
}

function SparkTooltip({ bucket, kind }: SparkTooltipProps) {
  const range = bucketRange(bucket);
  const count = bucket.calls.length;

  if (count === 0) {
    return (
      <div className="spark-tooltip">
        <div className="spark-tooltip-range">{range}</div>
        <div className="spark-tooltip-empty">no calls</div>
      </div>
    );
  }

  const headline = bucketHeadline(bucket, kind);
  const sample = bucket.calls.slice(0, 4);
  return (
    <div className="spark-tooltip">
      <div className="spark-tooltip-range">{range}</div>
      <div className="spark-tooltip-headline">
        <span className="spark-tooltip-headline-l">{headline.label}</span>
        <span className="spark-tooltip-headline-v">{headline.value}</span>
      </div>
      <ul className="spark-tooltip-list">
        {sample.map((c) => {
          const num = c.direction === 'inbound' ? c.from : c.to;
          return (
            <li key={c.id}>
              <span className="num">{num}</span>
              <span className="status">{c.status}</span>
              <span className="cost">{fmtCostUSD(callCost(c))}</span>
            </li>
          );
        })}
      </ul>
      {count > sample.length && (
        <div className="spark-tooltip-more">+{count - sample.length} more</div>
      )}
    </div>
  );
}

interface SparkBarProps {
  bucket: MetricBucket | undefined;
  height: number;
  interactive: boolean;
  kind: MetricKind;
  onSelect?: (id: string) => void;
}

function SparkBar({ bucket, height, interactive, kind, onSelect }: SparkBarProps) {
  const [hovered, setHovered] = useState(false);
  const hasCalls = !!bucket && bucket.calls.length > 0;

  if (!interactive || !bucket) {
    return <span className="spark-bar-static" style={{ height: height + '%' }} />;
  }

  return (
    <div
      className="spark-bar-wrap"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <button
        type="button"
        className={'spark-bar' + (hasCalls ? '' : ' empty')}
        style={{ height: height + '%' }}
        disabled={!hasCalls}
        onClick={() => {
          if (!hasCalls) return;
          const id = newestCallId(bucket);
          if (id && onSelect) onSelect(id);
        }}
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        aria-label={`${bucket.calls.length} calls in ${bucketRange(bucket)}`}
      />
      {hovered && <SparkTooltip bucket={bucket} kind={kind} />}
    </div>
  );
}

export function Metric({
  label,
  value,
  unit,
  delta,
  deltaTone,
  spark,
  buckets,
  onSelectCall,
  kind = 'count',
  peach,
  footer,
  badge,
}: MetricProps) {
  const interactive = !!buckets && !!onSelectCall;

  return (
    <div className={'metric' + (peach ? ' peach' : '')}>
      <div className="lbl">
        <span>{label}</span>
        {badge && <span className="badge-now">LIVE</span>}
      </div>
      <div className="val">
        {value}
        {unit && <span className="unit"> {unit}</span>}
      </div>
      {delta && <div className={'delta ' + (deltaTone || '')}>{delta}</div>}
      {footer && <div className="delta">{footer}</div>}
      <div className="spark">
        {spark.map((h, i) => (
          <SparkBar
            key={i}
            bucket={buckets?.[i]}
            height={h}
            interactive={interactive}
            kind={kind}
            onSelect={onSelectCall}
          />
        ))}
      </div>
    </div>
  );
}

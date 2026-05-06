import { useState } from 'react';
import type { Call } from './CallTable';

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

interface SparkTooltipProps {
  bucket: MetricBucket;
}

function SparkTooltip({ bucket }: SparkTooltipProps) {
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

  const sample = bucket.calls.slice(0, 4);
  return (
    <div className="spark-tooltip">
      <div className="spark-tooltip-range">{range}</div>
      <div className="spark-tooltip-count">
        {count} call{count === 1 ? '' : 's'}
      </div>
      <ul className="spark-tooltip-list">
        {sample.map((c) => {
          const num = c.direction === 'inbound' ? c.from : c.to;
          return (
            <li key={c.id}>
              <span className="num">{num}</span>
              <span className="status">{c.status}</span>
              <span className="cost">${callCost(c).toFixed(3)}</span>
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
  onSelect?: (id: string) => void;
}

function SparkBar({ bucket, height, interactive, onSelect }: SparkBarProps) {
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
      {hovered && <SparkTooltip bucket={bucket} />}
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
            onSelect={onSelectCall}
          />
        ))}
      </div>
    </div>
  );
}

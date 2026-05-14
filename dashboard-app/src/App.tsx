import { useEffect, useMemo, useState } from 'react';
import { Topbar } from './components/Topbar';
import { PageHeader } from './components/PageHeader';
import { Metric, type MetricBucket } from './components/Metric';
import { CallTable, type Call } from './components/CallTable';
import { fmtCostUSD } from './components/format';
import { LiveCallPanel } from './components/LiveCallPanel';
import { MetricsPanel } from './components/MetricsPanel';
import { useDashboardData } from './hooks/useDashboardData';
import { useTranscript } from './hooks/useTranscript';
import { useUiPrefs } from './hooks/useUiPrefs';
import { deleteCalls } from './lib/api';
import {
  bucketStrategyForRange,
  computeSparkline,
  filterCallsInWindow,
  type RangeKey,
  type SparklineResult,
} from './lib/mappers';

const SDK_VERSION = '0.6.0';
const RANGE_LABEL: Record<RangeKey, string> = {
  '1h': '1h',
  '24h': '24h',
  '7d': '7d',
  All: 'all-time',
};

function avgP95(calls: readonly Call[]): number {
  const withLat = calls.filter((c) => typeof c.latencyP95 === 'number');
  if (withLat.length === 0) return 0;
  const total = withLat.reduce((s, c) => s + (c.latencyP95 ?? 0), 0);
  return Math.round(total / withLat.length);
}

function totalSpend(calls: readonly Call[]): number {
  return calls.reduce((s, c) => {
    if (typeof c.cost.total === 'number') return s + c.cost.total;
    const granular = (c.cost.telco ?? 0) + (c.cost.llm ?? 0) + (c.cost.sttTts ?? 0);
    return s + granular;
  }, 0);
}

/**
 * Patter-side number for the topbar pill — derived from the most recent call
 * (live preferred, otherwise newest ended). Inbound calls expose ``to``,
 * outbound calls expose ``from``. Falls back to an empty string when no
 * call data is available.
 */
function pickPhoneNumber(calls: readonly Call[]): string {
  const live = calls.find((c) => c.status === 'live');
  const ref = live ?? calls[0];
  if (!ref) return '';
  const num = ref.direction === 'inbound' ? ref.to : ref.from;
  return num && num !== '—' ? num : '';
}

export function App() {
  const { calls, aggregates, isStreaming, error, refresh, removeCallsLocal } =
    useDashboardData();
  const { revealed, dark, toggleRevealed, toggleDark } = useUiPrefs();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [range, setRange] = useState<RangeKey>('24h');
  const [recording, setRecording] = useState(true);
  const [muted, setMuted] = useState(false);

  // Resolve bucket strategy + window for the active range. Bucket counts
  // and sizes are aligned to natural boundaries so tooltip ranges read as
  // "11:00 → 12:00" rather than "11:39 → 12:33".
  const strategy = useMemo(() => bucketStrategyForRange(range), [range]);
  const timeWindow = strategy.window;

  // Calls that fall inside the selected range. Live calls are always
  // included regardless of the range filter — the user always wants to see
  // what's happening right now.
  const filteredCalls = useMemo(() => {
    if (range === 'All') return calls;
    const inWindow = new Set(filterCallsInWindow(calls, timeWindow).map((c) => c.id));
    return calls.filter((c) => c.status === 'live' || inWindow.has(c.id));
  }, [calls, range, timeWindow]);

  // Auto-select first live call when none is selected
  useEffect(() => {
    if (selectedId !== null) return;
    const liveCall = filteredCalls.find((c) => c.status === 'live') ?? filteredCalls[0];
    if (liveCall) setSelectedId(liveCall.id);
  }, [filteredCalls, selectedId]);

  // Drop selection if the selected call disappeared from the visible list
  useEffect(() => {
    if (selectedId === null) return;
    if (!filteredCalls.some((c) => c.id === selectedId)) setSelectedId(null);
  }, [filteredCalls, selectedId]);

  // ⇧K / ⌘K focuses the search input
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const isShortcut =
        (e.shiftKey && e.key.toLowerCase() === 'k') ||
        (e.metaKey && e.key.toLowerCase() === 'k');
      if (!isShortcut) return;
      e.preventDefault();
      const el = document.querySelector<HTMLInputElement>('.panel-h .search input');
      el?.focus();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const selected = useMemo(
    () => filteredCalls.find((c) => c.id === selectedId) ?? null,
    [filteredCalls, selectedId],
  );
  const isSelectedLive = selected?.status === 'live';
  const transcript = useTranscript(selected?.id ?? null, isSelectedLive);

  const liveCount = useMemo(() => calls.filter((c) => c.status === 'live').length, [calls]);
  const inbound = useMemo(
    () => calls.filter((c) => c.status === 'live' && c.direction === 'inbound').length,
    [calls],
  );
  const outbound = liveCount - inbound;

  // Headline counters reflect the active range (Total / Latency / Spend),
  // except "Active now" which is always the current live count.
  const totalCount = filteredCalls.length;
  const rangeAvgP95 = avgP95(filteredCalls) || aggregates?.avg_latency_ms || 0;
  const rangeSpend = totalSpend(filteredCalls) || aggregates?.total_cost || 0;
  const phoneNumber = pickPhoneNumber(calls);

  const sparkTotalCalls = useMemo(
    () => computeSparkline(filteredCalls, 'totalCalls', strategy),
    [filteredCalls, strategy],
  );
  const sparkLatency = useMemo(
    () => computeSparkline(filteredCalls, 'latency', strategy),
    [filteredCalls, strategy],
  );
  const sparkSpend = useMemo(
    () => computeSparkline(filteredCalls, 'spend', strategy),
    [filteredCalls, strategy],
  );
  const sparkLive = useMemo(() => {
    const liveCalls = calls.filter((c) => c.status === 'live');
    return computeSparkline(liveCalls, 'totalCalls', strategy);
  }, [calls, strategy]);

  const toBuckets = (s: SparklineResult): MetricBucket[] =>
    s.heights.map((h, i) => ({
      height: h,
      calls: s.buckets[i],
      fromMs: s.window.fromMs + i * s.bucketSizeMs,
      toMs: s.window.fromMs + (i + 1) * s.bucketSizeMs,
    }));

  const handleEnd = () => {
    if (!selected) return;
    // TODO: wire to POST /api/v1/calls/:id/hangup. For now refresh so the
    //  status will follow once the SDK reports the hangup.
    refresh().catch(() => undefined);
  };

  const handleDeleteCalls = async (ids: readonly string[]): Promise<void> => {
    if (ids.length === 0) return;
    // Optimistic local removal so the rows vanish immediately. The SSE
    // ``calls_deleted`` event triggers a refresh shortly after; if the
    // server rejects an id (race with a status callback), it re-appears.
    removeCallsLocal(ids);
    if (ids.includes(selectedId ?? '')) setSelectedId(null);
    try {
      await deleteCalls(ids);
    } catch {
      // Pull the authoritative snapshot back if the call failed so the UI
      // is consistent with the server's view.
      await refresh().catch(() => undefined);
    }
  };

  return (
    <>
      <Topbar
        liveCount={liveCount}
        todayCount={totalCount}
        phoneNumber={phoneNumber}
        sdkVersion={SDK_VERSION}
        revealed={revealed}
        dark={dark}
        onToggleRevealed={toggleRevealed}
        onToggleDark={toggleDark}
      />
      <div className="page">
        <PageHeader range={range} setRange={(r) => setRange(r as RangeKey)} />

        <div className="metrics">
          <Metric
            label={`Calls · ${RANGE_LABEL[range]}`}
            value={totalCount}
            spark={sparkTotalCalls.heights}
            buckets={toBuckets(sparkTotalCalls)}
            onSelectCall={setSelectedId}
            kind="count"
          />
          <Metric
            label="Avg latency p95"
            value={rangeAvgP95 || 0}
            unit="ms"
            spark={sparkLatency.heights}
            buckets={toBuckets(sparkLatency)}
            onSelectCall={setSelectedId}
            kind="latency"
          />
          <Metric
            label={`Spend · ${RANGE_LABEL[range]}`}
            value={fmtCostUSD(rangeSpend)}
            spark={sparkSpend.heights}
            buckets={toBuckets(sparkSpend)}
            onSelectCall={setSelectedId}
            kind="spend"
          />
          <Metric
            label="Active now"
            value={liveCount}
            peach
            badge
            footer={`${inbound} inbound · ${outbound} outbound`}
            spark={sparkLive.heights}
            buckets={toBuckets(sparkLive)}
            onSelectCall={setSelectedId}
            kind="count"
          />
        </div>

        <div className="split">
          <CallTable
            calls={filteredCalls}
            selectedId={selectedId}
            onSelect={setSelectedId}
            newId={null}
            search={search}
            setSearch={setSearch}
            onDeleteCalls={handleDeleteCalls}
            revealed={revealed}
          />
          <div className="rr">
            <LiveCallPanel
              call={selected}
              transcript={transcript}
              onEnd={handleEnd}
              recording={recording}
              setRecording={setRecording}
              muted={muted}
              setMuted={setMuted}
              revealed={revealed}
            />
            <MetricsPanel call={selected} />
          </div>
        </div>

        <div className="statusbar">
          <div className="group">
            <span className={isStreaming ? 'green' : ''}>
              {isStreaming ? 'streaming · sse' : error ? `error · ${error}` : 'idle'}
            </span>
            <span>SDK · {SDK_VERSION}</span>
          </div>
          <div className="group">
            <span>
              {liveCount} live · {totalCount} {RANGE_LABEL[range]}
            </span>
          </div>
        </div>
      </div>
    </>
  );
}

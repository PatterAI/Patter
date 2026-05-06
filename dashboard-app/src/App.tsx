import { useEffect, useMemo, useState } from 'react';
import { Topbar } from './components/Topbar';
import { PageHeader } from './components/PageHeader';
import { Metric } from './components/Metric';
import { CallTable, type Call } from './components/CallTable';
import { LiveCallPanel } from './components/LiveCallPanel';
import { LatencyPanel } from './components/LatencyPanel';
import { CostPanel } from './components/CostPanel';
import { useDashboardData } from './hooks/useDashboardData';
import { useTranscript } from './hooks/useTranscript';
import { bucketSparkline } from './lib/mappers';

const SDK_VERSION = '0.6.0';

function avgLiveP95(calls: readonly Call[]): number {
  const live = calls.filter((c) => c.status === 'live' && typeof c.latencyP95 === 'number');
  if (live.length === 0) return 0;
  const total = live.reduce((s, c) => s + (c.latencyP95 ?? 0), 0);
  return Math.round(total / live.length);
}

function totalSpend(calls: readonly Call[]): number {
  return calls.reduce((s, c) => {
    if (typeof c.cost.total === 'number') return s + c.cost.total;
    const granular = (c.cost.telco ?? 0) + (c.cost.llm ?? 0) + (c.cost.sttTts ?? 0);
    return s + granular;
  }, 0);
}

function pickPhoneNumber(calls: readonly Call[]): string {
  const live = calls.find((c) => c.status === 'live');
  if (!live) return '—';
  return live.direction === 'inbound' ? live.to : live.from;
}

export function App() {
  const { calls, aggregates, isStreaming, error, refresh } = useDashboardData();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [range, setRange] = useState('24h');
  const [recording, setRecording] = useState(true);
  const [muted, setMuted] = useState(false);

  // Auto-select first live call when none is selected
  useEffect(() => {
    if (selectedId !== null) return;
    const liveCall = calls.find((c) => c.status === 'live') ?? calls[0];
    if (liveCall) setSelectedId(liveCall.id);
  }, [calls, selectedId]);

  // Drop selection if the selected call disappeared from the list
  useEffect(() => {
    if (selectedId === null) return;
    if (!calls.some((c) => c.id === selectedId)) setSelectedId(null);
  }, [calls, selectedId]);

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
    () => calls.find((c) => c.id === selectedId) ?? null,
    [calls, selectedId],
  );
  const isSelectedLive = selected?.status === 'live';
  const transcript = useTranscript(selected?.id ?? null, isSelectedLive);

  const liveCount = useMemo(() => calls.filter((c) => c.status === 'live').length, [calls]);
  const inbound = useMemo(
    () => calls.filter((c) => c.status === 'live' && c.direction === 'inbound').length,
    [calls],
  );
  const outbound = liveCount - inbound;

  const todayCount = aggregates?.total_calls ?? calls.length;
  const avgP95 = avgLiveP95(calls) || aggregates?.avg_latency_ms || 0;
  const spend = totalSpend(calls) || aggregates?.total_cost || 0;
  const phoneNumber = pickPhoneNumber(calls);

  const sparkTotalCalls = useMemo(() => bucketSparkline(calls, 'totalCalls'), [calls]);
  const sparkLatency = useMemo(() => bucketSparkline(calls, 'latency'), [calls]);
  const sparkSpend = useMemo(() => bucketSparkline(calls, 'spend'), [calls]);
  const sparkLive = useMemo(() => {
    const liveCalls = calls.filter((c) => c.status === 'live');
    return bucketSparkline(liveCalls, 'totalCalls');
  }, [calls]);

  const handlePlace = () => {
    // TODO: wire to outbound POST /api/v1/calls when the dashboard supports it.
    window.alert('Place call: outbound dialer not yet wired in the dashboard.');
  };

  const handleEnd = () => {
    if (!selected) return;
    // TODO: wire to POST /api/v1/calls/:id/hangup. For now refresh so the
    //  status will follow once the SDK reports the hangup.
    refresh().catch(() => undefined);
  };

  return (
    <>
      <Topbar
        liveCount={liveCount}
        todayCount={todayCount}
        phoneNumber={phoneNumber}
        sdkVersion={SDK_VERSION}
      />
      <div className="page">
        <PageHeader range={range} setRange={setRange} onPlace={handlePlace} />

        <div className="metrics">
          <Metric
            label="Total calls"
            value={todayCount}
            spark={sparkTotalCalls}
          />
          <Metric
            label="Avg latency p95"
            value={avgP95 || 0}
            unit="ms"
            spark={sparkLatency}
          />
          <Metric
            label="Spend"
            value={`$${spend.toFixed(2)}`}
            spark={sparkSpend}
          />
          <Metric
            label="Active now"
            value={liveCount}
            peach
            badge
            footer={`${inbound} inbound · ${outbound} outbound`}
            spark={sparkLive}
          />
        </div>

        <div className="split">
          <CallTable
            calls={calls}
            selectedId={selectedId}
            onSelect={setSelectedId}
            newId={null}
            search={search}
            setSearch={setSearch}
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
            />
            <LatencyPanel call={selected} />
            <CostPanel call={selected} />
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
            <span>{liveCount} live · {todayCount} today</span>
          </div>
        </div>
      </div>
    </>
  );
}

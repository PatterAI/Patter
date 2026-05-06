import { useMemo } from 'react';
import { fmtDuration, fmtPhone } from './format';
import { IconArrowDown, IconArrowUp, IconSearch } from './icons';

export interface CallCost {
  telco?: number;
  llm?: number;
  sttTts?: number;
  cached?: number;
  total?: number;
}

export type CallMode = 'realtime' | 'pipeline' | 'convai' | 'unknown';

export interface Call {
  id: string;
  status: 'live' | 'ended' | 'no-answer' | 'queued' | 'fail';
  direction: 'inbound' | 'outbound';
  from: string;
  to: string;
  carrier: 'twilio' | 'telnyx';
  /** ms epoch — set for any call we know started, live or ended. */
  startedAtMs?: number;
  durationStart?: number;
  duration?: number;
  latencyP95?: number;
  latencyP50?: number;
  sttAvg?: number;
  ttsAvg?: number;
  cost: CallCost;
  agent?: string;
  model?: string;
  mode?: CallMode;
  transcriptKey?: string;
  endedAgo?: number;
}

interface CallRowProps {
  call: Call;
  isSelected: boolean;
  onSelect: () => void;
  isNew: boolean;
}

function CallRow({ call, isSelected, onSelect, isNew }: CallRowProps) {
  const dur =
    call.status === 'live' && call.durationStart
      ? fmtDuration((Date.now() - call.durationStart) / 1000)
      : fmtDuration(call.duration || 0);

  const latPct = call.latencyP95 ? Math.min(100, (call.latencyP95 / 1000) * 100) : 0;
  const warn = (call.latencyP95 ?? 0) > 600;

  const totalCost =
    call.cost.total ??
    (call.cost.telco ?? 0) + (call.cost.llm ?? 0) + (call.cost.sttTts ?? 0);

  const statusClass = call.status.replace('-', '');

  return (
    <tr
      className={(isSelected ? 'selected ' : '') + (isNew ? 'new-row' : '')}
      onClick={onSelect}
    >
      <td>
        <span className={'pill ' + statusClass}>{call.status}</span>
      </td>
      <td>
        <span
          className="dir in"
          style={{
            marginRight: 8,
            color: call.direction === 'inbound' ? '#3b6f3b' : '#4a4a4a',
          }}
        >
          {call.direction === 'inbound' ? <IconArrowDown /> : <IconArrowUp />}
        </span>
        <span className="num-cell">
          {fmtPhone(call.from)} → {fmtPhone(call.to)}
        </span>
      </td>
      <td>
        <span className="car-tw">
          <span className={'car-dot ' + (call.carrier === 'twilio' ? 'tw' : 'tx')}></span>
          {call.carrier === 'twilio' ? 'Twilio' : 'Telnyx'}
        </span>
      </td>
      <td className="num-cell">{call.status === 'no-answer' ? '—' : dur}</td>
      <td>
        {call.latencyP95 ? (
          <>
            <span className={'lat-bar' + (warn ? ' warn' : '')}>
              <i style={{ width: latPct + '%' }} />
            </span>
            <span className="num-cell">{call.latencyP95} ms</span>
          </>
        ) : (
          '—'
        )}
      </td>
      <td className="num-cell">${totalCost.toFixed(2)}</td>
    </tr>
  );
}

export interface CallTableProps {
  calls: Call[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  newId: string | null;
  search: string;
  setSearch: (s: string) => void;
}

export function CallTable({
  calls,
  selectedId,
  onSelect,
  newId,
  search,
  setSearch,
}: CallTableProps) {
  const filtered = useMemo(() => {
    if (!search.trim()) return calls;
    const q = search.toLowerCase();
    return calls.filter(
      (c) =>
        c.from.toLowerCase().includes(q) ||
        c.to.toLowerCase().includes(q) ||
        c.status.includes(q) ||
        c.carrier.includes(q) ||
        c.id.includes(q),
    );
  }, [calls, search]);

  return (
    <div className="panel">
      <div className="panel-h">
        <h3>
          Recent calls{' '}
          <span
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              color: '#aaa',
              fontWeight: 500,
              marginLeft: 4,
            }}
          >
            ({filtered.length})
          </span>
        </h3>
        <div className="search">
          <IconSearch />
          <input
            placeholder="Search number, status, carrier…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <span className="sse">
          <span className="dot"></span>streaming · SSE
        </span>
      </div>
      <div style={{ maxHeight: 540, overflow: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>From → To</th>
              <th>Carrier</th>
              <th>Duration</th>
              <th>p95 latency</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty">
                  No calls match "{search}"
                </td>
              </tr>
            ) : (
              filtered.map((c) => (
                <CallRow
                  key={c.id}
                  call={c}
                  isSelected={c.id === selectedId}
                  onSelect={() => onSelect(c.id)}
                  isNew={c.id === newId}
                />
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

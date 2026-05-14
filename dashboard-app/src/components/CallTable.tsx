import { useMemo, useState } from 'react';
import { fmtDuration, fmtPhone, fmtCostUSD } from './format';
import {
  IconArrowDown,
  IconArrowUp,
  IconCheck,
  IconSearch,
  IconTrash,
  IconX,
} from './icons';

export interface CallCost {
  telco?: number;
  llm?: number;
  stt?: number;
  tts?: number;
  /** @deprecated Sum of stt+tts kept for legacy aggregate-spend callers. */
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
  /** avg(llm_ms) across this call's turns — for the waterfall llm bar. */
  llmAvg?: number;
  sttAvg?: number;
  ttsAvg?: number;
  /** Number of completed turns. p50/p95 are statistically meaningful only when this is >= 5. */
  turnCount?: number;
  /** p50 of agent_response_ms (wait time after user stops speaking) — user-perceived latency. */
  agentResponseP50?: number;
  /** p95 of agent_response_ms — user-perceived latency outlier. */
  agentResponseP95?: number;
  cost: CallCost;
  agent?: string;
  model?: string;
  mode?: CallMode;
  sttProvider?: string;
  ttsProvider?: string;
  /** Model identifier within the provider, e.g. "ink-whisper", "eleven_flash_v2_5", "gpt-oss-120b". */
  sttModel?: string;
  ttsModel?: string;
  llmModel?: string;
  transcriptKey?: string;
  endedAgo?: number;
}

interface CallRowProps {
  call: Call;
  isSelected: boolean;
  onSelect: () => void;
  isNew: boolean;
  isChecked: boolean;
  /** ``null`` when the row cannot be checked (live calls). */
  onToggleCheck: ((event: React.MouseEvent) => void) | null;
  revealed: boolean;
}

function CallRow({
  call,
  isSelected,
  onSelect,
  isNew,
  isChecked,
  onToggleCheck,
  revealed,
}: CallRowProps) {
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
      className={
        (isSelected ? 'selected ' : '') +
        (isNew ? 'new-row ' : '') +
        (isChecked ? 'checked' : '')
      }
      onClick={onSelect}
    >
      <td
        className="check-cell"
        onClick={(e) => {
          // The whole cell is the hit area to forgive imprecise clicks.
          e.stopPropagation();
          if (onToggleCheck) onToggleCheck(e);
        }}
        aria-disabled={onToggleCheck === null}
      >
        <button
          type="button"
          className={
            'row-check' +
            (isChecked ? ' on' : '') +
            (onToggleCheck === null ? ' disabled' : '')
          }
          aria-label={
            onToggleCheck === null
              ? 'Live calls cannot be deleted'
              : isChecked
                ? 'Deselect call'
                : 'Select call'
          }
          aria-pressed={isChecked}
          disabled={onToggleCheck === null}
          onClick={(e) => {
            e.stopPropagation();
            if (onToggleCheck) onToggleCheck(e);
          }}
          tabIndex={onToggleCheck === null ? -1 : 0}
        >
          {isChecked ? <IconCheck /> : null}
        </button>
      </td>
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
        <span className="num-cell pii">
          {fmtPhone(call.from, revealed)} → {fmtPhone(call.to, revealed)}
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
      <td className="num-cell">{fmtCostUSD(totalCost)}</td>
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
  /**
   * Confirmed deletion handler. The component owns the per-row checkbox
   * state internally; ``onDeleteCalls`` is invoked with the ids the user
   * confirmed in the bulk-action bar. Live ids are filtered out before
   * this handler is called.
   */
  onDeleteCalls?: (ids: readonly string[]) => Promise<void> | void;
  /** When ``false`` phone numbers are masked client-side (eye-OFF). */
  revealed: boolean;
}

export function CallTable({
  calls,
  selectedId,
  onSelect,
  newId,
  search,
  setSearch,
  onDeleteCalls,
  revealed,
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

  // Multi-select state lives in the table — App.tsx doesn't need it for any
  // other reason. Persisting across filter changes is fine: a checked id
  // that scrolls out of the visible list re-appears checked when it
  // returns, matching how Gmail / Linear / Notion handle bulk-select.
  const [checked, setChecked] = useState<ReadonlySet<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  // Ids the user CAN delete in the current visible window (live rows are
  // excluded — the server would skip them anyway, but doing it client-side
  // keeps the counter honest).
  const deletableIds = useMemo(
    () => filtered.filter((c) => c.status !== 'live').map((c) => c.id),
    [filtered],
  );
  const checkedDeletable = useMemo(
    () => deletableIds.filter((id) => checked.has(id)),
    [deletableIds, checked],
  );
  const allDeletableChecked =
    deletableIds.length > 0 && checkedDeletable.length === deletableIds.length;
  const someChecked = checkedDeletable.length > 0;

  const toggleOne = (id: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (allDeletableChecked) {
        for (const id of deletableIds) next.delete(id);
      } else {
        for (const id of deletableIds) next.add(id);
      }
      return next;
    });
  };

  const clearSelection = () => {
    setChecked(new Set());
    setConfirming(false);
  };

  const handleConfirmDelete = async () => {
    if (!onDeleteCalls || checkedDeletable.length === 0 || busy) return;
    setBusy(true);
    try {
      await onDeleteCalls(checkedDeletable);
      clearSelection();
    } finally {
      setBusy(false);
    }
  };

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

      {someChecked ? (
        <div
          className={'bulk-bar' + (confirming ? ' confirming' : '')}
          role="region"
          aria-label="Bulk actions"
        >
          <span className="bulk-count">
            <span className="bulk-num">{checkedDeletable.length}</span>
            <span className="bulk-lbl">
              {checkedDeletable.length === 1 ? 'call selected' : 'calls selected'}
            </span>
          </span>
          <div className="bulk-spacer" />
          {confirming ? (
            <>
              <span className="bulk-warn">
                Removes from view + metrics. Logs kept on disk.
              </span>
              <button
                type="button"
                className="bulk-btn ghost"
                onClick={() => setConfirming(false)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="bulk-btn destructive"
                onClick={() => void handleConfirmDelete()}
                disabled={busy}
                autoFocus
              >
                <IconTrash />
                <span>
                  {busy
                    ? 'Deleting…'
                    : `Delete ${checkedDeletable.length}`}
                </span>
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="bulk-btn ghost"
                onClick={clearSelection}
                aria-label="Clear selection"
              >
                <IconX />
                <span>Clear</span>
              </button>
              <button
                type="button"
                className="bulk-btn destructive"
                onClick={() => setConfirming(true)}
              >
                <IconTrash />
                <span>Delete</span>
              </button>
            </>
          )}
        </div>
      ) : null}

      <div style={{ minHeight: 540, maxHeight: 540, overflow: 'auto' }}>
        <table className="call-table">
          <thead>
            <tr>
              <th className="check-cell">
                <button
                  type="button"
                  className={
                    'row-check head' +
                    (allDeletableChecked
                      ? ' on'
                      : someChecked
                        ? ' indet'
                        : '') +
                    (deletableIds.length === 0 ? ' disabled' : '')
                  }
                  onClick={toggleAll}
                  disabled={deletableIds.length === 0}
                  aria-label={
                    allDeletableChecked
                      ? 'Deselect all'
                      : 'Select all calls in view'
                  }
                  aria-pressed={allDeletableChecked}
                >
                  {allDeletableChecked ? (
                    <IconCheck />
                  ) : someChecked ? (
                    <span className="indet-mark" />
                  ) : null}
                </button>
              </th>
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
                <td colSpan={7} className="empty">
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
                  isChecked={checked.has(c.id)}
                  onToggleCheck={
                    c.status === 'live' ? null : () => toggleOne(c.id)
                  }
                  revealed={revealed}
                />
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

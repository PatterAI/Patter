import { useState } from 'react';
import type { Call } from './CallTable';
import { fmtCostUSD } from './format';

export interface MetricsPanelProps {
  call: Call | null;
}

type Tab = 'latency' | 'cost';

const hasLatency = (call: Call | null): boolean =>
  !!call && typeof call.latencyP95 === 'number';

const hasCost = (call: Call | null): boolean =>
  !!call &&
  (typeof call.cost.telco === 'number' ||
    typeof call.cost.llm === 'number' ||
    typeof call.cost.sttTts === 'number' ||
    typeof call.cost.total === 'number');

export function MetricsPanel({ call }: MetricsPanelProps) {
  const [tab, setTab] = useState<Tab>('latency');
  const showLatency = hasLatency(call);
  const showCost = hasCost(call);
  if (!call || (!showLatency && !showCost)) return null;

  // Auto-fall back to whichever tab has data if the active one is empty.
  const activeTab: Tab =
    tab === 'latency' && !showLatency ? 'cost' : tab === 'cost' && !showCost ? 'latency' : tab;

  return (
    <div className="rr-card metrics-panel">
      <div className="metrics-panel-h">
        <div className="seg" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'latency'}
            disabled={!showLatency}
            className={activeTab === 'latency' ? 'on' : ''}
            onClick={() => setTab('latency')}
          >
            Latency
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'cost'}
            disabled={!showCost}
            className={activeTab === 'cost' ? 'on' : ''}
            onClick={() => setTab('cost')}
          >
            Cost
          </button>
        </div>
      </div>

      <div className="metrics-panel-body">
        {activeTab === 'latency' && showLatency && <LatencyView call={call} />}
        {activeTab === 'cost' && showCost && <CostView call={call} />}
      </div>
    </div>
  );
}

// ---------- Latency ----------

function LatencyView({ call }: { call: Call }) {
  const p50 = call.latencyP50 ?? 0;
  const p95 = call.latencyP95 ?? 0;
  const isRealtime = call.mode === 'realtime';

  // Realtime models (OpenAI Realtime, Gemini Live, ElevenLabs ConvAI when
  // running in realtime mode) handle audio in→out within a single
  // round-trip, so the SDK only knows the end-to-end latency. Breaking it
  // into stt/llm/tts is meaningless. Pipeline-mode calls expose all four.
  if (isRealtime) {
    const showPctRt = (call.turnCount ?? 0) >= 2;
    return (
      <>
        <div className="lat-grid">
          <div className="latbox">
            <div className="l">end-to-end p50</div>
            <div className="v">
              {showPctRt ? p50 || '—' : '—'}
              {showPctRt && <span className="u">ms</span>}
            </div>
          </div>
          <div className={'latbox' + (showPctRt && p95 > 600 ? ' warn' : '')}>
            <div className="l">end-to-end p95</div>
            <div className="v">
              {showPctRt ? p95 || '—' : '—'}
              {showPctRt && <span className="u">ms</span>}
            </div>
          </div>
        </div>
        <div className="waterfall">
          <div className="wf-row">
            <span className="lbl">e2e</span>
            <span className="track">
              <span
                className="seg-bar llm"
                style={{ left: 0, width: Math.min(100, (p95 / 1000) * 100) + '%' }}
              />
            </span>
            <span className="v">{p95}</span>
          </div>
        </div>
        <div className="wf-legend">
          <span>
            <i style={{ background: '#DF9367' }}></i>end-to-end
          </span>
          <span style={{ marginLeft: 'auto' }}>
            {call.agent ?? 'realtime'}
          </span>
        </div>
      </>
    );
  }

  const stt = call.sttAvg || 0;
  const llm = call.llmAvg || 0;
  const tts = call.ttsAvg || 0;
  const total = stt + llm + tts;
  const max = Math.max(total, 800);
  // Percentile boxes are statistical noise on calls with too few turns
  // (with n=4 samples, p95 is interpolation between sample[2] and sample[3]
  // and doesn't correspond to any real turn). Show ``—`` until ≥5 turns.
  const showPct = (call.turnCount ?? 0) >= 2;

  return (
    <>
      <div className="lat-grid">
        <div className="latbox">
          <div className="l">p50</div>
          <div className="v">
            {showPct ? call.latencyP50 ?? '—' : '—'}
            {showPct && <span className="u">ms</span>}
          </div>
        </div>
        <div className={'latbox' + (showPct && p95 > 600 ? ' warn' : '')}>
          <div className="l">p95</div>
          <div className="v">
            {showPct ? p95 : '—'}
            {showPct && <span className="u">ms</span>}
          </div>
        </div>
        <div className="latbox">
          <div className="l">stt avg</div>
          <div className="v">
            {call.sttAvg ?? '—'}
            <span className="u">ms</span>
          </div>
        </div>
        <div className="latbox">
          <div className="l">tts avg</div>
          <div className="v">
            {call.ttsAvg ?? '—'}
            <span className="u">ms</span>
          </div>
        </div>
      </div>

      <div className="waterfall">
        <div className="wf-row">
          <span className="lbl">stt</span>
          <span className="track">
            <span className="seg-bar stt" style={{ left: 0, width: (stt / max) * 100 + '%' }} />
          </span>
          <span className="v">{stt}</span>
        </div>
        <div className="wf-row">
          <span className="lbl">llm</span>
          <span className="track">
            <span
              className="seg-bar llm"
              style={{ left: (stt / max) * 100 + '%', width: (llm / max) * 100 + '%' }}
            />
          </span>
          <span className="v">{llm}</span>
        </div>
        <div className="wf-row">
          <span className="lbl">tts</span>
          <span className="track">
            <span
              className="seg-bar tts"
              style={{
                left: ((stt + llm) / max) * 100 + '%',
                width: (tts / max) * 100 + '%',
              }}
            />
          </span>
          <span className="v">{tts}</span>
        </div>
      </div>
      <div className="wf-legend">
        <span>
          <i style={{ background: '#1a1a1a' }}></i>stt
        </span>
        <span>
          <i style={{ background: '#DF9367' }}></i>llm
        </span>
        <span>
          <i style={{ background: '#278EFF', opacity: 0.8 }}></i>tts
        </span>
        <span style={{ marginLeft: 'auto' }}>total {total} ms</span>
      </div>
    </>
  );
}

// ---------- Cost ----------

function titleCase(s: string): string {
  if (s.length === 0) return s;
  // Strip provider-key transport suffixes (_ws, _rest) and role suffixes
  // (_stt, _tts, _llm). Repeated `+` handles compound suffixes like
  // "cartesia_tts_ws" -> "cartesia". The SDK uses provider_key like
  // "elevenlabs_ws" / "cartesia_stt" to disambiguate adapter classes;
  // the suffix is internal noise in user-facing UI.
  const cleaned = s.replace(/(?:_(?:ws|rest|stt|tts|llm))+$/i, '');
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

function CostView({ call }: { call: Call }) {
  const c = call.cost;
  const telco = c.telco ?? 0;
  const llm = c.llm ?? 0;
  // Always prefer the per-component split when present; fall back to the
  // legacy combined ``sttTts`` field only when the SDK didn't emit the
  // split. Greenfield calls (>=0.6.1) always emit stt + tts separately.
  const stt = c.stt ?? 0;
  const tts = c.tts ?? 0;
  const sttTtsCombined = c.sttTts ?? 0;
  const sttTtsLegacy = stt === 0 && tts === 0 ? sttTtsCombined : 0;
  const cached = c.cached ?? 0;
  const subtotal = telco + llm + stt + tts + sttTtsLegacy;
  const total = c.total ?? subtotal - cached;
  const seg = (v: number) => (subtotal > 0 ? (v / subtotal) * 100 : 0);

  const sttLabel = call.sttProvider
    ? `${titleCase(call.sttProvider)} STT${call.sttModel ? ` · ${call.sttModel}` : ''}`
    : 'STT';
  const ttsLabel = call.ttsProvider
    ? `${titleCase(call.ttsProvider)} TTS${call.ttsModel ? ` · ${call.ttsModel}` : ''}`
    : 'TTS';
  const llmLabel = call.llmModel
    ? `${call.model ? titleCase(call.model) + ' · ' : ''}${call.llmModel}`
    : call.model || 'LLM';

  return (
    <>
      {subtotal > 0 && (
        <div className="cost-bar">
          <i style={{ background: '#cc0000', width: seg(telco) + '%' }} />
          <i style={{ background: '#DF9367', width: seg(llm) + '%' }} />
          <i style={{ background: '#1a1a1a', width: seg(stt + sttTtsLegacy) + '%' }} />
          <i style={{ background: '#6c6c6c', width: seg(tts) + '%' }} />
        </div>
      )}
      {telco > 0 && (
        <div className="stack-row">
          <span className="lbl">
            <span className="swatch" style={{ background: '#cc0000' }}></span>
            {call.carrier === 'twilio' ? 'Twilio' : 'Telnyx'}
          </span>
          <span className="v">{fmtCostUSD(telco)}</span>
        </div>
      )}
      {llm > 0 && (
        <div className="stack-row">
          <span className="lbl">
            <span className="swatch" style={{ background: '#DF9367' }}></span>
            {llmLabel}
          </span>
          <span className="v">{fmtCostUSD(llm)}</span>
          {cached > 0 && <span className="saved">−{fmtCostUSD(cached)} cached</span>}
        </div>
      )}
      {stt > 0 && (
        <div className="stack-row">
          <span className="lbl">
            <span className="swatch" style={{ background: '#1a1a1a' }}></span>
            {sttLabel}
          </span>
          <span className="v">{fmtCostUSD(stt)}</span>
        </div>
      )}
      {tts > 0 && (
        <div className="stack-row">
          <span className="lbl">
            <span className="swatch" style={{ background: '#6c6c6c' }}></span>
            {ttsLabel}
          </span>
          <span className="v">{fmtCostUSD(tts)}</span>
        </div>
      )}
      {sttTtsLegacy > 0 && (
        <div className="stack-row">
          <span className="lbl">
            <span className="swatch" style={{ background: '#1a1a1a' }}></span>
            STT / TTS (legacy)
          </span>
          <span className="v">{fmtCostUSD(sttTtsLegacy)}</span>
        </div>
      )}
      <div className="stack-row">
        <span className="lbl">
          Total{' '}
          {call.status === 'live' && (
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 10,
                color: '#aaa',
                marginLeft: 4,
              }}
            >
              (running)
            </span>
          )}
        </span>
        <span className="v">{fmtCostUSD(total)}</span>
      </div>
    </>
  );
}

import { useState } from 'react';
import type { Call } from './CallTable';

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

      {activeTab === 'latency' && showLatency && <LatencyView call={call} />}
      {activeTab === 'cost' && showCost && <CostView call={call} />}
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
    return (
      <>
        <div className="lat-grid">
          <div className="latbox">
            <div className="l">end-to-end p50</div>
            <div className="v">
              {p50 || '—'}
              <span className="u">ms</span>
            </div>
          </div>
          <div className={'latbox' + (p95 > 600 ? ' warn' : '')}>
            <div className="l">end-to-end p95</div>
            <div className="v">
              {p95 || '—'}
              <span className="u">ms</span>
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
  const llm = call.latencyP50 || 0;
  const tts = call.ttsAvg || 0;
  const total = stt + llm + tts;
  const max = Math.max(total, 800);

  return (
    <>
      <div className="lat-grid">
        <div className="latbox">
          <div className="l">p50</div>
          <div className="v">
            {call.latencyP50 ?? '—'}
            <span className="u">ms</span>
          </div>
        </div>
        <div className={'latbox' + (p95 > 600 ? ' warn' : '')}>
          <div className="l">p95</div>
          <div className="v">
            {p95}
            <span className="u">ms</span>
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

function CostView({ call }: { call: Call }) {
  const c = call.cost;
  const telco = c.telco ?? 0;
  const llm = c.llm ?? 0;
  const sttTts = c.sttTts ?? 0;
  const cached = c.cached ?? 0;
  const subtotal = telco + llm + sttTts;
  const total = c.total ?? subtotal - cached;
  const seg = (v: number) => (subtotal > 0 ? (v / subtotal) * 100 : 0);

  return (
    <>
      {subtotal > 0 && (
        <div className="cost-bar">
          <i style={{ background: '#cc0000', width: seg(telco) + '%' }} />
          <i style={{ background: '#DF9367', width: seg(llm) + '%' }} />
          <i style={{ background: '#1a1a1a', width: seg(sttTts) + '%' }} />
        </div>
      )}
      {telco > 0 && (
        <div className="stack-row">
          <span className="lbl">
            <span className="swatch" style={{ background: '#cc0000' }}></span>
            {call.carrier === 'twilio' ? 'Twilio' : 'Telnyx'}
          </span>
          <span className="v">${telco.toFixed(3)}</span>
        </div>
      )}
      {llm > 0 && (
        <div className="stack-row">
          <span className="lbl">
            <span className="swatch" style={{ background: '#DF9367' }}></span>
            {call.model || 'LLM'}
          </span>
          <span className="v">${llm.toFixed(3)}</span>
          {cached > 0 && <span className="saved">−${cached.toFixed(3)} cached</span>}
        </div>
      )}
      {sttTts > 0 && (
        <div className="stack-row">
          <span className="lbl">
            <span className="swatch" style={{ background: '#1a1a1a' }}></span>
            STT / TTS
          </span>
          <span className="v">${sttTts.toFixed(3)}</span>
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
        <span className="v">${total.toFixed(3)}</span>
      </div>
    </>
  );
}

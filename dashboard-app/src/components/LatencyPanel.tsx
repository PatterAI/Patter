import type { Call } from './CallTable';

export interface LatencyPanelProps {
  call: Call | null;
}

// 2 turn = almeno 1 turn user genuino oltre al firstMessage. Sotto a 2 i
// percentili sono privi di senso (un singolo campione). Sopra a 2 sono
// statisticamente magri ma informativi — meglio mostrarli che lasciare il
// pannello con "—" quando la tabella sopra mostra già una p95 dal fallback
// ad avg.
const MIN_TURNS_FOR_PERCENTILES = 2;

export function LatencyPanel({ call }: LatencyPanelProps) {
  if (!call || (!call.latencyP95 && !call.agentResponseP95)) return null;

  const stt = call.sttAvg ?? 0;
  const llm = call.llmAvg ?? 0;
  const tts = call.ttsAvg ?? 0;
  const total = stt + llm + tts;
  const max = Math.max(total, 800);

  const turns = call.turnCount ?? 0;
  const showPercentiles = turns >= MIN_TURNS_FOR_PERCENTILES;
  const dash = '—';

  return (
    <div className="rr-card">
      <h3 style={{ marginBottom: 14 }}>Latency · this call</h3>
      <div className="lat-grid">
        <div className="latbox">
          <div className="l">p50 round-trip</div>
          <div className="v">
            {showPercentiles ? call.latencyP50 ?? dash : dash}
            {showPercentiles && <span className="u">ms</span>}
          </div>
        </div>
        <div className={'latbox' + (showPercentiles && (call.latencyP95 ?? 0) > 600 ? ' warn' : '')}>
          <div className="l">p95 round-trip</div>
          <div className="v">
            {showPercentiles ? call.latencyP95 ?? dash : dash}
            {showPercentiles && <span className="u">ms</span>}
          </div>
        </div>
        <div className="latbox">
          <div className="l">p50 wait</div>
          <div className="v">
            {showPercentiles ? call.agentResponseP50 ?? dash : dash}
            {showPercentiles && <span className="u">ms</span>}
          </div>
        </div>
        <div className={'latbox' + (showPercentiles && (call.agentResponseP95 ?? 0) > 600 ? ' warn' : '')}>
          <div className="l">p95 wait</div>
          <div className="v">
            {showPercentiles ? call.agentResponseP95 ?? dash : dash}
            {showPercentiles && <span className="u">ms</span>}
          </div>
        </div>
      </div>
      {!showPercentiles && (
        <div style={{ marginTop: -6, marginBottom: 10, fontSize: 11, opacity: 0.6 }}>
          {turns} {turns === 1 ? 'turn' : 'turns'} — percentiles need ≥{MIN_TURNS_FOR_PERCENTILES}
        </div>
      )}

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
        <span style={{ marginLeft: 'auto' }}>avg wait {Math.round(total)} ms</span>
      </div>
    </div>
  );
}

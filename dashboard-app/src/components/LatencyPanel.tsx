import type { Call } from './CallTable';

export interface LatencyPanelProps {
  call: Call | null;
}

export function LatencyPanel({ call }: LatencyPanelProps) {
  if (!call || !call.latencyP95) return null;

  const stt = call.sttAvg || 0;
  const llm = call.latencyP50 || 0;
  const tts = call.ttsAvg || 0;
  const total = stt + llm + tts;
  const max = Math.max(total, 800);

  return (
    <div className="rr-card">
      <h3 style={{ marginBottom: 14 }}>Latency · this call</h3>
      <div className="lat-grid">
        <div className="latbox">
          <div className="l">p50</div>
          <div className="v">
            {call.latencyP50}
            <span className="u">ms</span>
          </div>
        </div>
        <div className={'latbox' + (call.latencyP95 > 600 ? ' warn' : '')}>
          <div className="l">p95</div>
          <div className="v">
            {call.latencyP95}
            <span className="u">ms</span>
          </div>
        </div>
        <div className="latbox">
          <div className="l">stt avg</div>
          <div className="v">
            {call.sttAvg}
            <span className="u">ms</span>
          </div>
        </div>
        <div className="latbox">
          <div className="l">tts avg</div>
          <div className="v">
            {call.ttsAvg}
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
    </div>
  );
}

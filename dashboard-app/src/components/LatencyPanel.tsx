import type { Call } from './CallTable';

export interface LatencyPanelProps {
  call: Call | null;
}

// With <10 samples p95 is dominated by a single outlier turn — observed on
// a real n=5 call where p95=1977ms but p50=309ms, making the headline
// number misleading. 10 turns is the threshold where p95 becomes a stable
// signal (95th percentile = 9.5th-ranked sample, so at n=10 it interpolates
// between the two slowest turns rather than reporting the absolute slowest).
// Below the threshold we show p50 instead — robust, single-sample-resistant,
// and labelled so the user knows why.
const MIN_TURNS_FOR_PERCENTILES = 10;

export function LatencyPanel({ call }: LatencyPanelProps) {
  if (!call) return null;
  // Hide the panel entirely when there is no latency signal at all (neither
  // p50 nor p95 on either metric). Below the percentile threshold we still
  // render — falling back to p50 — so a 1-2 turn call with measured timings
  // does not show a blank pane.
  const hasAnyLatency =
    call.latencyP50 != null ||
    call.latencyP95 != null ||
    call.agentResponseP50 != null ||
    call.agentResponseP95 != null;
  if (!hasAnyLatency) return null;

  const stt = call.sttAvg ?? 0;
  const llm = call.llmAvg ?? 0;
  const tts = call.ttsAvg ?? 0;
  const total = stt + llm + tts;
  const max = Math.max(total, 800);

  const turns = call.turnCount ?? 0;
  const showPercentiles = turns >= MIN_TURNS_FOR_PERCENTILES;
  const dash = '—';

  const lowSampleHint = `p95 hidden until ≥${MIN_TURNS_FOR_PERCENTILES} turns — showing p50 instead (n=${turns})`;

  return (
    <div className="rr-card">
      <h3 style={{ marginBottom: 14 }}>Latency · this call</h3>
      <div className="lat-grid">
        <div className="latbox">
          <div className="l">p50 round-trip</div>
          <div className="v">
            {call.latencyP50 ?? dash}
            {call.latencyP50 != null && <span className="u">ms</span>}
          </div>
        </div>
        <div
          className={
            'latbox' + (showPercentiles && (call.latencyP95 ?? 0) > 600 ? ' warn' : '')
          }
          title={showPercentiles ? undefined : lowSampleHint}
        >
          <div className="l">
            {showPercentiles
              ? 'p95 round-trip'
              : `p50 round-trip (n<${MIN_TURNS_FOR_PERCENTILES})`}
          </div>
          <div className="v">
            {showPercentiles ? call.latencyP95 ?? dash : call.latencyP50 ?? dash}
            {(showPercentiles ? call.latencyP95 : call.latencyP50) != null && (
              <span className="u">ms</span>
            )}
          </div>
        </div>
        <div className="latbox">
          <div className="l">p50 wait</div>
          <div className="v">
            {call.agentResponseP50 ?? dash}
            {call.agentResponseP50 != null && <span className="u">ms</span>}
          </div>
        </div>
        <div
          className={
            'latbox' +
            (showPercentiles && (call.agentResponseP95 ?? 0) > 600 ? ' warn' : '')
          }
          title={showPercentiles ? undefined : lowSampleHint}
        >
          <div className="l">
            {showPercentiles ? 'p95 wait' : `p50 wait (n<${MIN_TURNS_FOR_PERCENTILES})`}
          </div>
          <div className="v">
            {showPercentiles
              ? call.agentResponseP95 ?? dash
              : call.agentResponseP50 ?? dash}
            {(showPercentiles ? call.agentResponseP95 : call.agentResponseP50) != null && (
              <span className="u">ms</span>
            )}
          </div>
        </div>
      </div>
      {!showPercentiles && (
        <div style={{ marginTop: -6, marginBottom: 10, fontSize: 11, opacity: 0.6 }}>
          {turns} {turns === 1 ? 'turn' : 'turns'} — p95 hidden until ≥
          {MIN_TURNS_FOR_PERCENTILES}, showing p50
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

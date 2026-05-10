import type { Call } from './CallTable';

export interface CostPanelProps {
  call: Call | null;
}

function titleCase(s: string): string {
  return s.length === 0 ? s : s.charAt(0).toUpperCase() + s.slice(1);
}

export function CostPanel({ call }: CostPanelProps) {
  if (!call || !call.cost?.telco) return null;

  const c = call.cost;
  const telco = c.telco ?? 0;
  const llm = c.llm ?? 0;
  const stt = c.stt ?? 0;
  const tts = c.tts ?? 0;
  const sttTtsLegacy = c.sttTts ?? stt + tts;
  const cached = c.cached ?? 0;

  const subtotal = telco + llm + sttTtsLegacy;
  const total = subtotal - cached;
  const seg = (v: number) => (subtotal > 0 ? (v / subtotal) * 100 : 0);

  const sttLabel = call.sttProvider ? `${titleCase(call.sttProvider)} STT` : 'STT';
  const ttsLabel = call.ttsProvider ? `${titleCase(call.ttsProvider)} TTS` : 'TTS';

  return (
    <div className="rr-card peach">
      <h3 style={{ marginBottom: 14 }}>Cost breakdown</h3>
      <div className="cost-bar">
        <i style={{ background: '#cc0000', width: seg(telco) + '%' }} />
        <i style={{ background: '#DF9367', width: seg(llm) + '%' }} />
        <i style={{ background: '#1a1a1a', width: seg(stt) + '%' }} />
        <i style={{ background: '#6c6c6c', width: seg(tts) + '%' }} />
      </div>
      <div className="stack-row">
        <span className="lbl">
          <span className="swatch" style={{ background: '#cc0000' }}></span>
          {call.carrier === 'twilio' ? 'Twilio' : 'Telnyx'}
        </span>
        <span className="v">${telco.toFixed(3)}</span>
      </div>
      <div className="stack-row">
        <span className="lbl">
          <span className="swatch" style={{ background: '#DF9367' }}></span>
          {call.model || 'LLM'}
        </span>
        <span className="v">${llm.toFixed(3)}</span>
        {cached > 0 && <span className="saved">−${cached.toFixed(3)} cached</span>}
      </div>
      <div className="stack-row">
        <span className="lbl">
          <span className="swatch" style={{ background: '#1a1a1a' }}></span>
          {sttLabel}
        </span>
        <span className="v">${stt.toFixed(3)}</span>
      </div>
      <div className="stack-row">
        <span className="lbl">
          <span className="swatch" style={{ background: '#6c6c6c' }}></span>
          {ttsLabel}
        </span>
        <span className="v">${tts.toFixed(3)}</span>
      </div>
      <div className="stack-row">
        <span className="lbl">
          Total{' '}
          <span
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              color: '#aaa',
              marginLeft: 4,
            }}
          >
            {call.status === 'live' ? '(running)' : ''}
          </span>
        </span>
        <span className="v">${total.toFixed(3)}</span>
      </div>
    </div>
  );
}

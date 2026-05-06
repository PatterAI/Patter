import type { Call } from './CallTable';

export interface CostPanelProps {
  call: Call | null;
}

export function CostPanel({ call }: CostPanelProps) {
  if (!call || !call.cost?.telco) return null;

  const c = call.cost;
  const telco = c.telco ?? 0;
  const llm = c.llm ?? 0;
  const sttTts = c.sttTts ?? 0;
  const cached = c.cached ?? 0;

  const subtotal = telco + llm + sttTts;
  const total = subtotal - cached;
  const seg = (v: number) => (subtotal > 0 ? (v / subtotal) * 100 : 0);

  return (
    <div className="rr-card peach">
      <h3 style={{ marginBottom: 14 }}>Cost breakdown</h3>
      <div className="cost-bar">
        <i style={{ background: '#cc0000', width: seg(telco) + '%' }} />
        <i style={{ background: '#DF9367', width: seg(llm) + '%' }} />
        <i style={{ background: '#1a1a1a', width: seg(sttTts) + '%' }} />
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
          STT / TTS
        </span>
        <span className="v">${sttTts.toFixed(3)}</span>
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

import type { Call } from './CallTable';
import { fmtCostUSD } from './format';
import { CarrierBadge } from './CarrierBadge';

export interface CostPanelProps {
  call: Call | null;
}

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
          <CarrierBadge carrier={call.carrier} />
        </span>
        <span className="v">{fmtCostUSD(telco)}</span>
      </div>
      <div className="stack-row">
        <span className="lbl">
          <span className="swatch" style={{ background: '#DF9367' }}></span>
          {llmLabel}
        </span>
        <span className="v">{fmtCostUSD(llm)}</span>
        {cached > 0 && <span className="saved">−{fmtCostUSD(cached)} cached</span>}
      </div>
      <div className="stack-row">
        <span className="lbl">
          <span className="swatch" style={{ background: '#1a1a1a' }}></span>
          {sttLabel}
        </span>
        <span className="v">{fmtCostUSD(stt)}</span>
      </div>
      <div className="stack-row">
        <span className="lbl">
          <span className="swatch" style={{ background: '#6c6c6c' }}></span>
          {ttsLabel}
        </span>
        <span className="v">{fmtCostUSD(tts)}</span>
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
        <span className="v">{fmtCostUSD(total)}</span>
      </div>
    </div>
  );
}

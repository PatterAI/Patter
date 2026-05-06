import { PatterLogo } from './PatterLogo';

export interface TopbarProps {
  liveCount: number;
  todayCount: number;
  phoneNumber: string;
  sdkVersion: string;
}

export function Topbar({ liveCount, todayCount, phoneNumber, sdkVersion }: TopbarProps) {
  return (
    <header className="top">
      <div className="brand">
        <PatterLogo />
        <span className="tag">dashboard · v{sdkVersion}</span>
      </div>
      <div className="top-r">
        <span className="live-chip">
          <span className={'pulse' + (liveCount > 0 ? ' active' : '')}></span>
          {liveCount} live · {todayCount} today
        </span>
        {phoneNumber !== '—' && <span className="num-chip">{phoneNumber}</span>}
      </div>
    </header>
  );
}

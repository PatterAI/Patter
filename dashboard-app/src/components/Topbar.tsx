import { IconBell, IconBrandMark, IconSettings } from './icons';

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
        <IconBrandMark aria-hidden="true" />
        <span>Patter</span>
        <span className="tag">dashboard · v{sdkVersion}</span>
      </div>
      <div className="top-r">
        <span className="live-chip">
          <span className="pulse"></span>
          {liveCount} live · {todayCount} today
        </span>
        <span className="num-chip">{phoneNumber}</span>
        <button className="icon-btn" title="Notifications" type="button">
          <IconBell />
        </button>
        <button className="icon-btn" title="Settings" type="button">
          <IconSettings />
        </button>
        <div className="avatar">MD</div>
      </div>
    </header>
  );
}

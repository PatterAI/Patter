import { PatterLogo } from './PatterLogo';
import { fmtPhone } from './format';
import { IconEye, IconEyeOff, IconMoon, IconSun } from './icons';

export interface TopbarProps {
  liveCount: number;
  todayCount: number;
  /** Raw phone number as the SDK reported it (may already be masked on disk). */
  phoneNumber: string;
  sdkVersion: string;
  /** PII reveal state: when ``true`` show full numbers, when ``false`` mask. */
  revealed: boolean;
  /** Dark-theme state for the ``body.dark`` override. */
  dark: boolean;
  onToggleRevealed: () => void;
  onToggleDark: () => void;
}

export function Topbar({
  liveCount,
  todayCount,
  phoneNumber,
  sdkVersion,
  revealed,
  dark,
  onToggleRevealed,
  onToggleDark,
}: TopbarProps) {
  const displayNumber = fmtPhone(phoneNumber, revealed);
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
        {phoneNumber && phoneNumber !== '—' && (
          <span className="num-chip">{displayNumber}</span>
        )}
        <button
          type="button"
          className={'icon-btn toggle' + (revealed ? ' on' : '')}
          onClick={onToggleRevealed}
          aria-label={revealed ? 'Hide phone numbers' : 'Reveal phone numbers'}
          aria-pressed={revealed}
          title={revealed ? 'Hide numbers' : 'Reveal numbers'}
        >
          {revealed ? <IconEye /> : <IconEyeOff />}
        </button>
        <button
          type="button"
          className={'icon-btn toggle' + (dark ? ' on' : '')}
          onClick={onToggleDark}
          aria-label={dark ? 'Switch to light theme' : 'Switch to dark theme'}
          aria-pressed={dark}
          title={dark ? 'Light mode' : 'Dark mode'}
        >
          {dark ? <IconSun /> : <IconMoon />}
        </button>
      </div>
    </header>
  );
}

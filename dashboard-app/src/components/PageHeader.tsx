import { IconArrowDown, IconPlus } from './icons';

const RANGES = ['1h', '24h', '7d', 'All'] as const;

export interface PageHeaderProps {
  range: string;
  setRange: (r: string) => void;
  onPlace: () => void;
}

export function PageHeader({ range, setRange, onPlace }: PageHeaderProps) {
  return (
    <div className="ph">
      <div>
        <h1>Calls</h1>
        <p className="sub">
          Real-time view of every call routed through this Patter instance.{' '}
          <span className="kbd">⇧K</span> to focus search.
        </p>
      </div>
      <div className="filters">
        <div className="seg">
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              className={range === r ? 'on' : ''}
              onClick={() => setRange(r)}
            >
              {r}
            </button>
          ))}
        </div>
        <button className="btn" type="button">
          <IconArrowDown /> Export CSV
        </button>
        <button className="btn primary" type="button" onClick={onPlace}>
          <IconPlus /> Place call
        </button>
      </div>
    </div>
  );
}

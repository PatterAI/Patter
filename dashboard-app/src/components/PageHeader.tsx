import { IconArrowDown } from './icons';
import { withToken } from '../lib/api';

const RANGES = ['1h', '24h', '7d', 'All'] as const;

export interface PageHeaderProps {
  range: string;
  setRange: (r: string) => void;
}

/**
 * Trigger a CSV download from the SDK's export endpoint. The endpoint
 * already handles streaming the file and setting Content-Disposition, so
 * we just navigate the browser to it via a transient anchor element.
 */
function downloadCsv(): void {
  const a = document.createElement('a');
  a.href = withToken('/api/dashboard/export/calls?format=csv');
  a.download = 'patter_calls.csv';
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

export function PageHeader({ range, setRange }: PageHeaderProps) {
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
        <button className="btn" type="button" onClick={downloadCsv}>
          <IconArrowDown /> Export CSV
        </button>
      </div>
    </div>
  );
}

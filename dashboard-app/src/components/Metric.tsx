export interface MetricProps {
  label: string;
  value: string | number;
  unit?: string;
  delta?: string;
  deltaTone?: 'up' | 'dn';
  spark: number[];
  peach?: boolean;
  footer?: string;
  badge?: boolean;
}

export function Metric({
  label,
  value,
  unit,
  delta,
  deltaTone,
  spark,
  peach,
  footer,
  badge,
}: MetricProps) {
  return (
    <div className={'metric' + (peach ? ' peach' : '')}>
      <div className="lbl">
        <span>{label}</span>
        {badge && <span className="badge-now">LIVE</span>}
      </div>
      <div className="val">
        {value}
        {unit && <span className="unit"> {unit}</span>}
      </div>
      {delta && <div className={'delta ' + (deltaTone || '')}>{delta}</div>}
      {footer && <div className="delta">{footer}</div>}
      <div className="spark">
        {spark.map((h, i) => (
          <span key={i} style={{ height: h + '%' }} />
        ))}
      </div>
    </div>
  );
}

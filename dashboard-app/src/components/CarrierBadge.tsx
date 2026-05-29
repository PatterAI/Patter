// Carrier display primitives — the single place that knows how to render a
// carrier as a small visual element. Future styling changes (icon, hover
// state, badge variant) happen here once, not in every panel.

import { CARRIERS, type CallCarrier } from '../lib/mappers';

/** Carrier name with a coloured indicator dot — used in the call-table row.
 *  Wrapping element class is the existing ``.car-tw`` layout slot. */
export function CarrierChip({ carrier }: { carrier: CallCarrier }) {
  const m = CARRIERS[carrier];
  return (
    <span className="car-tw">
      <span className={`car-dot ${m.dotClass}`}></span>
      {m.label}
    </span>
  );
}

/** Coloured ``.swatch`` square + carrier name, as used in the Cost / Metrics
 *  panel stat rows. Returns a fragment so the caller controls the row
 *  wrapper (``<span className="lbl">``) — the component doesn't need to know
 *  whether it sits inside a stat row, a tooltip, or somewhere else. */
export function CarrierBadge({ carrier }: { carrier: CallCarrier }) {
  const m = CARRIERS[carrier];
  return (
    <>
      <span className={`swatch ${m.dotClass}`}></span>
      {m.label}
    </>
  );
}

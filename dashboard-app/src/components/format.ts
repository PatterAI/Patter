/**
 * Pure formatting helpers used across dashboard components.
 */

export function fmtDuration(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export function fmtAgo(sec: number): string {
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}

export function fmtPhone(p: string): string {
  return p;
}

/**
 * Render a USD amount with precision adapted to its magnitude so per-call
 * costs from cheap providers (Cerebras gpt-oss-120b ≈ $0.0001 / 5-turn call)
 * are not flattened to "$0.00" by a fixed `toFixed(2)`.
 *
 *   ≥ $0.01       → 2 decimals  "$0.12"
 *   ≥ $0.001      → 3 decimals  "$0.012"
 *   ≥ $0.0001     → 4 decimals  "$0.0001"
 *   > 0           → 5 decimals  "$0.00001"
 *   0 / nullish   → "$0.00"
 */
export function fmtCostUSD(value: number | undefined | null): string {
  if (value === undefined || value === null || !Number.isFinite(value)) {
    return '$0.00';
  }
  const v = Math.abs(value);
  if (v === 0) return '$0.00';
  if (v >= 0.01) return `$${value.toFixed(2)}`;
  if (v >= 0.001) return `$${value.toFixed(3)}`;
  if (v >= 0.0001) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(5)}`;
}

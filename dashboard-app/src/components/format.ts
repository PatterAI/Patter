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

/**
 * Render a phone number for display.
 *
 * The SDK's CallLogger writes phone numbers to disk in one of three forms
 * (controlled by ``PATTER_LOG_REDACT_PHONE``):
 *   - ``mask`` (default) → ``***<last4>``  (three U+002A asterisks)
 *   - ``full``           → raw E.164 (``+15551234567``)
 *   - ``hash_only``      → ``sha256:<16-hex>``
 *
 * When ``revealed=true`` we hand the value back as-is so the operator
 * sees whatever the server provided (full when ``full``, masked when
 * ``mask``). When ``revealed=false`` we ENFORCE masking client-side —
 * even if the server happens to have full numbers we don't render them.
 * The masked form uses U+2022 BULLET ``•`` instead of asterisks because
 * bullets sit on the digit baseline; asterisks float toward the cap
 * height and look misaligned next to numerals.
 *
 * Examples (revealed=false):
 *   "+15556234231"  → "•••4231"
 *   "***4231"       → "•••4231"   (re-normalise existing server masking)
 *   "sha256:abcd…"  → "••••••••"
 *   "" / "?"        → ""           (let callers fall back to "—")
 */
export function fmtPhone(p: string, revealed = true): string {
  if (!p) return '';
  if (revealed) {
    // Honour whatever the server gave us. Re-render legacy "***<last4>" as
    // bullets so the alignment is consistent even when the operator opts
    // to reveal — the underlying log artefact is unchanged.
    if (p.startsWith('***')) return '•••' + p.slice(3);
    return p;
  }
  // Masked mode. Try to keep a last-4 anchor for correlation.
  if (p.startsWith('***')) return '•••' + p.slice(3);
  if (p.startsWith('sha256:')) return '••••••••';
  const digits = p.replace(/\D/g, '');
  if (digits.length >= 4) return '•••' + digits.slice(-4);
  return '••••••••';
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

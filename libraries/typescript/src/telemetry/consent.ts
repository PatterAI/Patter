/**
 * Resolve whether anonymous telemetry is enabled.
 *
 * OPT-OUT model: telemetry is **on by default**, matching the open-source norm
 * (Next.js / Astro / Gatsby / .NET CLI). The env vars and the constructor flag
 * are *disable* switches, not opt-in switches.
 *
 * Precedence (first match wins):
 *   1. DO_NOT_TRACK truthy            -> OFF  (cross-tool kill switch, always wins)
 *   2. PATTER_TELEMETRY_DISABLED      -> OFF  (Patter-specific kill switch)
 *   3. persisted opt-out marker       -> OFF  (`getpatter telemetry disable`)
 *   4. flag === false                 -> OFF  (explicit in-code opt-out)
 *   5. CI / test runner detected      -> OFF
 *   6. default                        -> ON
 *
 * Mirrors `getpatter/telemetry/consent.py`.
 */

import { isCi, isTest, isTruthy } from './env';
import { isOptedOut } from './install-id';

/**
 * Resolve telemetry enablement. `flag` is the value of the public
 * `new Patter({ telemetry })` option: `undefined` means "not specified"
 * (default ON), `false` is an explicit opt-out, `true` an explicit opt-in that
 * still yields to DO_NOT_TRACK / the kill switch / CI detection above it.
 */
export function isEnabled(flag?: boolean): boolean {
  if (isTruthy(process.env.DO_NOT_TRACK)) return false;
  if (isTruthy(process.env.PATTER_TELEMETRY_DISABLED)) return false;
  // Persisted, machine-level opt-out written by `getpatter telemetry disable`.
  // Read-only — resolving consent never writes to the filesystem.
  if (isOptedOut()) return false;
  if (flag === false) return false;
  if (isCi() || isTest()) return false;
  return true;
}

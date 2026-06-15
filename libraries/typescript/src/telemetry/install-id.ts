/**
 * Anonymous identifiers for telemetry.
 *
 * Two ids, both free of PII and never derived from hardware (MAC / hostname /
 * serials — that would be fingerprinting):
 *  - `runId()` — a fresh random id per process start; groups the events of one
 *    run without correlating runs over time.
 *  - `installId()` — a random UUID generated once and persisted to a small local
 *    file, so the same install reports the same id across restarts. This is the
 *    standard anonymous "install id" used by OSS tools (Homebrew, Next.js, Astro)
 *    to count active installs — a random number, not tied to a person or any
 *    identifying data, only read/created on the telemetry-enabled path. If the
 *    file cannot be written we fall back to the per-process run id.
 *
 * Mirrors `getpatter/telemetry/install_id.py`.
 */

import { randomUUID } from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

const RUN_ID = randomUUID().replace(/-/g, '');
const HEX32 = /^[0-9a-f]{32}$/;
const VERSION_RE = /^[0-9][0-9a-z.+-]{0,31}$/;
let cachedInstallId: string | null = null;

/** This process's anonymous run id (stable for the process lifetime). */
export function runId(): string {
  return RUN_ID;
}

/**
 * Directory holding the telemetry state files (overridable for tests).
 * Precedence: `PATTER_TELEMETRY_STATE_DIR` (used literally) →
 * `$XDG_STATE_HOME/getpatter` (the XDG spec requires an app subdirectory —
 * writing into the shared root collides with other tools) → `~/.getpatter`.
 */
function stateDir(): string {
  const override = process.env.PATTER_TELEMETRY_STATE_DIR;
  if (override && override.length > 0) return override;
  const xdg = process.env.XDG_STATE_HOME;
  if (xdg && xdg.length > 0) return path.join(xdg, 'getpatter');
  return path.join(os.homedir(), '.getpatter');
}

/**
 * Where pre-0.6.8 builds wrote state when `XDG_STATE_HOME` was set: the bare
 * `$XDG_STATE_HOME` root (no `getpatter/` subdirectory). Existing installs must
 * keep their id — and, critically, a persisted opt-out must keep being honored —
 * so the readers below fall back here. `null` when no legacy location applies.
 */
function legacyStateDir(): string | null {
  if (process.env.PATTER_TELEMETRY_STATE_DIR) return null;
  const xdg = process.env.XDG_STATE_HOME;
  return xdg && xdg.length > 0 ? xdg : null;
}

function statePath(): string {
  return path.join(stateDir(), 'install-id');
}

/**
 * The persisted anonymous install id (random UUID, created once). Best-effort:
 * an unwritable filesystem degrades to the per-process run id.
 */
export function installId(): string {
  if (cachedInstallId !== null) return cachedInstallId;

  const p = statePath();
  try {
    const existing = fs.readFileSync(p, 'utf8').trim();
    if (HEX32.test(existing)) {
      cachedInstallId = existing;
      return cachedInstallId;
    }
  } catch {
    // not present / unreadable — fall through to create one
  }

  const legacyDir = legacyStateDir();
  if (legacyDir !== null) {
    const legacy = path.join(legacyDir, 'install-id');
    let existing = '';
    try {
      existing = fs.readFileSync(legacy, 'utf8').trim();
    } catch {
      existing = '';
    }
    if (HEX32.test(existing)) {
      // Migrate the pre-0.6.8 id so the install keeps counting as one,
      // preserving the file mtime that feeds daysSinceInstallBucket.
      try {
        fs.mkdirSync(path.dirname(p), { recursive: true });
        fs.writeFileSync(p, existing, 'utf8');
        const stat = fs.statSync(legacy);
        fs.utimesSync(p, stat.atime, stat.mtime);
      } catch {
        /* best-effort */
      }
      cachedInstallId = existing;
      return cachedInstallId;
    }
  }

  const newId = randomUUID().replace(/-/g, '');
  try {
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, newId, 'utf8');
    cachedInstallId = newId;
  } catch {
    // read-only / sandboxed FS: fall back to the per-process id (not persisted).
    cachedInstallId = RUN_ID;
  }
  return cachedInstallId;
}

function versionPath(): string {
  return path.join(path.dirname(statePath()), 'version');
}

/**
 * Return the last sdk_version this install reported ('' on first run), then
 * record `current` for next time. Powers the upgrade funnel. Best-effort.
 */
export function previousVersion(current: string): string {
  const p = versionPath();
  let prev = '';
  try {
    prev = fs.readFileSync(p, 'utf8').trim();
  } catch {
    prev = '';
  }
  if (prev === '') {
    const legacyDir = legacyStateDir();
    if (legacyDir !== null) {
      try {
        prev = fs.readFileSync(path.join(legacyDir, 'version'), 'utf8').trim();
      } catch {
        prev = '';
      }
    }
  }
  try {
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, current, 'utf8');
  } catch {
    /* read-only FS */
  }
  return VERSION_RE.test(prev) ? prev : '';
}

/** Coarse age of this install from the install-id file mtime (0/1_7/8_30/30_plus). */
export function daysSinceInstallBucket(): string {
  let mtimeMs: number;
  try {
    mtimeMs = fs.statSync(statePath()).mtimeMs;
  } catch {
    return '0';
  }
  const days = Math.max(0, Math.floor((Date.now() - mtimeMs) / 86_400_000));
  if (days === 0) return '0';
  if (days <= 7) return '1_7';
  if (days <= 30) return '8_30';
  return '30_plus';
}

/**
 * Seconds since this install was created, from the install-id file mtime.
 * Reuses the same mtime seam as `daysSinceInstallBucket`. Returns `undefined`
 * on an unreadable / read-only filesystem (the caller buckets that to
 * `'unknown'`). Best-effort and never throws. Mirrors `install_age_seconds`
 * in `install_id.py`.
 */
export function installAgeSeconds(): number | undefined {
  let mtimeMs: number;
  try {
    mtimeMs = fs.statSync(statePath()).mtimeMs;
  } catch {
    return undefined;
  }
  return Math.max(0, (Date.now() - mtimeMs) / 1000);
}

function firstRunPath(): string {
  return path.join(path.dirname(statePath()), 'first-run');
}

/**
 * Return `true` exactly once per install — on the run that first marks it.
 * Powers the `first_run` activation event. Idempotent: the first call writes a
 * marker and returns `true`; later calls return `false`. Best-effort — an
 * unwritable filesystem returns `false` (never emit `first_run` repeatedly).
 * MUST only be called on the telemetry-enabled path (opting out never touches the
 * filesystem). Mirrors `is_first_run` in `install_id.py`.
 */
export function isFirstRun(): boolean {
  const p = firstRunPath();
  try {
    if (fs.existsSync(p)) return false;
  } catch {
    return false;
  }
  const legacyDir = legacyStateDir();
  if (legacyDir !== null) {
    try {
      // A pre-0.6.8 marker in the bare XDG root — never re-emit first_run.
      if (fs.existsSync(path.join(legacyDir, 'first-run'))) return false;
    } catch {
      return false;
    }
  }
  try {
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, '1', 'utf8');
    return true;
  } catch {
    return false;
  }
}

function optOutPath(): string {
  return path.join(path.dirname(statePath()), 'telemetry-disabled');
}

/**
 * Whether a persisted opt-out marker exists (`getpatter telemetry disable`).
 * Read-only — checking consent never writes. Mirrors `is_opted_out` in Python.
 */
export function isOptedOut(): boolean {
  try {
    if (fs.existsSync(optOutPath())) return true;
  } catch {
    /* fall through to the legacy check */
  }
  // Honor a marker written by pre-0.6.8 builds into the bare $XDG_STATE_HOME
  // root: an opt-out must survive the state-dir move.
  const legacyDir = legacyStateDir();
  if (legacyDir === null) return false;
  try {
    return fs.existsSync(path.join(legacyDir, 'telemetry-disabled'));
  } catch {
    return false;
  }
}

/**
 * Create or remove the persisted opt-out marker. Used by the
 * `getpatter telemetry disable/enable` CLI. Lets filesystem errors propagate so
 * the CLI can report a failure. Mirrors `set_opt_out` in Python.
 */
export function setOptOut(disabled: boolean): void {
  const p = optOutPath();
  if (disabled) {
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, '1', 'utf8');
  } else {
    try {
      fs.unlinkSync(p);
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
    }
    // Also clear a pre-0.6.8 marker in the bare $XDG_STATE_HOME root —
    // isOptedOut honors it, so leaving it behind would pin telemetry off.
    const legacyDir = legacyStateDir();
    if (legacyDir !== null) {
      try {
        fs.unlinkSync(path.join(legacyDir, 'telemetry-disabled'));
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
      }
    }
  }
}

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

function statePath(): string {
  const base = process.env.PATTER_TELEMETRY_STATE_DIR || process.env.XDG_STATE_HOME;
  const root = base && base.length > 0 ? base : path.join(os.homedir(), '.getpatter');
  return path.join(root, 'install-id');
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
    return fs.existsSync(optOutPath());
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
  }
}

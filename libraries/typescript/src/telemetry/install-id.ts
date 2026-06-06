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

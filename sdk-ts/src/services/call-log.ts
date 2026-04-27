/**
 * Per-call filesystem logging for Patter voice agents.
 *
 * Opt-in, off by default. Enable by setting ``PATTER_LOG_DIR`` (absolute
 * path or ``"auto"`` for platform-default) before constructing ``Patter``.
 * When unset the CallLogger is a no-op — no files are written, no
 * directories are created.
 *
 * Layout:
 *
 *   <root>/calls/YYYY/MM/DD/<call_id>/
 *     metadata.json     envelope written at call start, updated at call end
 *     transcript.jsonl  one turn per line (role/text/ts/latency/cost)
 *     events.jsonl      operational events (tool_call, barge_in, error)
 *
 * Files are written atomically (tmp + rename) for ``metadata.json``; JSONL
 * files are append-only. All timestamps are UTC ISO-8601 with millisecond
 * precision. Phone numbers in ``metadata.json`` are masked by default via
 * ``maskPhoneNumber``.
 *
 * Schema matches ``sdk-py/getpatter/services/call_log.py`` for cross-SDK
 * compatibility (LiveKit/Pipecat-compatible trace shape; fields map to
 * OpenTelemetry ``gen_ai.*`` semantic conventions).
 *
 * Environment variables:
 *
 * - ``PATTER_LOG_DIR``             root directory or ``"auto"``
 * - ``PATTER_LOG_RETENTION_DAYS``  auto-cleanup threshold (default ``30``;
 *                                  ``0`` = keep forever)
 * - ``PATTER_LOG_REDACT_PHONE``    ``full`` | ``mask`` (default) | ``hash_only``
 */

import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import { promises as fsp } from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { getLogger } from '../logger';
import { sanitizeLogValue, maskPhoneNumber } from '../stream-handler';

export const SCHEMA_VERSION = '1.0';
export const DEFAULT_RETENTION_DAYS = 30;

export type RedactMode = 'full' | 'mask' | 'hash_only';

// --- Paths ---------------------------------------------------------------

function xdgDataHome(): string {
  return process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
}

function platformDefaultRoot(): string {
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'patter');
  }
  if (process.platform === 'win32') {
    const localAppData = process.env.LOCALAPPDATA;
    if (localAppData) return path.join(localAppData, 'patter');
    return path.join(os.homedir(), 'AppData', 'Local', 'patter');
  }
  return path.join(xdgDataHome(), 'patter');
}

/**
 * Resolve the log root directory, or ``null`` if logging is disabled.
 *
 * Precedence:
 *   1. ``explicit`` argument
 *   2. ``PATTER_LOG_DIR`` env var (``"auto"`` → platform default)
 *   3. disabled (return ``null``)
 */
export function resolveLogRoot(explicit?: string | null): string | null {
  const value = explicit ?? process.env.PATTER_LOG_DIR;
  if (!value) return null;
  if (value.trim().toLowerCase() === 'auto') return platformDefaultRoot();
  if (value.startsWith('~')) return path.join(os.homedir(), value.slice(1));
  return value;
}

function retentionDays(): number {
  const raw = process.env.PATTER_LOG_RETENTION_DAYS;
  if (raw === undefined) return DEFAULT_RETENTION_DAYS;
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed)) return DEFAULT_RETENTION_DAYS;
  return Math.max(0, parsed);
}

function redactMode(): RedactMode {
  const raw = (process.env.PATTER_LOG_REDACT_PHONE || 'mask').trim().toLowerCase();
  if (raw === 'full' || raw === 'mask' || raw === 'hash_only') return raw;
  return 'mask';
}

function redactPhone(raw: string): string {
  if (!raw) return '';
  const mode = redactMode();
  if (mode === 'full') return raw;
  if (mode === 'hash_only') {
    return 'sha256:' + crypto.createHash('sha256').update(raw, 'utf8').digest('hex').slice(0, 16);
  }
  return maskPhoneNumber(raw);
}

/** RFC 3339 / ISO 8601 UTC timestamp with millisecond precision. */
function utcIso(tsSeconds?: number): string {
  const ms = tsSeconds !== undefined ? tsSeconds * 1000 : Date.now();
  return new Date(ms).toISOString();
}

// --- IO helpers ----------------------------------------------------------

async function atomicWriteJson(filePath: string, payload: unknown): Promise<void> {
  const dir = path.dirname(filePath);
  await fsp.mkdir(dir, { recursive: true });
  const tmp = path.join(dir, `.tmp.${process.pid}.${crypto.randomBytes(4).toString('hex')}.json`);
  try {
    const handle = await fsp.open(tmp, 'w');
    try {
      await handle.writeFile(JSON.stringify(payload, null, 2) + '\n', { encoding: 'utf8' });
      await handle.sync();
    } finally {
      await handle.close();
    }
    await fsp.rename(tmp, filePath);
  } catch (err) {
    try {
      await fsp.unlink(tmp);
    } catch {
      // ignore
    }
    throw err;
  }
}

async function appendJsonl(filePath: string, record: unknown): Promise<void> {
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  await fsp.appendFile(filePath, JSON.stringify(record) + '\n', { encoding: 'utf8' });
}

// --- Types ---------------------------------------------------------------

export interface CallStartInput {
  readonly caller?: string;
  readonly callee?: string;
  readonly telephonyProvider?: string;
  readonly providerMode?: string;
  readonly agent?: Record<string, unknown>;
  readonly traceId?: string | null;
}

export interface CallEndInput {
  readonly durationSeconds?: number;
  readonly turns?: number;
  readonly cost?: Record<string, unknown> | null;
  readonly latency?: Record<string, unknown> | null;
  readonly status?: string;
  readonly error?: string | null;
}

export interface CallTurnRecord {
  readonly timestamp?: number;
  readonly [key: string]: unknown;
}

// --- CallLogger ----------------------------------------------------------

/**
 * Per-call filesystem logger.
 *
 * Instantiate once per server (or pass ``null`` to disable). All methods
 * degrade gracefully: errors during file writes are logged but never
 * raised to the caller — logging must not take down a live phone call.
 */
export class CallLogger {
  private readonly root: string | null;

  constructor(root: string | null | undefined) {
    if (!root) {
      this.root = null;
      return;
    }
    const resolved = root.startsWith('~') ? path.join(os.homedir(), root.slice(1)) : root;
    try {
      fs.mkdirSync(resolved, { recursive: true });
      this.root = resolved;
      getLogger().info(`Call logs: ${resolved}`);
    } catch (err) {
      getLogger().warn(
        `Could not create call log root ${resolved}: ${sanitizeLogValue(String(err))}`,
      );
      this.root = null;
    }
  }

  get enabled(): boolean {
    return this.root !== null;
  }

  private callDir(callId: string, startedAtSeconds?: number): string | null {
    if (this.root === null) return null;
    const ms = startedAtSeconds !== undefined ? startedAtSeconds * 1000 : Date.now();
    const dt = new Date(ms);
    const year = String(dt.getUTCFullYear()).padStart(4, '0');
    const month = String(dt.getUTCMonth() + 1).padStart(2, '0');
    const day = String(dt.getUTCDate()).padStart(2, '0');
    const safeId = sanitizeLogValue(callId, 64).replace(/\//g, '_') || 'unknown';
    return path.join(this.root, 'calls', year, month, day, safeId);
  }

  async logCallStart(callId: string, input: CallStartInput = {}): Promise<void> {
    if (!this.enabled) return;
    const startedAt = Date.now() / 1000;
    const dir = this.callDir(callId, startedAt);
    if (dir === null) return;
    const metadata = {
      schema_version: SCHEMA_VERSION,
      call_id: callId,
      trace_id: input.traceId ?? null,
      started_at: utcIso(startedAt),
      ended_at: null,
      duration_ms: null,
      status: 'in_progress',
      caller: redactPhone(input.caller ?? ''),
      callee: redactPhone(input.callee ?? ''),
      telephony_provider: input.telephonyProvider ?? '',
      provider_mode: input.providerMode ?? '',
      agent: input.agent ?? {},
      turns: 0,
      cost: null,
      latency: null,
      error: null,
    };
    try {
      await atomicWriteJson(path.join(dir, 'metadata.json'), metadata);
    } catch (err) {
      getLogger().warn(`call_log write failed (${sanitizeLogValue(callId)}): ${sanitizeLogValue(String(err))}`);
    }
    // Sample-based sweep (~2%) so we don't need a daemon.
    if (crypto.randomBytes(1)[0] < 5) {
      this.sweepOldDays();
    }
  }

  async logTurn(callId: string, turn: CallTurnRecord): Promise<void> {
    if (!this.enabled) return;
    const dir = this.callDir(callId);
    if (dir === null) return;
    const record = {
      schema_version: SCHEMA_VERSION,
      ts: utcIso(typeof turn.timestamp === 'number' ? turn.timestamp : undefined),
      ...turn,
    };
    try {
      await appendJsonl(path.join(dir, 'transcript.jsonl'), record);
    } catch (err) {
      getLogger().warn(
        `call_log turn write failed (${sanitizeLogValue(callId)}): ${sanitizeLogValue(String(err))}`,
      );
    }
  }

  async logEvent(callId: string, eventType: string, payload: Record<string, unknown> = {}): Promise<void> {
    if (!this.enabled) return;
    const dir = this.callDir(callId);
    if (dir === null) return;
    const record = {
      schema_version: SCHEMA_VERSION,
      ts: utcIso(),
      type: eventType,
      data: payload,
    };
    try {
      await appendJsonl(path.join(dir, 'events.jsonl'), record);
    } catch (err) {
      getLogger().warn(
        `call_log event write failed (${sanitizeLogValue(callId)}): ${sanitizeLogValue(String(err))}`,
      );
    }
  }

  async logCallEnd(callId: string, input: CallEndInput = {}): Promise<void> {
    if (!this.enabled) return;
    const dir = this.callDir(callId);
    if (dir === null) return;
    const metadataPath = path.join(dir, 'metadata.json');
    let existing: Record<string, unknown> = {};
    try {
      existing = JSON.parse(await fsp.readFile(metadataPath, 'utf8')) as Record<string, unknown>;
    } catch {
      existing = {
        schema_version: SCHEMA_VERSION,
        call_id: callId,
        started_at: null,
      };
    }
    const merged = {
      ...existing,
      ended_at: utcIso(),
      duration_ms:
        input.durationSeconds !== undefined
          ? Math.round(input.durationSeconds * 1000 * 10) / 10
          : null,
      status: input.status ?? 'completed',
      turns: input.turns ?? null,
      cost: input.cost ?? null,
      latency: input.latency ?? null,
      error: input.error ?? null,
    };
    try {
      await atomicWriteJson(metadataPath, merged);
    } catch (err) {
      getLogger().warn(
        `call_log finalize failed (${sanitizeLogValue(callId)}): ${sanitizeLogValue(String(err))}`,
      );
    }
  }

  // --- Retention ---------------------------------------------------------

  private sweepOldDays(): void {
    if (this.root === null) return;
    const days = retentionDays();
    if (days === 0) return;
    const cutoff = Date.now() / 1000 - days * 86400;
    const callsRoot = path.join(this.root, 'calls');
    if (!fs.existsSync(callsRoot)) return;
    try {
      for (const yearName of fs.readdirSync(callsRoot)) {
        if (!/^\d+$/.test(yearName)) continue;
        const yearDir = path.join(callsRoot, yearName);
        if (!fs.statSync(yearDir).isDirectory()) continue;
        for (const monthName of fs.readdirSync(yearDir)) {
          if (!/^\d+$/.test(monthName)) continue;
          const monthDir = path.join(yearDir, monthName);
          if (!fs.statSync(monthDir).isDirectory()) continue;
          for (const dayName of fs.readdirSync(monthDir)) {
            if (!/^\d+$/.test(dayName)) continue;
            const dayDir = path.join(monthDir, dayName);
            const y = Number.parseInt(yearName, 10);
            const m = Number.parseInt(monthName, 10);
            const d = Number.parseInt(dayName, 10);
            const ts = Date.UTC(y, m - 1, d) / 1000;
            if (ts < cutoff) {
              rmTree(dayDir);
            }
          }
          try {
            if (fs.readdirSync(monthDir).length === 0) fs.rmdirSync(monthDir);
          } catch {
            // ignore
          }
        }
        try {
          if (fs.readdirSync(yearDir).length === 0) fs.rmdirSync(yearDir);
        } catch {
          // ignore
        }
      }
    } catch (err) {
      getLogger().debug(`call_log sweep failed: ${sanitizeLogValue(String(err))}`);
    }
  }
}

function rmTree(target: string): void {
  try {
    for (const child of fs.readdirSync(target)) {
      const childPath = path.join(target, child);
      const stat = fs.lstatSync(childPath);
      if (stat.isDirectory()) {
        rmTree(childPath);
      } else {
        try {
          fs.unlinkSync(childPath);
        } catch {
          // ignore
        }
      }
    }
    fs.rmdirSync(target);
  } catch {
    // ignore
  }
}

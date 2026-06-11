/**
 * Fire-and-forget anonymous telemetry client.
 *
 * Design invariants (this guards live phone calls — see the rules):
 *  - Never blocks the call path: `record` only buffers + schedules a microtask
 *    flush; it never awaits a network call inline.
 *  - Never throws into user code: every entry point swallows all errors.
 *  - Identical behaviour offline: a DNS failure / timeout / non-2xx is dropped.
 *  - Bounded memory: a fixed-size buffer drops the oldest event when full.
 *  - Flush during normal operation: `serve()` calls `flushPending()` once the
 *    server is up, so events buffered at construction ship promptly. The
 *    `process.once('beforeExit', ...)` hook is a genuinely best-effort fallback
 *    for construct-and-never-serve scripts — unlike the Python `atexit` path it
 *    cannot synchronously guarantee delivery before the process exits.
 *
 * Disabled instances are cheap no-ops. Uses the global `fetch` (Node 18+) — no
 * new dependency. Mirrors `getpatter/telemetry/client.py`.
 */

import { getLogger } from '../logger';
import { isEnabled } from './consent';
import { buildEvent, type Dimensions, type TelemetryEvent } from './events';
import { isTruthy } from './env';

export const DEFAULT_ENDPOINT = 'https://telemetry.getpatter.com/v1/ingest';

const TIMEOUT_MS = 3000;
const BUFFER_MAX = 256;
// The relay rejects batches larger than 64 events per request — a full buffer
// must ship as multiple POSTs or events 65..256 silently vanish server-side.
const MAX_EVENTS_PER_POST = 64;

let noticeShown = false;
// WeakRefs so a client whose owning `Patter` was discarded is garbage-collected
// and pruned, mirroring the Python `WeakSet` registry — a long-running process
// that creates and drops many `Patter` instances does not accumulate dead clients.
const liveClients = new Set<WeakRef<TelemetryClient>>();
let exitHookRegistered = false;

// Strong references to clients that still hold undelivered events (mirrors the
// Python `_PENDING_FLUSH` set). The registry above is deliberately weak so a
// discarded `Patter`'s client can be collected — but a client constructed
// fire-and-forget (`new TelemetryClient(...).record(...)` with no reference
// held) must not take its buffered events to the grave before a flush delivers
// them. A client is held strongly from its first buffered event until the
// buffer drains, so the lifetime is bounded by the next flush.
const pendingFlush = new Set<TelemetryClient>();

function showNoticeOnce(): void {
  if (noticeShown) return;
  noticeShown = true;
  getLogger().info(
    'Anonymous usage telemetry is on (no PII, no call content). Collected: ' +
      'a random anonymous install id, SDK version, language, OS family, runtime ' +
      'version, coarse feature flags, the composed stack (provider + model per ' +
      'layer), tool counts, integration category, and per-call duration, latency, ' +
      'cost, and error codes (no call content, no message text). ' +
      'Disable with PATTER_TELEMETRY_DISABLED=1, DO_NOT_TRACK=1, or telemetry: false. ' +
      'Details: https://docs.getpatter.com/telemetry',
  );
}

function registerExitHook(): void {
  if (exitHookRegistered) return;
  exitHookRegistered = true;
  process.once('beforeExit', () => {
    for (const ref of [...liveClients]) {
      const client = ref.deref();
      if (client) void client.close();
      else liveClients.delete(ref); // prune a GC'd client's dead ref
    }
  });
}

export interface TelemetryClientOptions {
  readonly sdkVersion: string;
  /** `new Patter({ telemetry })` value: undefined = default ON, false = opt-out. */
  readonly flag?: boolean;
  readonly endpoint?: string;
}

export class TelemetryClient {
  private readonly sdkVersion: string;
  private readonly enabledFlag: boolean;
  private readonly endpoint: string;
  private readonly debug: boolean;
  private readonly buffer: TelemetryEvent[] = [];
  private inflight: Promise<void> | null = null;
  private closed = false;
  private readonly selfRef: WeakRef<TelemetryClient> = new WeakRef(this);

  constructor(options: TelemetryClientOptions) {
    this.sdkVersion = options.sdkVersion;
    this.enabledFlag = isEnabled(options.flag);
    this.endpoint =
      options.endpoint ?? process.env.PATTER_TELEMETRY_ENDPOINT ?? DEFAULT_ENDPOINT;
    this.debug = isTruthy(process.env.PATTER_TELEMETRY_DEBUG);

    if (this.enabledFlag && !this.debug) {
      showNoticeOnce();
      registerExitHook();
      liveClients.add(this.selfRef);
    }
  }

  get enabled(): boolean {
    return this.enabledFlag;
  }

  /** Enqueue an event. Fire-and-forget; never throws, never blocks. */
  record(name: string, dimensions?: Dimensions): void {
    if (!this.enabledFlag || this.closed) return;

    let event: TelemetryEvent;
    try {
      event = buildEvent(name, { sdkVersion: this.sdkVersion, dimensions });
    } catch (err) {
      getLogger().debug('telemetry buildEvent failed', err);
      return;
    }

    if (this.debug) {
      // Print-without-send: the highest-trust audit feature.
      try {
        process.stderr.write(`[patter telemetry] ${JSON.stringify(event)}\n`);
      } catch {
        /* ignore */
      }
      return;
    }

    try {
      if (this.buffer.length >= BUFFER_MAX) this.buffer.shift(); // drop oldest
      this.buffer.push(event);
      pendingFlush.add(this); // survive GC until the buffer drains
      this.scheduleFlush();
    } catch (err) {
      getLogger().debug('telemetry enqueue failed', err);
    }
  }

  /**
   * Schedule a flush of any buffered events. Events recorded before the server
   * is running (e.g. at `new Patter(...)`) sit in the buffer; call this once the
   * server is up so they ship promptly. Cheap when disabled or buffer is empty.
   */
  flushPending(): void {
    if (!this.enabledFlag || this.debug) return;
    try {
      this.scheduleFlush();
    } catch (err) {
      getLogger().debug('telemetry flushPending failed', err);
    }
  }

  /** Flush remaining events (graceful shutdown). Never throws. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    liveClients.delete(this.selfRef);
    if (!this.enabledFlag || this.debug) {
      pendingFlush.delete(this);
      return;
    }
    try {
      // A flush scheduled by `record()` drains the buffer immediately, so
      // flushing again below would see nothing — await the in-flight delivery
      // first or a CLI that exits right after close() kills the POST mid-air.
      if (this.inflight) await this.inflight;
      await this.flush();
    } catch (err) {
      getLogger().debug('telemetry close flush failed', err);
    }
    pendingFlush.delete(this);
  }

  private scheduleFlush(): void {
    if (this.inflight) return;
    this.inflight = this.flush().finally(() => {
      this.inflight = null;
      // Events recorded while the POST was in flight are sitting in the buffer
      // with no flush scheduled (record() saw `inflight` and skipped) — chain
      // another flush or they strand until close()/process exit. Never chain
      // after close() began: close() awaits the in-flight flush and then drains
      // the buffer itself — a chained flush here would detach from close() and
      // die mid-air on a prompt process exit (mirrors Python's `not _closed`).
      if (this.buffer.length > 0 && !this.closed) this.scheduleFlush();
    });
    void this.inflight;
  }

  private async flush(): Promise<void> {
    if (this.buffer.length === 0) return;
    const events = this.buffer.splice(0, this.buffer.length);
    pendingFlush.delete(this); // nothing buffered — GC may reclaim us again

    try {
      // Ship in relay-sized chunks: a buffer larger than the relay's
      // per-request cap would otherwise be silently truncated server-side.
      for (let start = 0; start < events.length; start += MAX_EVENTS_PER_POST) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
        (timer as { unref?: () => void }).unref?.();
        try {
          await fetch(this.endpoint, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(events.slice(start, start + MAX_EVENTS_PER_POST)),
            signal: controller.signal,
          });
          // Status ignored — telemetry is best-effort and never load-bearing.
        } finally {
          clearTimeout(timer);
        }
      }
    } catch (err) {
      // Drop on any failure — including this flush's remaining chunks; do NOT
      // requeue (keeps offline behaviour identical).
      getLogger().debug('telemetry flush failed', err);
    }
  }
}

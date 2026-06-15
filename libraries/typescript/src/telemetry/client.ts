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

import { createHash } from 'node:crypto';

import { getLogger } from '../logger';
import { isEnabled } from './consent';
import {
  EVENT_CALL_COMPLETED,
  EVENT_CALL_STARTED,
  buildEvent,
  type Dimensions,
  type TelemetryEvent,
} from './events';
import { isTruthy, sampleRate } from './env';
import { runId } from './install-id';

export const DEFAULT_ENDPOINT = 'https://telemetry.getpatter.com/v1/ingest';

const TIMEOUT_MS = 3000;
const BUFFER_MAX = 256;
// The relay rejects batches larger than 64 events per request — a full buffer
// must ship as multiple POSTs or events 65..256 silently vanish server-side.
const MAX_EVENTS_PER_POST = 64;

// The ONLY events the client-side sampling gate may drop. Everything else
// (first_run, config_incomplete, sdk_initialized, cli_command, feature_used,
// agent_configured) is always delivered — those are low-frequency activation /
// usage signals, never high-volume call traffic. An *error* call_completed is
// also force-kept (see record()) so error rates stay unbiased.
const SAMPLEABLE_EVENTS = new Set<string>([EVENT_CALL_STARTED, EVENT_CALL_COMPLETED]);

/**
 * A stable per-run value in `[0, 1)` derived from this process's run id.
 *
 * Hashing the run id (constant for the process) makes the keep/drop decision
 * DETERMINISTIC PER RUN: the whole run consistently keeps or drops its
 * sampleable call events rather than flipping a per-event coin — which would
 * bias short runs. `Math.random` would NOT be deterministic per run. Any
 * failure degrades to `0` so the caller keeps the event (fail safe). Mirrors
 * Python's `_run_keep_ratio`.
 */
function runKeepRatio(): number {
  try {
    const digest = createHash('sha256').update(runId(), 'utf8').digest('hex').slice(0, 8);
    return parseInt(digest, 16) / 0xffffffff;
  } catch {
    return 0;
  }
}

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
      'layer), tool counts, integration category, a random per-call correlation ' +
      'id, and per-call duration, latency, cost, and error codes (no call ' +
      'content, no message text). ' +
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
  // Client-side sampling gate (computed ONCE at construction):
  //   * sampleRate — the effective rate in [0, 1] from the env.
  //   * keepSampled — the deterministic per-run keep/drop decision for the
  //     high-frequency call events. Because the seed is the run id (constant for
  //     the process) the whole run consistently keeps or drops them — never a
  //     per-event coin flip. rate >= 1.0 always keeps. See record().
  private readonly sampleRate: number;
  private readonly keepSampled: boolean;

  constructor(options: TelemetryClientOptions) {
    this.sdkVersion = options.sdkVersion;
    this.enabledFlag = isEnabled(options.flag);
    this.endpoint =
      options.endpoint ?? process.env.PATTER_TELEMETRY_ENDPOINT ?? DEFAULT_ENDPOINT;
    this.debug = isTruthy(process.env.PATTER_TELEMETRY_DEBUG);
    this.sampleRate = sampleRate();
    this.keepSampled = this.sampleRate >= 1.0 || runKeepRatio() < this.sampleRate;

    if (this.enabledFlag && !this.debug) {
      showNoticeOnce();
      registerExitHook();
      liveClients.add(this.selfRef);
    }
  }

  get enabled(): boolean {
    return this.enabledFlag;
  }

  /**
   * Enqueue an event. Fire-and-forget; never throws, never blocks.
   *
   * Applies the client-side sampling gate (see constructor): only the
   * high-frequency `call_started` / `call_completed` events may be dropped, and
   * only when this run's deterministic decision says so. An error
   * `call_completed` (`outcome === 'error'` or an `error_code` present) is
   * ALWAYS kept so error rates stay unbiased. A kept sampled event carries
   * `sample_rate` so analytics can extrapolate (weight `1/sample_rate`). All
   * other events bypass the gate entirely.
   */
  record(name: string, dimensions?: Dimensions): void {
    if (!this.enabledFlag || this.closed) return;

    // -- sampling gate (high-frequency call events only) --------------------
    let dims = dimensions;
    if (SAMPLEABLE_EVENTS.has(name) && this.sampleRate < 1.0) {
      // Force-keep error call_completed events regardless of rate — error rates
      // would be biased if errors were sampled like clean calls.
      const isError =
        name === EVENT_CALL_COMPLETED &&
        ((dims?.outcome === 'error') || (dims != null && 'error_code' in dims));
      if (!this.keepSampled && !isError) return; // sampled out for this run
      // Kept (by the run decision or because it's an error): stamp the rate so
      // analytics can extrapolate (weight = 1/rate). buildEvent keeps it (the
      // Schema phase added sample_rate to the numeric allowlist). Stamped only
      // when rate < 1.0 to keep payloads lean. New object — never mutate the
      // caller's dimensions.
      dims = { ...dims, sample_rate: this.sampleRate };
    }

    let event: TelemetryEvent;
    try {
      event = buildEvent(name, { sdkVersion: this.sdkVersion, dimensions: dims });
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

  /**
   * Flush buffered events and wait for delivery. Unlike `close()` the client
   * stays usable afterwards — for teardown paths that may serve again
   * (`Patter.disconnect()`). Bounded by the flush's own per-POST abort timer.
   * Mirrors Python's `drain()`.
   */
  async drain(): Promise<void> {
    if (!this.enabledFlag || this.debug || this.closed) return;
    try {
      if (this.inflight) await this.inflight;
      if (this.buffer.length > 0) await this.flush();
    } catch (err) {
      getLogger().debug('telemetry drain failed', err);
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

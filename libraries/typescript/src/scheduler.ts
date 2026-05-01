/**
 * Thin scheduling wrapper around node-cron (MIT).
 *
 *    import { scheduleCron, scheduleOnce } from 'getpatter';
 *
 *    const handle = scheduleCron('* /5 * * * *', async () => doWork());
 *    handle.cancel();
 *
 * node-cron is an optional dependency. This module imports it lazily so that
 * consumers who never schedule anything do not need it installed.
 */

import { getLogger } from './logger';

export type JobCallback = () => void | Promise<void>;

export interface ScheduleHandle {
  readonly jobId: string;
  cancel(): void;
  readonly pending: boolean;
}

interface CronTask {
  stop(): void;
  destroy?: () => void;
}

interface CronModule {
  schedule(
    expression: string,
    cb: () => void,
    options?: { scheduled?: boolean; timezone?: string },
  ): CronTask;
  validate(expression: string): boolean;
}

let cronModule: CronModule | null = null;
let loadError: Error | null = null;

async function loadCron(): Promise<CronModule> {
  if (cronModule) return cronModule;
  if (loadError) throw loadError;
  try {
    // node-cron ships without TypeScript types in its default export; we
    // intentionally keep this ``import`` untyped and narrow it to our local
    // ``CronModule`` interface above.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const imported: any = await import(
      /* @vite-ignore */ 'node-cron' as string
    );
    // Some bundlers wrap CJS exports under .default
    cronModule = (imported && imported.default
      ? (imported.default as CronModule)
      : (imported as CronModule));
    return cronModule;
  } catch (err) {
    loadError = new Error(
      "Scheduling requires the 'node-cron' package. Install with: npm install node-cron",
    );
    throw loadError;
  }
}

function wrapCallback(cb: JobCallback): () => void {
  return () => {
    try {
      const result = cb();
      if (result && typeof (result as Promise<void>).catch === 'function') {
        (result as Promise<void>).catch((err) =>
          getLogger().error(`Scheduled callback threw: ${String(err)}`),
        );
      }
    } catch (err) {
      getLogger().error(`Scheduled callback threw: ${String(err)}`);
    }
  };
}

/** Schedule ``callback`` on a cron expression (node-cron dialect).
 *
 * Returns a ``ScheduleHandle`` synchronously (parity with Python
 * ``schedule_cron``). The handle is "pending" until the lazy ``node-cron``
 * import resolves; cancelling the handle before then discards the pending
 * job cleanly. If ``node-cron`` is not installed, the returned promise
 * attached to ``.ready`` rejects with a helpful install message.
 */
export function scheduleCron(cron: string, callback: JobCallback): ScheduleHandle {
  let cancelled = false;
  let task: CronTask | null = null;
  const jobId = `cron-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

  loadCron()
    .then((cm) => {
      if (cancelled) return;
      if (!cm.validate(cron)) {
        throw new Error(`Invalid cron expression: ${cron}`);
      }
      task = cm.schedule(cron, wrapCallback(callback));
    })
    .catch((err) => getLogger().error(`scheduleCron failed: ${String(err)}`));

  return {
    jobId,
    cancel(): void {
      if (cancelled) return;
      cancelled = true;
      if (task) {
        try { task.stop(); } catch { /* ignore */ }
        try { task.destroy?.(); } catch { /* ignore */ }
      }
    },
    get pending(): boolean {
      return !cancelled;
    },
  };
}

/** Schedule ``callback`` once at the given date. */
export function scheduleOnce(at: Date, callback: JobCallback): ScheduleHandle {
  const delayMs = at.getTime() - Date.now();
  let cancelled = false;
  let done = false;
  const timer = setTimeout(() => {
    if (cancelled) return;
    done = true;
    wrapCallback(callback)();
  }, Math.max(0, delayMs));

  return {
    jobId: `once-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    cancel(): void {
      cancelled = true;
      clearTimeout(timer);
    },
    get pending(): boolean {
      return !cancelled && !done;
    },
  };
}

/**
 * Schedule ``callback`` on a recurring interval.
 *
 * Accepts either a millisecond number (legacy, matches the original TS API)
 * or an object with ``seconds`` / ``intervalMs`` for parity with Python's
 * ``schedule_interval(seconds=...)``.
 *
 * Examples:
 *   scheduleInterval(5000, cb)              // 5 s, legacy
 *   scheduleInterval({ intervalMs: 5000 }, cb)
 *   scheduleInterval({ seconds: 5 }, cb)    // parity with Python
 */
export function scheduleInterval(
  intervalOrOpts: number | { seconds?: number; intervalMs?: number },
  callback: JobCallback,
): ScheduleHandle {
  let intervalMs: number;
  if (typeof intervalOrOpts === 'number') {
    intervalMs = intervalOrOpts;
  } else if (intervalOrOpts.intervalMs !== undefined) {
    intervalMs = intervalOrOpts.intervalMs;
  } else if (intervalOrOpts.seconds !== undefined) {
    intervalMs = intervalOrOpts.seconds * 1000;
  } else {
    throw new Error('scheduleInterval requires seconds or intervalMs');
  }
  if (intervalMs <= 0) throw new Error('interval must be positive');
  let cancelled = false;
  const wrapped = wrapCallback(callback);
  const timer = setInterval(() => {
    if (!cancelled) wrapped();
  }, intervalMs);

  return {
    jobId: `interval-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    cancel(): void {
      cancelled = true;
      clearInterval(timer);
    },
    get pending(): boolean {
      return !cancelled;
    },
  };
}

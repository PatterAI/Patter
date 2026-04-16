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

/** Schedule ``callback`` on a cron expression (node-cron dialect). */
export async function scheduleCron(
  cron: string,
  callback: JobCallback,
): Promise<ScheduleHandle> {
  const cm = await loadCron();
  if (!cm.validate(cron)) {
    throw new Error(`Invalid cron expression: ${cron}`);
  }
  const task = cm.schedule(cron, wrapCallback(callback));
  return makeHandle(`cron-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, task);
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

/** Schedule ``callback`` every ``intervalMs`` milliseconds. */
export function scheduleInterval(intervalMs: number, callback: JobCallback): ScheduleHandle {
  if (intervalMs <= 0) throw new Error('intervalMs must be positive');
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

function makeHandle(jobId: string, task: CronTask): ScheduleHandle {
  let cancelled = false;
  return {
    jobId,
    cancel(): void {
      if (cancelled) return;
      cancelled = true;
      try {
        task.stop();
      } catch {
        /* ignore */
      }
      try {
        task.destroy?.();
      } catch {
        /* ignore */
      }
    },
    get pending(): boolean {
      return !cancelled;
    },
  };
}

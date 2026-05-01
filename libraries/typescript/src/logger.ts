/**
 * Process-wide logger used by the SDK.
 *
 * Provides the in-library logger abstraction (`getLogger`/`setLogger`) and
 * default console-based implementation. Library code MUST use these helpers
 * rather than calling `console.*` directly so applications can route logs.
 */

/** Minimal logger interface implemented by the default console logger and any user-supplied replacement. */
export interface Logger {
  info(message: string, ...args: unknown[]): void;
  warn(message: string, ...args: unknown[]): void;
  error(message: string, ...args: unknown[]): void;
  debug(message: string, ...args: unknown[]): void;
}

const defaultLogger: Logger = {
  info: (msg, ...args) => console.info(`[PATTER] ${msg}`, ...args),
  warn: (msg, ...args) => console.warn(`[PATTER] WARNING: ${msg}`, ...args),
  error: (msg, ...args) => console.error(`[PATTER] ERROR: ${msg}`, ...args),
  debug: () => {},
};

let currentLogger: Logger = defaultLogger;

/** Return the active logger (defaults to a console-backed implementation). */
export function getLogger(): Logger {
  return currentLogger;
}

/** Replace the process-wide logger; useful for routing SDK logs into a host app's logger. */
export function setLogger(logger: Logger): void {
  currentLogger = logger;
}

export interface Logger {
  info(message: string, ...args: unknown[]): void;
  warn(message: string, ...args: unknown[]): void;
  error(message: string, ...args: unknown[]): void;
  debug(message: string, ...args: unknown[]): void;
}

const defaultLogger: Logger = {
  info: (msg, ...args) => console.log(`[PATTER] ${msg}`, ...args),
  warn: (msg, ...args) => console.warn(`[PATTER] WARNING: ${msg}`, ...args),
  error: (msg, ...args) => console.error(`[PATTER] ERROR: ${msg}`, ...args),
  debug: () => {},
};

let currentLogger: Logger = defaultLogger;

export function getLogger(): Logger {
  return currentLogger;
}

export function setLogger(logger: Logger): void {
  currentLogger = logger;
}

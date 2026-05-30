/**
 * Per-tool circuit breaker for the Patter SDK.
 *
 * Trips OPEN after N consecutive failures, rejects calls for a cooldown
 * window so a flaky downstream (DB outage, vendor API rate-limit, dead
 * webhook) doesn't burn LLM tokens on retries that will keep failing.
 * After the cooldown elapses the next call probes (HALF_OPEN); a success
 * resets to CLOSED, a failure reopens. The model receives a structured
 * ``{ error, fallback: true }`` JSON in all rejection paths so it can
 * recover gracefully instead of waiting forever.
 *
 * Lightweight in-memory implementation — one ``CircuitBreakerRegistry``
 * per ``DefaultToolExecutor``, state is per tool name. Not persisted
 * across process restarts (intentional — voice calls are too short for
 * persistence to matter).
 */

/** Lifecycle states for the breaker. */
export const CircuitBreakerState = {
  CLOSED: 'closed',
  OPEN: 'open',
  HALF_OPEN: 'half_open',
} as const;
export type CircuitBreakerState =
  (typeof CircuitBreakerState)[keyof typeof CircuitBreakerState];

/** Tunables for a single per-tool breaker. */
export interface CircuitBreakerOptions {
  /** Consecutive failures that flip CLOSED → OPEN. ``0`` disables. */
  failureThreshold?: number;
  /** Time (ms) the breaker stays OPEN before allowing a probe. */
  cooldownMs?: number;
}

const DEFAULT_FAILURE_THRESHOLD = 5;
const DEFAULT_COOLDOWN_MS = 30_000;

interface PerToolState {
  state: CircuitBreakerState;
  consecutiveFailures: number;
  openedAt: number;
  /** True while a HALF_OPEN probe call is already in-flight. */
  probeInFlight: boolean;
}

/** Per-name registry tracking circuit state for a fleet of tools. */
export class CircuitBreakerRegistry {
  private readonly threshold: number;
  private readonly cooldownMs: number;
  private readonly state = new Map<string, PerToolState>();
  /** Inject for deterministic tests; defaults to ``Date.now()``. */
  private readonly clock: () => number;

  constructor(opts: CircuitBreakerOptions = {}, clock: () => number = Date.now) {
    this.threshold = opts.failureThreshold ?? DEFAULT_FAILURE_THRESHOLD;
    this.cooldownMs = opts.cooldownMs ?? DEFAULT_COOLDOWN_MS;
    this.clock = clock;
  }

  /** Returns ``true`` when this tool is currently allowed to run. */
  allow(toolName: string): boolean {
    if (this.threshold <= 0) return true;
    const s = this.state.get(toolName);
    if (!s) return true;
    if (s.state === CircuitBreakerState.CLOSED) return true;
    if (s.state === CircuitBreakerState.OPEN) {
      if (this.clock() - s.openedAt >= this.cooldownMs) {
        // Cooldown elapsed — allow exactly one probe to determine if
        // the downstream has recovered.
        s.state = CircuitBreakerState.HALF_OPEN;
        s.probeInFlight = true;
        return true;
      }
      return false;
    }
    // HALF_OPEN — allow only one in-flight probe at a time.
    if (s.probeInFlight) return false;
    s.probeInFlight = true;
    return true;
  }

  /** Mark a successful execution. Resets the breaker to CLOSED. */
  recordSuccess(toolName: string): void {
    const s = this.state.get(toolName);
    if (!s) return;
    s.state = CircuitBreakerState.CLOSED;
    s.consecutiveFailures = 0;
    s.openedAt = 0;
    s.probeInFlight = false;
  }

  /** Mark a failed execution; trips OPEN once threshold is reached. */
  recordFailure(toolName: string): void {
    if (this.threshold <= 0) return;
    let s = this.state.get(toolName);
    if (!s) {
      s = { state: CircuitBreakerState.CLOSED, consecutiveFailures: 0, openedAt: 0, probeInFlight: false };
      this.state.set(toolName, s);
    }
    s.consecutiveFailures += 1;
    if (s.consecutiveFailures >= this.threshold) {
      s.state = CircuitBreakerState.OPEN;
      s.openedAt = this.clock();
      s.probeInFlight = false;
    }
  }

  /**
   * Time until the breaker transitions OPEN → HALF_OPEN, in ms. Returns
   * ``0`` when the breaker is currently allowing calls. Useful for
   * tests and the structured rejection JSON.
   */
  timeUntilHalfOpen(toolName: string): number {
    const s = this.state.get(toolName);
    if (!s || s.state !== CircuitBreakerState.OPEN) return 0;
    const elapsed = this.clock() - s.openedAt;
    return Math.max(0, this.cooldownMs - elapsed);
  }

  /** Snapshot for debugging / metrics. */
  snapshot(toolName: string): PerToolState | null {
    const s = this.state.get(toolName);
    return s ? { ...s } : null;
  }
}

import { describe, it, expect } from 'vitest';
import {
  CircuitBreakerRegistry,
  CircuitBreakerState,
} from '../src/tools/circuit-breaker';

/**
 * Deterministic clock helper. Tests advance ``now`` explicitly instead
 * of using ``setTimeout`` so they finish in milliseconds and survive
 * loaded CI runners.
 */
function makeFakeClock(initial = 1_000_000): { now: () => number; advance: (ms: number) => void; set: (t: number) => void } {
  let t = initial;
  return {
    now: () => t,
    advance: (ms: number) => { t += ms; },
    set: (n: number) => { t = n; },
  };
}

describe('[unit] CircuitBreakerRegistry', () => {
  it('starts CLOSED and allows the first call for any tool', () => {
    const breaker = new CircuitBreakerRegistry();
    expect(breaker.allow('book_appointment')).toBe(true);
    expect(breaker.snapshot('book_appointment')).toBeNull();
  });

  it('stays CLOSED on success — consecutiveFailures stays 0', () => {
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 3 });
    breaker.recordSuccess('book');
    expect(breaker.allow('book')).toBe(true);
    expect(breaker.snapshot('book')?.consecutiveFailures ?? 0).toBe(0);
  });

  it('opens after `failureThreshold` consecutive failures', () => {
    const clock = makeFakeClock();
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 3, cooldownMs: 5_000 }, clock.now);
    breaker.recordFailure('book');
    breaker.recordFailure('book');
    expect(breaker.allow('book')).toBe(true);  // 2 < 3, still closed
    breaker.recordFailure('book');
    expect(breaker.allow('book')).toBe(false); // 3 >= 3 → OPEN
    expect(breaker.snapshot('book')?.state).toBe(CircuitBreakerState.OPEN);
  });

  it('resets to CLOSED on a successful call after intermediate failures', () => {
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 3 });
    breaker.recordFailure('book');
    breaker.recordFailure('book');
    breaker.recordSuccess('book');
    breaker.recordFailure('book');
    breaker.recordFailure('book');
    expect(breaker.allow('book')).toBe(true);  // counter reset → 2 failures only
  });

  it('transitions OPEN → HALF_OPEN after cooldown elapses', () => {
    const clock = makeFakeClock();
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 2, cooldownMs: 10_000 }, clock.now);
    breaker.recordFailure('book');
    breaker.recordFailure('book');
    expect(breaker.allow('book')).toBe(false);

    clock.advance(9_999);
    expect(breaker.allow('book')).toBe(false);  // still in cooldown

    clock.advance(2);  // total 10_001 ≥ cooldown
    expect(breaker.allow('book')).toBe(true);
    expect(breaker.snapshot('book')?.state).toBe(CircuitBreakerState.HALF_OPEN);
  });

  it('HALF_OPEN → CLOSED on probe success', () => {
    const clock = makeFakeClock();
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 2, cooldownMs: 1_000 }, clock.now);
    breaker.recordFailure('book');
    breaker.recordFailure('book');
    clock.advance(1_001);
    expect(breaker.allow('book')).toBe(true);
    breaker.recordSuccess('book');
    expect(breaker.snapshot('book')?.state).toBe(CircuitBreakerState.CLOSED);
    expect(breaker.snapshot('book')?.consecutiveFailures).toBe(0);
  });

  it('HALF_OPEN → OPEN on probe failure (with refreshed cooldown)', () => {
    const clock = makeFakeClock();
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 2, cooldownMs: 1_000 }, clock.now);
    breaker.recordFailure('book');
    breaker.recordFailure('book');
    clock.advance(1_001);
    expect(breaker.allow('book')).toBe(true);  // probe permitted
    breaker.recordFailure('book');             // probe failed
    expect(breaker.allow('book')).toBe(false); // back to OPEN
  });

  it('failureThreshold: 0 disables the breaker entirely', () => {
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 0, cooldownMs: 1 });
    for (let i = 0; i < 100; i++) breaker.recordFailure('book');
    expect(breaker.allow('book')).toBe(true);
  });

  it('tracks state per tool name independently', () => {
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 2 });
    breaker.recordFailure('a');
    breaker.recordFailure('a');
    expect(breaker.allow('a')).toBe(false);
    expect(breaker.allow('b')).toBe(true);
  });

  it('timeUntilHalfOpen returns 0 when CLOSED, and the remaining ms when OPEN', () => {
    const clock = makeFakeClock();
    const breaker = new CircuitBreakerRegistry({ failureThreshold: 2, cooldownMs: 5_000 }, clock.now);
    expect(breaker.timeUntilHalfOpen('book')).toBe(0);
    breaker.recordFailure('book');
    breaker.recordFailure('book');
    expect(breaker.timeUntilHalfOpen('book')).toBe(5_000);
    clock.advance(2_000);
    expect(breaker.timeUntilHalfOpen('book')).toBe(3_000);
  });
});

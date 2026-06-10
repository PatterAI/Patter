/**
 * Unit tests for ``createStreamIdleWatchdog`` (llm-loop.ts).
 *
 * The watchdog replaces the old fixed 30 s whole-stream ceiling on LLM
 * streams: it must abort only when NO data has arrived for the window,
 * re-arm on every ``touch()``, and stay quiet after ``clear()``.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createStreamIdleWatchdog, LLM_STREAM_IDLE_TIMEOUT_MS } from '../../src/llm-loop';

describe('createStreamIdleWatchdog', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('aborts after the idle window with no touches', () => {
    const idle = createStreamIdleWatchdog(1_000);
    expect(idle.signal.aborted).toBe(false);
    vi.advanceTimersByTime(999);
    expect(idle.signal.aborted).toBe(false);
    vi.advanceTimersByTime(1);
    expect(idle.signal.aborted).toBe(true);
    expect(idle.fired).toBe(true);
  });

  it('touch() re-arms the window so a steadily streaming response never aborts', () => {
    const idle = createStreamIdleWatchdog(1_000);
    for (let i = 0; i < 10; i++) {
      vi.advanceTimersByTime(900);
      idle.touch();
    }
    // 9 s elapsed in total — far beyond the window — but never 1 s idle.
    expect(idle.signal.aborted).toBe(false);
    expect(idle.fired).toBe(false);
    idle.clear();
  });

  it('clear() disarms the watchdog', () => {
    const idle = createStreamIdleWatchdog(1_000);
    idle.clear();
    vi.advanceTimersByTime(10_000);
    expect(idle.signal.aborted).toBe(false);
    expect(idle.fired).toBe(false);
  });

  it('touch() after firing is a no-op (fired stays latched)', () => {
    const idle = createStreamIdleWatchdog(1_000);
    vi.advanceTimersByTime(1_000);
    expect(idle.fired).toBe(true);
    idle.touch();
    vi.advanceTimersByTime(10_000);
    expect(idle.fired).toBe(true);
    expect(idle.signal.aborted).toBe(true);
  });

  it('defaults to the documented 30 s window', () => {
    expect(LLM_STREAM_IDLE_TIMEOUT_MS).toBe(30_000);
    const idle = createStreamIdleWatchdog();
    vi.advanceTimersByTime(LLM_STREAM_IDLE_TIMEOUT_MS - 1);
    expect(idle.signal.aborted).toBe(false);
    vi.advanceTimersByTime(1);
    expect(idle.signal.aborted).toBe(true);
  });
});

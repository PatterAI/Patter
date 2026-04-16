import { describe, it, expect } from 'vitest';
import {
  scheduleInterval,
  scheduleOnce,
} from '../src/scheduler';

describe('scheduleInterval', () => {
  it('fires the callback multiple times', async () => {
    let n = 0;
    const handle = scheduleInterval(30, () => {
      n += 1;
    });
    expect(handle.pending).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 120));
    handle.cancel();
    expect(n).toBeGreaterThanOrEqual(2);
    expect(handle.pending).toBe(false);
  });

  it('does not fire after cancel', async () => {
    let n = 0;
    const handle = scheduleInterval(25, () => {
      n += 1;
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    handle.cancel();
    const snapshot = n;
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(n).toBe(snapshot);
  });

  it('throws on non-positive interval', () => {
    expect(() => scheduleInterval(0, () => undefined)).toThrow();
    expect(() => scheduleInterval(-5, () => undefined)).toThrow();
  });

  it('propagates async callback errors to the logger, not the caller', async () => {
    let n = 0;
    const handle = scheduleInterval(20, async () => {
      n += 1;
      if (n === 1) throw new Error('boom');
    });
    await new Promise((resolve) => setTimeout(resolve, 80));
    handle.cancel();
    expect(n).toBeGreaterThanOrEqual(2);
  });
});

describe('scheduleOnce', () => {
  it('fires exactly once', async () => {
    let n = 0;
    scheduleOnce(new Date(Date.now() + 40), () => {
      n += 1;
    });
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(n).toBe(1);
  });

  it('does not fire when cancelled before firing', async () => {
    let n = 0;
    const handle = scheduleOnce(new Date(Date.now() + 80), () => {
      n += 1;
    });
    handle.cancel();
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(n).toBe(0);
  });

  it('exposes pending state', async () => {
    const handle = scheduleOnce(new Date(Date.now() + 30), () => undefined);
    expect(handle.pending).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(handle.pending).toBe(false);
  });
});

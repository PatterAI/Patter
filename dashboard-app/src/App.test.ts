import { describe, expect, it } from 'vitest';
import { avgP95 } from './App';
import { bucketHeadline, type MetricBucket } from './components/Metric';
import type { Call } from './components/CallTable';

function makeCall(id: string, overrides: Partial<Call> = {}): Call {
  return {
    id,
    status: 'ended',
    direction: 'inbound',
    from: `from-${id}`,
    to: `to-${id}`,
    carrier: 'twilio',
    cost: {},
    ...overrides,
  };
}

function makeBucket(calls: Call[]): MetricBucket {
  return {
    height: 100,
    calls,
    fromMs: 0,
    toMs: 60_000,
  };
}

describe('avgP95 — cross-call headline gating', () => {
  it('returns 0 when no calls have latencyP95', () => {
    expect(avgP95([])).toBe(0);
    expect(avgP95([makeCall('a')])).toBe(0);
  });

  it('returns 0 when no call has >=10 turns (avoids single-outlier headline)', () => {
    // Three calls, all with latencyP95 but only short turn counts — pre-fix
    // this would average all three; post-fix none qualify and we return 0
    // so the UI can fall back to "—".
    const calls = [
      makeCall('a', { latencyP95: 1977, turnCount: 5 }),
      makeCall('b', { latencyP95: 1500, turnCount: 7 }),
      makeCall('c', { latencyP95: 1200, turnCount: 3 }),
    ];
    expect(avgP95(calls)).toBe(0);
  });

  it('returns 0 when fewer than 3 calls qualify (sample too thin)', () => {
    // Two qualifying calls — below the 3-call minimum we still return 0
    // because a 2-call average is too noisy for a headline number.
    const calls = [
      makeCall('a', { latencyP95: 400, turnCount: 12 }),
      makeCall('b', { latencyP95: 500, turnCount: 15 }),
      makeCall('c', { latencyP95: 1900, turnCount: 4 }), // disqualified
    ];
    expect(avgP95(calls)).toBe(0);
  });

  it('averages only calls with >=10 turns when 3+ qualify', () => {
    const calls = [
      makeCall('a', { latencyP95: 400, turnCount: 10 }),
      makeCall('b', { latencyP95: 500, turnCount: 12 }),
      makeCall('c', { latencyP95: 600, turnCount: 15 }),
      makeCall('d', { latencyP95: 1977, turnCount: 5 }), // disqualified outlier
    ];
    // avg(400,500,600) = 500. The outlier is excluded.
    expect(avgP95(calls)).toBe(500);
  });

  it('rounds the average to an integer', () => {
    const calls = [
      makeCall('a', { latencyP95: 401, turnCount: 10 }),
      makeCall('b', { latencyP95: 501, turnCount: 12 }),
      makeCall('c', { latencyP95: 601, turnCount: 15 }),
    ];
    expect(avgP95(calls)).toBe(501); // 1503 / 3 = 501.0
  });
});

describe('bucketHeadline — latency sparkline tooltip', () => {
  it('shows "n/a (n<10 turns)" when no call in bucket has enough turns', () => {
    const bucket = makeBucket([
      makeCall('a', { latencyP95: 1977, turnCount: 5 }),
      makeCall('b', { latencyP95: 1500, turnCount: 7 }),
    ]);
    expect(bucketHeadline(bucket, 'latency')).toEqual({
      label: 'AVG LATENCY',
      value: 'n/a (n<10 turns)',
    });
  });

  it('averages only qualifying calls when at least one has >=10 turns', () => {
    const bucket = makeBucket([
      makeCall('a', { latencyP95: 400, turnCount: 10 }),
      makeCall('b', { latencyP95: 1977, turnCount: 4 }), // excluded
    ]);
    expect(bucketHeadline(bucket, 'latency')).toEqual({
      label: 'AVG LATENCY',
      value: '400 ms',
    });
  });

  it('still reports CALLS count headline for kind=count regardless of turns', () => {
    const bucket = makeBucket([
      makeCall('a', { latencyP95: 1977, turnCount: 3 }),
      makeCall('b', { turnCount: 2 }),
    ]);
    expect(bucketHeadline(bucket, 'count')).toEqual({
      label: 'CALLS',
      value: '2',
    });
  });
});

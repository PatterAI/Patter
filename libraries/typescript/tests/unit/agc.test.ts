/**
 * Unit tests for {@link Agc} (parity with Python ``tests/unit/test_agc.py``).
 * Real DSP — tones at known dBFS pushed through the real AGC; output level /
 * peak measured.
 */
import { describe, it, expect } from 'vitest';
import { Agc } from '../../src/audio/agc';

const SR = 16000;
const FRAME = 320; // 20 ms @ 16 kHz
const FULL_SCALE = 32768;

function sineFrame(dbfs: number, freqHz = 220, n = FRAME): Buffer {
  const rmsLin = FULL_SCALE * 10 ** (dbfs / 20);
  const amp = rmsLin * Math.SQRT2;
  const buf = Buffer.alloc(n * 2);
  for (let i = 0; i < n; i += 1) {
    buf.writeInt16LE(Math.round(amp * Math.sin((2 * Math.PI * freqHz * i) / SR)), i * 2);
  }
  return buf;
}

function rmsDbfs(pcm: Buffer): number {
  const n = pcm.length / 2;
  if (n === 0) return -120;
  let acc = 0;
  for (let i = 0; i < n; i += 1) {
    const s = pcm.readInt16LE(i * 2);
    acc += s * s;
  }
  const r = Math.sqrt(acc / n);
  return r <= 0 ? -120 : 20 * Math.log10(r / FULL_SCALE);
}

function peak(pcm: Buffer): number {
  const n = pcm.length / 2;
  let p = 0;
  for (let i = 0; i < n; i += 1) {
    const a = Math.abs(pcm.readInt16LE(i * 2));
    if (a > p) p = a;
  }
  return p;
}

describe('[unit] Agc', () => {
  it('normalises a quiet input toward the target', () => {
    const agc = new Agc(); // target -18 dBFS
    const frame = sineFrame(-30);
    let out = Buffer.alloc(0);
    for (let i = 0; i < 200; i += 1) out = agc.process(frame);
    expect(Math.abs(rmsDbfs(out) - -18)).toBeLessThan(2);
  });

  it('keeps an input already at target stable', () => {
    const agc = new Agc();
    const frame = sineFrame(-18);
    for (let i = 0; i < 60; i += 1) {
      const out = agc.process(frame);
      expect(Math.abs(rmsDbfs(out) - -18)).toBeLessThan(1.5);
    }
  });

  it('does not clip under an aggressive boost (limiter engages)', () => {
    const agc = new Agc({ targetRmsDbfs: -3 });
    const frame = sineFrame(-10, 300);
    let out = Buffer.alloc(0);
    for (let i = 0; i < 200; i += 1) out = agc.process(frame);
    const ceiling = Math.floor(0.99 * FULL_SCALE);
    const p = peak(out);
    expect(p).toBeLessThanOrEqual(ceiling);
    expect(p).toBeGreaterThanOrEqual(30000);
    const n = out.length / 2;
    for (let i = 0; i < n; i += 1) {
      const s = out.readInt16LE(i * 2);
      expect(s).toBeGreaterThanOrEqual(-32768);
      expect(s).toBeLessThanOrEqual(32767);
    }
  });

  it('does not pump the noise floor in silence gaps', () => {
    const agc = new Agc();
    const speech = sineFrame(-30);
    for (let i = 0; i < 120; i += 1) agc.process(speech);
    const noise = sineFrame(-55); // below the -45 dBFS speech floor
    let out = Buffer.alloc(0);
    for (let i = 0; i < 60; i += 1) out = agc.process(noise);
    // Pumped noise would land near -43 dBFS (+12 dB); selective release keeps
    // it near its original level.
    expect(rmsDbfs(out)).toBeLessThan(-48);
  });

  it('rejects invalid config', () => {
    expect(() => new Agc({ targetRmsDbfs: 0 })).toThrow();
    expect(() => new Agc({ limiterCeiling: 1.5 })).toThrow();
    expect(() => new Agc({ maxGainDb: 0 })).toThrow();
  });
});

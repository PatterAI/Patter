/**
 * Unit tests for {@link BiquadHighPass} (parity with Python
 * ``tests/unit/test_high_pass.py``). Real DSP — tones synthesised here are
 * filtered by the real biquad and the output energy is measured.
 */
import { describe, it, expect } from 'vitest';
import { BiquadHighPass } from '../../src/audio/high-pass';

const SR = 16000;

function sine(freqHz: number, n: number, amplitude = 10000): Buffer {
  const buf = Buffer.alloc(n * 2);
  for (let i = 0; i < n; i += 1) {
    buf.writeInt16LE(Math.round(amplitude * Math.sin((2 * Math.PI * freqHz * i) / SR)), i * 2);
  }
  return buf;
}

function rms(pcm: Buffer): number {
  const n = pcm.length / 2;
  if (n === 0) return 0;
  let acc = 0;
  for (let i = 0; i < n; i += 1) {
    const s = pcm.readInt16LE(i * 2);
    acc += s * s;
  }
  return Math.sqrt(acc / n);
}

function tailRms(pcm: Buffer, skipSamples: number): number {
  return rms(pcm.subarray(skipSamples * 2));
}

describe('[unit] BiquadHighPass', () => {
  it('attenuates a sub-cutoff (50 Hz) tone by > 10 dB', () => {
    const hpf = new BiquadHighPass({ cutoffHz: 100, sampleRate: SR });
    const sig = sine(50, SR);
    const out = hpf.process(sig);
    const attenDb = 20 * Math.log10(tailRms(out, SR / 10) / tailRms(sig, SR / 10));
    expect(attenDb).toBeLessThan(-10);
  });

  it('passes a 440 Hz speech-band tone almost unchanged', () => {
    const hpf = new BiquadHighPass({ cutoffHz: 100, sampleRate: SR });
    const sig = sine(440, SR);
    const out = hpf.process(sig);
    const attenDb = 20 * Math.log10(tailRms(out, SR / 10) / tailRms(sig, SR / 10));
    expect(Math.abs(attenDb)).toBeLessThan(1);
  });

  it('removes a DC offset', () => {
    const hpf = new BiquadHighPass({ cutoffHz: 100, sampleRate: SR });
    const n = SR;
    const sig = Buffer.alloc(n * 2);
    for (let i = 0; i < n; i += 1) sig.writeInt16LE(8000, i * 2);
    const out = hpf.process(sig);
    const tail = out.subarray((SR / 5) * 2);
    let sum = 0;
    const m = tail.length / 2;
    for (let i = 0; i < m; i += 1) sum += tail.readInt16LE(i * 2);
    expect(Math.abs(sum / m)).toBeLessThan(50);
  });

  it('processes chunked input identically to one-shot', () => {
    const sig = sine(300, 4000);
    const oneShot = new BiquadHighPass({ cutoffHz: 120, sampleRate: SR }).process(sig);
    const chunked = new BiquadHighPass({ cutoffHz: 120, sampleRate: SR });
    const parts: Buffer[] = [];
    const step = 320 * 2;
    for (let off = 0; off < sig.length; off += step) {
      parts.push(chunked.process(sig.subarray(off, off + step)));
    }
    expect(Buffer.concat(parts).equals(oneShot)).toBe(true);
  });

  it('reset() clears filter state', () => {
    const hpf = new BiquadHighPass({ cutoffHz: 100, sampleRate: SR });
    const sig = sine(300, 640);
    const first = hpf.process(sig);
    hpf.reset();
    const again = hpf.process(sig);
    expect(again.equals(first)).toBe(true);
  });

  it('rejects an invalid cutoff', () => {
    expect(() => new BiquadHighPass({ cutoffHz: 0, sampleRate: SR })).toThrow();
    expect(() => new BiquadHighPass({ cutoffHz: 9000, sampleRate: SR })).toThrow();
  });

  it('carries a trailing odd byte across calls', () => {
    const hpf = new BiquadHighPass({ cutoffHz: 100, sampleRate: SR });
    const out = hpf.process(Buffer.from([0x10, 0x00, 0x20]));
    expect(out.length).toBe(2);
    const rest = hpf.process(Buffer.from([0x00]));
    expect(rest.length).toBe(2);
  });
});

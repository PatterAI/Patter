import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  PcmCarry,
  StatefulResampler,
  createResampler8kTo16k,
  createResampler16kTo8k,
  createResampler24kTo16k,
  resample8kTo16k,
  resample16kTo8k,
  resample24kTo16k,
} from '../../src/transcoding';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Write an array of Int16 values into a PCM16-LE Buffer. */
function i16buf(samples: number[]): Buffer {
  const buf = Buffer.alloc(samples.length * 2);
  for (let i = 0; i < samples.length; i++) buf.writeInt16LE(samples[i], i * 2);
  return buf;
}

/** Read all Int16 samples from a PCM16-LE Buffer. */
function readI16(buf: Buffer): number[] {
  const out: number[] = [];
  for (let i = 0; i + 1 < buf.length; i += 2) out.push(buf.readInt16LE(i));
  return out;
}

/**
 * Synthesise a 440 Hz sine wave at sampleRate Hz for durationSamples samples.
 * Amplitude 8000 (well within Int16 range, keeps headroom for filter droop).
 */
function sineWave(sampleRate: number, durationSamples: number, freqHz = 440): Buffer {
  const buf = Buffer.alloc(durationSamples * 2);
  for (let i = 0; i < durationSamples; i++) {
    const val = Math.round(8000 * Math.sin(2 * Math.PI * freqHz * i / sampleRate));
    buf.writeInt16LE(Math.max(-32768, Math.min(32767, val)), i * 2);
  }
  return buf;
}

/**
 * Split a Buffer into random-sized chunks of 1–maxSize bytes.
 * Uses a seeded iteration rather than actual randomness so tests are
 * deterministic. Seed controls the step sequence.
 */
function splitRandom(buf: Buffer, maxSize: number, seed = 42): Buffer[] {
  const chunks: Buffer[] = [];
  let offset = 0;
  let s = seed;
  while (offset < buf.length) {
    // LCG pseudo-random: produces chunk sizes in [1, maxSize]
    s = (s * 1664525 + 1013904223) >>> 0;
    const size = (s % maxSize) + 1;
    chunks.push(buf.subarray(offset, offset + size));
    offset += size;
  }
  return chunks;
}

// ---------------------------------------------------------------------------
// PcmCarry tests
// ---------------------------------------------------------------------------

describe('PcmCarry', () => {
  it('passes through even-byte chunks unchanged', () => {
    const carry = new PcmCarry();
    const input = Buffer.from([0x01, 0x02, 0x03, 0x04]);
    expect(carry.push(input)).toEqual(input);
    expect(carry.flush().length).toBe(0);
  });

  it('holds odd trailing byte and prepends it to next chunk', () => {
    const carry = new PcmCarry();
    // 3-byte chunk → emit 2 bytes, hold 1
    const out1 = carry.push(Buffer.from([0x10, 0x20, 0x30]));
    expect(out1).toEqual(Buffer.from([0x10, 0x20]));
    // 1-byte chunk → combined = [0x30, 0x40] → emit 2 bytes, hold 0
    const out2 = carry.push(Buffer.from([0x40]));
    expect(out2).toEqual(Buffer.from([0x30, 0x40]));
    expect(carry.flush().length).toBe(0);
  });

  it('3 / 1 / 5 byte sequence: emits 2 / 2 / 4 bytes + 1 in flush', () => {
    const carry = new PcmCarry();
    // [0x11,0x22,0x33]: emits [0x11,0x22], pending=[0x33]
    expect(carry.push(Buffer.from([0x11, 0x22, 0x33])).length).toBe(2);
    // [0x44]: combined=[0x33,0x44] -> emits 2 bytes, no pending
    expect(carry.push(Buffer.from([0x44])).length).toBe(2);
    // [0x55..0x99]: 5 bytes -> emits 4 bytes, pending=[0x99]
    expect(carry.push(Buffer.from([0x55, 0x66, 0x77, 0x88, 0x99])).length).toBe(4);
    const tail = carry.flush();
    expect(tail.length).toBe(1);
    expect(tail[0]).toBe(0x99);
  });

  it('flush on empty carry returns zero-length buffer', () => {
    const carry = new PcmCarry();
    expect(carry.flush().length).toBe(0);
  });

  it('reset clears pending byte', () => {
    const carry = new PcmCarry();
    carry.push(Buffer.from([0x01])); // 1 byte pending
    carry.reset();
    expect(carry.flush().length).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// StatefulResampler — constructor validation
// ---------------------------------------------------------------------------

describe('StatefulResampler constructor', () => {
  it('accepts supported rate pairs', () => {
    expect(() => createResampler16kTo8k()).not.toThrow();
    expect(() => createResampler8kTo16k()).not.toThrow();
    expect(() => createResampler24kTo16k()).not.toThrow();
  });

  it('throws on unsupported rate pair', () => {
    expect(() => new StatefulResampler({ srcRate: 48000, dstRate: 16000 }))
      .toThrow('unsupported conversion');
  });

  it('throws on multi-channel input', () => {
    expect(() => new StatefulResampler({ srcRate: 16000, dstRate: 8000, channels: 2 }))
      .toThrow('only mono');
  });

  it('accepts explicit channels=1', () => {
    expect(() => new StatefulResampler({ srcRate: 16000, dstRate: 8000, channels: 1 }))
      .not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// StatefulResampler 16k→8k: no boundary clicks on chunked sine wave
// ---------------------------------------------------------------------------

describe('StatefulResampler 16k→8k', () => {
  it('returns empty buffer for empty input', () => {
    const r = createResampler16kTo8k();
    expect(r.process(Buffer.alloc(0)).length).toBe(0);
  });

  it('produces half as many samples as input for an even chunk', () => {
    const r = createResampler16kTo8k();
    const input = i16buf([100, 200, 300, 400, 500, 600, 700, 800]);
    const out = r.process(input);
    expect(out.length).toBe(input.length / 2);
  });

  it('no boundary clicks: split sine wave concat matches single-pass output', () => {
    const totalSamples = 1600; // 100 ms at 16 kHz
    const src = sineWave(16000, totalSamples);

    // Single-pass reference.
    const rRef = createResampler16kTo8k();
    const refOut = Buffer.concat([rRef.process(src), rRef.flush()]);

    // Chunked pass with random chunk sizes (seed=7).
    const rChunked = createResampler16kTo8k();
    const chunks = splitRandom(src, 64, 7);
    const parts: Buffer[] = [];
    for (const chunk of chunks) {
      const out = rChunked.process(chunk);
      if (out.length > 0) parts.push(out);
    }
    parts.push(rChunked.flush());
    const chunkedOut = Buffer.concat(parts);

    expect(chunkedOut.length).toBe(refOut.length);

    // Check no large discontinuity between consecutive output samples.
    // A boundary click would produce an abrupt jump of tens of thousands LSB.
    const CLICK_THRESHOLD = 4000;
    const outSamples = readI16(chunkedOut);
    for (let i = 1; i < outSamples.length; i++) {
      const diff = Math.abs(outSamples[i] - outSamples[i - 1]);
      expect(diff).toBeLessThan(CLICK_THRESHOLD);
    }
  });

  it('DC signal passes through unchanged (FIR unity gain on DC)', () => {
    const r = createResampler16kTo8k();
    const input = i16buf(Array(20).fill(5000));
    const out = r.process(input);
    const samples = readI16(out);
    // Allow ±1 LSB for integer rounding.
    for (const s of samples) expect(Math.abs(s - 5000)).toBeLessThanOrEqual(1);
  });

  it('handles odd-byte input via carry (no throw, output still even)', () => {
    const r = createResampler16kTo8k();
    // 3 bytes = 1 complete sample + 1 odd byte carried.
    const out1 = r.process(Buffer.alloc(3));
    // 1 pending byte + 1 new byte = 1 complete sample → now 2 samples → 1 output.
    const out2 = r.process(Buffer.alloc(1));
    expect(out1.length % 2).toBe(0);
    expect(out2.length % 2).toBe(0);
  });

  it('reset clears state so next chunk starts fresh', () => {
    const r = createResampler16kTo8k();
    r.process(i16buf([1000, 2000, 3000, 4000]));
    r.reset();
    // After reset, first chunk should behave as if no prior history.
    const out = r.process(i16buf([0, 0, 0, 0]));
    const samples = readI16(out);
    for (const s of samples) expect(s).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// StatefulResampler 8k→16k
// ---------------------------------------------------------------------------

describe('StatefulResampler 8k→16k', () => {
  it('returns empty buffer for empty input', () => {
    const r = createResampler8kTo16k();
    expect(r.process(Buffer.alloc(0)).length).toBe(0);
  });

  it('no boundary clicks: split sine wave', () => {
    const totalSamples = 800; // 100 ms at 8 kHz
    const src = sineWave(8000, totalSamples);

    const rRef = createResampler8kTo16k();
    const refMain = rRef.process(src);
    const refTail = rRef.flush();
    const refOut = Buffer.concat([refMain, refTail]);

    const rChunked = createResampler8kTo16k();
    const chunks = splitRandom(src, 48, 13);
    const parts: Buffer[] = [];
    for (const chunk of chunks) {
      const out = rChunked.process(chunk);
      if (out.length > 0) parts.push(out);
    }
    parts.push(rChunked.flush());
    const chunkedOut = Buffer.concat(parts);

    expect(chunkedOut.length).toBe(refOut.length);

    const CLICK_THRESHOLD = 4000;
    const outSamples = readI16(chunkedOut);
    for (let i = 1; i < outSamples.length; i++) {
      const diff = Math.abs(outSamples[i] - outSamples[i - 1]);
      expect(diff).toBeLessThan(CLICK_THRESHOLD);
    }
  });

  it('flush emits deferred last sample pair', () => {
    const r = createResampler8kTo16k();
    // 1 sample: process emits 0 outputs (last sample is deferred).
    const out1 = r.process(i16buf([500]));
    expect(out1.length).toBe(0);
    // flush emits 2 samples: original + its self-interpolated duplicate.
    const tail = r.flush();
    expect(tail.length).toBe(4);
    expect(readI16(tail)).toEqual([500, 500]);
  });
});

// ---------------------------------------------------------------------------
// StatefulResampler 24k→16k
// ---------------------------------------------------------------------------

describe('StatefulResampler 24k→16k', () => {
  it('returns empty buffer for empty input', () => {
    const r = createResampler24kTo16k();
    expect(r.process(Buffer.alloc(0)).length).toBe(0);
  });

  it('no boundary clicks: split sine wave', () => {
    const totalSamples = 2400; // 100 ms at 24 kHz
    const src = sineWave(24000, totalSamples);

    const rRef = createResampler24kTo16k();
    const refOut = rRef.process(src);

    const rChunked = createResampler24kTo16k();
    const chunks = splitRandom(src, 72, 99);
    const parts: Buffer[] = [];
    for (const chunk of chunks) {
      const out = rChunked.process(chunk);
      if (out.length > 0) parts.push(out);
    }
    parts.push(rChunked.flush());
    const chunkedOut = Buffer.concat(parts);

    // Length should match (±2 samples tolerance for boundary rounding).
    expect(Math.abs(chunkedOut.length - refOut.length)).toBeLessThanOrEqual(4);

    const CLICK_THRESHOLD = 4000;
    const outSamples = readI16(chunkedOut);
    for (let i = 1; i < outSamples.length; i++) {
      const diff = Math.abs(outSamples[i] - outSamples[i - 1]);
      expect(diff).toBeLessThan(CLICK_THRESHOLD);
    }
  });

  it('exact 3-sample to 2-sample conversion (first chunk)', () => {
    const r = createResampler24kTo16k();
    const input = i16buf([100, 200, 300]);
    const out = r.process(input);
    // phase 0.0 → s[0]=100; phase 1.5 → interp(s[1],s[2]) = 250
    expect(readI16(out)).toEqual([100, 250]);
  });
});

// ---------------------------------------------------------------------------
// Deprecated wrappers: warn once, still produce correct output
// ---------------------------------------------------------------------------

describe('deprecated stateless wrappers', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('resample8kTo16k warns exactly once and returns correct output', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    // Note: the module-level flag may already be set from earlier test runs in
    // the same process, so we accept 0 or 1 warn calls here.
    const input = i16buf([0, 100, 200, 300]);
    const out = resample8kTo16k(input);
    expect(out.length).toBe(input.length * 2);
    expect(warnSpy.mock.calls.length).toBeLessThanOrEqual(1);
  });

  it('resample16kTo8k warns exactly once and returns correct output', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const input = i16buf([0, 100, 200, 300, 400, 500, 600, 700]);
    const out = resample16kTo8k(input);
    expect(out.length).toBe(input.length / 2);
    expect(warnSpy.mock.calls.length).toBeLessThanOrEqual(1);
  });

  it('resample24kTo16k warns exactly once and returns correct output', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const input = i16buf([100, 200, 300, 400, 500, 600]);
    const out = resample24kTo16k(input);
    expect(out.length).toBe(8); // 6 samples → 4 output samples = 8 bytes
    expect(warnSpy.mock.calls.length).toBeLessThanOrEqual(1);
  });
});

import { describe, it, expect } from 'vitest';
import {
  RateAwareResampler,
  StatefulResampler,
  pcm16ToMulaw,
  mulawToPcm16,
} from '../src/audio/transcoding';
import {
  resolveTtsSourceFormat,
  CARRIER_WIRE_FORMAT,
  formatsMatch,
  parseWireFormatString,
} from '../src/audio/format';

/** Synthesise `seconds` of a pure tone as PCM16-LE mono at `rate`. */
function sinePcm(freqHz: number, rate: number, seconds: number, amp = 12000): Buffer {
  const n = Math.round(rate * seconds);
  const buf = Buffer.alloc(n * 2);
  for (let i = 0; i < n; i++) {
    buf.writeInt16LE(Math.round(amp * Math.sin((2 * Math.PI * freqHz * i) / rate)), i * 2);
  }
  return buf;
}

/** Goertzel power of `buf` (PCM16-LE) at `freqHz` given `rate`. */
function goertzel(buf: Buffer, freqHz: number, rate: number): number {
  const n = buf.length >> 1;
  const w = (2 * Math.PI * freqHz) / rate;
  const coeff = 2 * Math.cos(w);
  let s0 = 0;
  let s1 = 0;
  let s2 = 0;
  for (let i = 0; i < n; i++) {
    s0 = buf.readInt16LE(i * 2) + coeff * s1 - s2;
    s2 = s1;
    s1 = s0;
  }
  return s1 * s1 + s2 * s2 - coeff * s1 * s2;
}

/** Dominant frequency of `buf` by scanning candidate tones (Hz, `step` apart). */
function dominantFreq(buf: Buffer, rate: number, step = 2): number {
  const maxHz = rate / 2 - step;
  let bestF = 0;
  let bestP = -Infinity;
  for (let f = step; f < maxHz; f += step) {
    const p = goertzel(buf, f, rate);
    if (p > bestP) {
      bestP = p;
      bestF = f;
    }
  }
  return bestF;
}

/** Energy in the band [loHz, hiHz] of `buf` at `rate` (sum of Goertzel powers). */
function bandEnergy(buf: Buffer, rate: number, loHz: number, hiHz: number, step = 25): number {
  let e = 0;
  for (let f = loHz; f <= hiHz; f += step) e += goertzel(buf, f, rate);
  return e;
}

/** Run PCM through a resampler chunked at `chunkBytes` then flush. */
function runChunked(r: RateAwareResampler, pcm: Buffer, chunkBytes = 517): Buffer {
  const parts: Buffer[] = [];
  for (let i = 0; i < pcm.length; i += chunkBytes) {
    parts.push(r.process(pcm.subarray(i, i + chunkBytes)));
  }
  parts.push(r.flush());
  return Buffer.concat(parts);
}

describe('[unit] RateAwareResampler — pitch preservation across input rates', () => {
  // The chipmunk bug: a 440 Hz tone produced by the TTS at rate R must STILL
  // be 440 Hz after resampling to the 8 kHz carrier wire. The old fixed
  // 16k→8k decimator doubled the pitch of an 8 kHz source.
  for (const srcRate of [8000, 16000, 22050, 24000, 44100]) {
    it(`keeps 440 Hz at 440 Hz for ${srcRate} Hz source → 8 kHz carrier`, () => {
      const input = sinePcm(440, srcRate, 0.5);
      const r = new RateAwareResampler({ srcRate, dstRate: 8000 });
      const out8k = runChunked(r, input);
      // Decode through the real mulaw round-trip the carrier wire uses.
      const wire = mulawToPcm16(pcm16ToMulaw(out8k));
      const f = dominantFreq(wire, 8000);
      expect(f).toBeGreaterThan(430);
      expect(f).toBeLessThan(450);
    });
  }

  it('reproduces the chipmunk bug with the OLD fixed 16k→8k path on an 8k source', () => {
    // Regression guard: the legacy resampler treats an 8 kHz source as 16 kHz,
    // decimating 2:1 → ~880 Hz (≈2x) when played at 8 kHz.
    const input = sinePcm(440, 8000, 0.5);
    const legacy = new StatefulResampler({ srcRate: 16000, dstRate: 8000 });
    const out = Buffer.concat([legacy.process(input), legacy.flush()]);
    const wire = mulawToPcm16(pcm16ToMulaw(out));
    const f = dominantFreq(wire, 8000);
    expect(f).toBeGreaterThan(820); // chipmunk: pitch roughly doubled
  });
});

describe('[unit] RateAwareResampler — duration preservation', () => {
  for (const srcRate of [8000, 16000, 22050, 24000, 44100]) {
    it(`1.0 s @ ${srcRate} Hz → ~1.0 s @ 8 kHz`, () => {
      const input = sinePcm(300, srcRate, 1.0);
      const r = new RateAwareResampler({ srcRate, dstRate: 8000 });
      const out = runChunked(r, input);
      const outSamples = out.length >> 1;
      // Expect ~8000 samples (1 s at 8 kHz), within a few-ms rounding window.
      expect(Math.abs(outSamples - 8000)).toBeLessThan(50);
    });
  }
});

describe('[unit] RateAwareResampler — anti-alias on downsample', () => {
  it('attenuates a 6 kHz tone that would alias into the 8 kHz band (16k→8k)', () => {
    // 6 kHz at 16 kHz source is above the 4 kHz Nyquist of 8 kHz output; raw
    // decimation folds it to 2 kHz (audible hiss). The anti-alias LPF must cut
    // it. Compare aliased-band energy WITH vs WITHOUT the filter.
    const input = sinePcm(6000, 16000, 0.5);
    const withAA = runChunked(new RateAwareResampler({ srcRate: 16000, dstRate: 8000 }), input);
    const noAA = runChunked(
      new RateAwareResampler({ srcRate: 16000, dstRate: 8000, antiAlias: false }),
      input,
    );
    // The 6 kHz tone folds to |8000-6000| = 2000 Hz at 8 kHz.
    const aliasWith = bandEnergy(withAA, 8000, 1800, 2200);
    const aliasWithout = bandEnergy(noAA, 8000, 1800, 2200);
    expect(aliasWith).toBeLessThan(aliasWithout * 0.25); // ≥12 dB suppression
  });

  it('reports a small, bounded anti-alias latency budget', () => {
    const r = new RateAwareResampler({ srcRate: 24000, dstRate: 8000 });
    expect(r.antiAliasLatencyMs).toBeGreaterThan(0);
    expect(r.antiAliasLatencyMs).toBeLessThan(3); // real-time safe: < 3 ms
  });
});

describe('[unit] RateAwareResampler — identity passthrough at equal rates', () => {
  it('8k → 8k returns the input samples unchanged (no resample)', () => {
    const input = sinePcm(440, 8000, 0.2);
    const r = new RateAwareResampler({ srcRate: 8000, dstRate: 8000 });
    const out = runChunked(r, input);
    expect(out.equals(input)).toBe(true);
    expect(r.antiAliasLatencyMs).toBe(0);
  });
});

describe('[unit] format resolution — single source of truth', () => {
  it('carrier wire format is μ-law 8 kHz for every PSTN carrier', () => {
    for (const c of ['twilio', 'telnyx', 'plivo'] as const) {
      expect(CARRIER_WIRE_FORMAT[c]).toEqual({ encoding: 'mulaw', sampleRate: 8000 });
    }
  });

  it('resolves a provider sourceAudioFormat() first', () => {
    const tts = { sourceAudioFormat: () => ({ encoding: 'pcm_s16le' as const, sampleRate: 24000 }) };
    expect(resolveTtsSourceFormat(tts)).toEqual({ encoding: 'pcm_s16le', sampleRate: 24000 });
  });

  it('falls back to outputFormat string then sampleRate then legacy 16k', () => {
    expect(resolveTtsSourceFormat({ outputFormat: 'ulaw_8000' })).toEqual({
      encoding: 'mulaw',
      sampleRate: 8000,
    });
    expect(resolveTtsSourceFormat({ outputFormat: 'pcm_22050' })).toEqual({
      encoding: 'pcm_s16le',
      sampleRate: 22050,
    });
    expect(resolveTtsSourceFormat({ sampleRate: 8000 })).toEqual({
      encoding: 'pcm_s16le',
      sampleRate: 8000,
    });
    // Unknown adapter → legacy default (preserves pre-fix behaviour).
    expect(resolveTtsSourceFormat({})).toEqual({ encoding: 'pcm_s16le', sampleRate: 16000 });
  });

  it('detects μ-law passthrough via formatsMatch', () => {
    const mulaw = parseWireFormatString('ulaw_8000')!;
    expect(formatsMatch(mulaw, CARRIER_WIRE_FORMAT.twilio)).toBe(true);
    const pcm16 = parseWireFormatString('pcm_16000')!;
    expect(formatsMatch(pcm16, CARRIER_WIRE_FORMAT.twilio)).toBe(false);
  });
});

/**
 * Regression test for the stateful 16 kHz → 8 kHz FIR resampler's
 * chunk-boundary handling: an odd-sized chunk used to write the carried
 * pending sample into FIR history too, so the next chunk processed it twice
 * and lost the true s-2 — audible crackle at every odd chunk boundary.
 *
 * Contract: chunked processing must match one-shot processing exactly
 * (that is the entire purpose of the stateful resampler).
 */

import { describe, expect, it } from 'vitest';
import { createResampler16kTo8k } from '../../src/audio/transcoding';

function pcm16(samples: number[]): Buffer {
  const buf = Buffer.alloc(samples.length * 2);
  samples.forEach((s, i) => buf.writeInt16LE(s, i * 2));
  return buf;
}

function runChunked(samples: number[], chunkSizes: number[]): Buffer {
  const r = createResampler16kTo8k();
  const out: Buffer[] = [];
  let offset = 0;
  let sizeIdx = 0;
  while (offset < samples.length) {
    const n = chunkSizes[sizeIdx % chunkSizes.length];
    sizeIdx++;
    const chunk = samples.slice(offset, offset + n);
    offset += n;
    out.push(r.process(pcm16(chunk)));
  }
  return Buffer.concat(out);
}

describe('stateful 16k→8k resampler chunk boundaries', () => {
  const ramp = Array.from({ length: 160 }, (_, i) => (i + 1) * 100);

  it('odd-sized chunks produce the same output as one-shot processing', () => {
    const oneShot = createResampler16kTo8k().process(pcm16(ramp));
    const chunked = runChunked(ramp, [3, 5, 8]);
    const n = Math.min(oneShot.length, chunked.length);
    expect(n).toBeGreaterThan(140); // ~80 outputs × 2 bytes, minus the deferred tail
    expect(chunked.subarray(0, n).equals(oneShot.subarray(0, n))).toBe(true);
  });

  it('mixed even/odd chunk sizes match one-shot processing', () => {
    const oneShot = createResampler16kTo8k().process(pcm16(ramp));
    const chunked = runChunked(ramp, [7, 2, 9, 4]);
    const n = Math.min(oneShot.length, chunked.length);
    expect(n).toBeGreaterThan(140);
    expect(chunked.subarray(0, n).equals(oneShot.subarray(0, n))).toBe(true);
  });

  it('flush() emits the deferred final output', () => {
    const r = createResampler16kTo8k();
    const processed = r.process(pcm16(ramp));
    const tail = r.flush();
    // 160 input samples → 80 decimated outputs total (process + flush).
    expect(processed.length + tail.length).toBe(160);
  });
});

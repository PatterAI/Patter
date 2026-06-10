/**
 * Audio transcoding utilities for Patter TypeScript SDK.
 *
 * Pure TypeScript implementation — no native dependencies required.
 * Handles mulaw (G.711) encoding/decoding and PCM16 resampling for
 * telephony audio pipelines (Twilio mulaw 8kHz, Telnyx 16kHz PCM,
 * OpenAI TTS 24kHz PCM).
 */

import { getLogger } from '../logger';

// ---------- ITU-T G.711 mu-law tables ----------

/**
 * Lookup table: mu-law encoded byte -> signed 16-bit PCM value.
 * Generated from the standard ITU-T G.711 algorithm.
 */
const MULAW_TO_PCM16_TABLE: Int16Array = (() => {
  const table = new Int16Array(256);
  for (let i = 0; i < 256; i++) {
    const mu = ~i & 0xff;
    const sign = mu & 0x80 ? -1 : 1;
    const exponent = (mu >> 4) & 0x07;
    const mantissa = mu & 0x0f;
    const magnitude = ((mantissa << 1) | 0x21) << (exponent + 2);
    table[i] = sign * (magnitude - 0x84);
  }
  return table;
})();

/**
 * Lookup table: signed 16-bit PCM value (shifted to 0..65535) -> mu-law byte.
 * Built using the standard compression algorithm for fast encoding.
 */
const PCM16_TO_MULAW_TABLE: Uint8Array = (() => {
  const BIAS = 0x84;
  const CLIP = 32635;
  const table = new Uint8Array(65536);

  for (let i = 0; i < 65536; i++) {
    // Convert unsigned index to signed 16-bit
    let sample = i >= 32768 ? i - 65536 : i;

    const sign = sample < 0 ? 0x80 : 0;
    if (sample < 0) sample = -sample;
    if (sample > CLIP) sample = CLIP;
    sample += BIAS;

    let exponent = 7;
    const exponentMask = 0x4000;
    for (let shift = exponentMask; shift > 0 && (sample & shift) === 0; shift >>= 1) {
      exponent--;
    }

    const mantissa = (sample >> (exponent + 3)) & 0x0f;
    const mulaw = ~(sign | (exponent << 4) | mantissa) & 0xff;
    table[i] = mulaw;
  }

  return table;
})();

/**
 * Decode mu-law 8-bit audio to signed 16-bit little-endian PCM.
 *
 * Each input byte produces one 16-bit sample (2 bytes), so the output
 * buffer is exactly twice the length of the input.
 */
export function mulawToPcm16(mulawData: Buffer): Buffer {
  const out = Buffer.alloc(mulawData.length * 2);
  for (let i = 0; i < mulawData.length; i++) {
    out.writeInt16LE(MULAW_TO_PCM16_TABLE[mulawData[i]], i * 2);
  }
  return out;
}

/**
 * Encode signed 16-bit little-endian PCM to mu-law 8-bit audio.
 *
 * Each pair of input bytes (one 16-bit sample) produces one output byte.
 * If the input length is odd, the trailing byte is ignored.
 */
export function pcm16ToMulaw(pcmData: Buffer): Buffer {
  const sampleCount = Math.floor(pcmData.length / 2);
  const out = Buffer.alloc(sampleCount);
  for (let i = 0; i < sampleCount; i++) {
    const sample = pcmData.readInt16LE(i * 2);
    // Shift signed value to unsigned index (0..65535)
    out[i] = PCM16_TO_MULAW_TABLE[(sample + 65536) & 0xffff];
  }
  return out;
}

// ---------- PcmCarry: odd-byte alignment buffer ----------

/**
 * Buffers a trailing odd byte across chunk boundaries so that downstream
 * consumers (resamplers, encoders) always receive even-length (2-byte-aligned)
 * PCM16 buffers.
 *
 * Mirror of the Python-side PcmCarry helper. Typical usage:
 *
 * ```ts
 * const carry = new PcmCarry();
 * for (const raw of stream) {
 *   const aligned = carry.push(raw);
 *   if (aligned.length > 0) process(aligned);
 * }
 * const tail = carry.flush();
 * if (tail.length > 0) process(tail);
 * ```
 */
export class PcmCarry {
  private pending: Buffer | null = null;

  /**
   * Prepend any carried odd byte, return the even-length prefix, and stash
   * any new trailing odd byte for the next call.
   *
   * Returns a zero-length buffer when no complete sample is yet available.
   */
  push(chunk: Buffer): Buffer {
    const combined = this.pending !== null
      ? Buffer.concat([this.pending, chunk])
      : chunk;
    this.pending = null;

    const alignedLen = combined.length & ~1; // round down to even
    if (alignedLen < combined.length) {
      this.pending = combined.subarray(alignedLen);
    }
    return combined.subarray(0, alignedLen);
  }

  /**
   * Return any pending byte as a 1-byte buffer (rare in practice — only if
   * the entire stream had an odd byte count), then reset internal state.
   */
  flush(): Buffer {
    if (this.pending === null) return Buffer.alloc(0);
    const out = this.pending;
    this.pending = null;
    return out;
  }

  /** Reset carry state without flushing. */
  reset(): void {
    this.pending = null;
  }
}

// ---------- StatefulResampler ----------

/** Options for constructing a {@link StatefulResampler}. */
export interface StatefulResamplerOptions {
  srcRate: number;
  dstRate: number;
  /** Number of channels (default 1 / mono). */
  channels?: number;
}

/**
 * Stateful PCM16 resampler that carries tail state across chunk boundaries,
 * eliminating the boundary discontinuities present in the legacy one-shot
 * helpers.
 *
 * Supported conversions:
 * - 16 000 → 8 000 Hz  (2:1 decimation with 5-tap FIR anti-alias)
 * - 8 000 → 16 000 Hz  (1:2 linear interpolation)
 * - 24 000 → 16 000 Hz (3:2 linear interpolation)
 * - 24 000 → 8 000 Hz  (3:1 decimation with linear interpolation;
 *   collapses 24k→16k→8k chain — fix #46)
 *
 * All methods accept and return Buffer (PCM16-LE, mono by default).
 */
export class StatefulResampler {
  private readonly srcRate: number;
  private readonly dstRate: number;

  // 16k→8k: 5-tap FIR state.
  // Extended sample buffer carries the 2 history samples that precede the
  // current chunk AND any "pending" input sample that did not yet generate
  // output (i.e. the odd sample when the chunk had an odd sample count).
  // `firPhase` = 0 means the next output is at input position 0 of the
  // current chunk; 1 means it starts at input position 1 (because the
  // previous chunk ended on an even-output boundary).
  private firHistory: Int16Array = new Int16Array(2); // [s_{-2}, s_{-1}]
  private firHistoryValid = false;
  /**
   * Samples after the last emitted FIR center, carried to the next chunk
   * (1–2 samples: the next center and/or its missing lookahead). Replaces
   * the old single ``firPendingSample`` — that design wrote the pending
   * sample into history too (processed twice, true s-2 lost) and edge-
   * replicated the +2 tap at every chunk end, producing audible crackle at
   * chunk boundaries.
   */
  private firCarry: Int16Array = new Int16Array(0);
  // Pending sample carried from odd-count chunks (not the byte carry —
  // this is a complete Int16 sample that becomes the first input for the
  // next call).

  // 8k→16k: last input sample deferred across chunk boundaries.
  private upsampleLast = 0;
  private upsampleHasHistory = false;

  // 24k→16k: fractional phase and last input sample across chunks.
  private resample24Last = 0;
  private resample24Phase = 0;
  private resample24HasHistory = false;

  // Odd-byte alignment carry.
  private readonly carry = new PcmCarry();

  constructor(opts: StatefulResamplerOptions) {
    this.srcRate = opts.srcRate;
    this.dstRate = opts.dstRate;
    if (opts.channels !== undefined && opts.channels !== 1) {
      throw new Error('StatefulResampler: only mono (channels=1) is supported');
    }
    const key = `${this.srcRate}->${this.dstRate}`;
    if (
      key !== '16000->8000' &&
      key !== '8000->16000' &&
      key !== '24000->16000' &&
      key !== '24000->8000'
    ) {
      throw new Error(
        `StatefulResampler: unsupported conversion ${key}. ` +
        'Supported: 16000->8000, 8000->16000, 24000->16000, 24000->8000',
      );
    }
  }

  /**
   * Process a chunk of PCM16-LE samples.
   *
   * Handles odd-byte inputs via an internal carry buffer. Returns an even-byte-
   * aligned output buffer; may return a zero-length buffer if not enough
   * aligned input is available yet.
   */
  process(pcm: Buffer): Buffer {
    const aligned = this.carry.push(pcm);
    if (aligned.length === 0) return Buffer.alloc(0);

    if (this.srcRate === 16000 && this.dstRate === 8000) {
      return this._downsample16kTo8k(aligned);
    }
    if (this.srcRate === 8000 && this.dstRate === 16000) {
      return this._upsample8kTo16k(aligned);
    }
    if (this.srcRate === 24000 && this.dstRate === 8000) {
      return this._resample24kTo8k(aligned);
    }
    return this._resample24kTo16k(aligned);
  }

  /**
   * Flush internal state and return any remaining output samples.
   *
   * For 8k→16k: the deferred last sample is emitted duplicated (matching
   * the stateless helper's end-of-stream behaviour).
   * For 16k→8k: any pending odd sample is processed with edge-replication.
   * Resets all state after flushing.
   */
  flush(): Buffer {
    const carryTail = this.carry.flush();
    if (carryTail.length > 0) {
      getLogger().warn(
        '[patter] StatefulResampler.flush: trailing odd byte discarded — upstream produced odd-length PCM stream',
      );
    }

    if (this.srcRate === 16000 && this.dstRate === 8000 && this.firCarry.length > 0) {
      // End of stream: emit the deferred final center, edge-replicating the
      // missing lookahead taps.
      const carry = this.firCarry;
      this.firCarry = new Int16Array(0);
      const s0 = carry[0];
      const sP1 = carry.length > 1 ? carry[1] : s0;
      const sP2 = sP1;
      const sM2 = this.firHistoryValid ? this.firHistory[0] : 0;
      const sM1 = this.firHistoryValid ? this.firHistory[1] : 0;
      const filtered = (sM2 + 4 * sM1 + 6 * s0 + 4 * sP1 + sP2 + 8) >> 4;
      const out = Buffer.alloc(2);
      out.writeInt16LE(Math.max(-32768, Math.min(32767, filtered)), 0);
      return out;
    }

    if (this.srcRate === 8000 && this.dstRate === 16000 && this.upsampleHasHistory) {
      const out = Buffer.alloc(4);
      out.writeInt16LE(this.upsampleLast, 0);
      out.writeInt16LE(this.upsampleLast, 2);
      this.upsampleHasHistory = false;
      this.upsampleLast = 0;
      return out;
    }

    return Buffer.alloc(0);
  }

  /** Reset all carried state (e.g. at call boundaries). */
  reset(): void {
    this.firHistory = new Int16Array(2);
    this.firHistoryValid = false;
    this.firCarry = new Int16Array(0);
    this.upsampleLast = 0;
    this.upsampleHasHistory = false;
    this.resample24Last = 0;
    this.resample24Phase = 0;
    this.resample24HasHistory = false;
    this.carry.reset();
  }

  // ---------------------------------------------------------------------------
  // Private: 16 kHz → 8 kHz
  // ---------------------------------------------------------------------------

  /**
   * 2:1 decimation with a 5-tap binomial FIR anti-alias filter.
   *
   * FIR coefficients: [1, 4, 6, 4, 1] / 16 (cutoff ~Fs/4 = 4 kHz).
   *
   * Cross-chunk state:
   * - `firHistory[0]` = s_{-2}, `firHistory[1]` = s_{-1} relative to the
   *   virtual stream (seeded to first-sample on the very first call).
   * - `firPendingSample` = a lone input sample carried from a chunk whose
   *   sample count was odd; it will become the first input of the next chunk.
   *
   * Decimation: outputs are at even positions (0, 2, 4 …) in the virtual
   * extended stream, so every 2 input samples yield 1 output. An odd-sample-
   * count chunk leaves 1 sample in `firPendingSample`; the next chunk
   * prepends it so the output cadence is unbroken.
   */
  private _downsample16kTo8k(buf: Buffer): Buffer {
    const newSampleCount = buf.length >> 1;
    const carried = this.firCarry;
    const total = carried.length + newSampleCount;

    const all = new Int16Array(total);
    all.set(carried, 0);
    for (let j = 0; j < newSampleCount; j++) {
      all[carried.length + j] = buf.readInt16LE(j * 2);
    }
    if (total === 0) return Buffer.alloc(0);

    // Seed FIR history with silence on the first call.  The correct
    // initial condition is zeros (there was no audio before this call).
    if (!this.firHistoryValid) {
      this.firHistory[0] = 0;
      this.firHistory[1] = 0;
      this.firHistoryValid = true;
    }

    // Centers sit at even stream positions 0, 2, 4 … within ``all``; each
    // output needs the +2 lookahead sample, so emit only while it exists —
    // the remaining 1-2 samples carry to the next chunk (``flush()``
    // edge-replicates at end of stream). This is what makes chunked
    // processing bit-identical to one-shot processing.
    const nOut = total >= 3 ? ((total - 3) >> 1) + 1 : 0;
    const out = Buffer.alloc(nOut * 2);
    for (let i = 0; i < nOut; i++) {
      const c = i * 2;
      const sM2 = c >= 2 ? all[c - 2] : this.firHistory[0];
      const sM1 = c >= 1 ? all[c - 1] : this.firHistory[1];
      const s0 = all[c];
      const sP1 = all[c + 1];
      const sP2 = all[c + 2];
      const filtered = (sM2 + 4 * sM1 + 6 * s0 + 4 * sP1 + sP2 + 8) >> 4;
      out.writeInt16LE(Math.max(-32768, Math.min(32767, filtered)), i * 2);
    }

    // Carry forward everything from the next (un-emittable) center on, and
    // remember the two samples preceding it as FIR history.
    const nextCenter = nOut * 2;
    if (nextCenter >= 2) {
      this.firHistory[0] = all[nextCenter - 2];
      this.firHistory[1] = all[nextCenter - 1];
    }
    this.firCarry = all.slice(nextCenter);

    return out;
  }

  // ---------------------------------------------------------------------------
  // Private: 8 kHz → 16 kHz
  // ---------------------------------------------------------------------------

  /**
   * 1:2 linear-interpolation upsampler.
   *
   * For the first chunk (no history): emits 2*(N-1) samples and defers the
   * last sample. For subsequent chunks (with history): emits the deferred
   * sample + its interpolated midpoint THEN 2*(N-1) samples from the new
   * chunk, deferring the new last sample. Total across K chunks + flush =
   * 2*total_input_samples (correct output length).
   *
   * Call flush() after the final chunk to emit the last deferred sample
   * pair (self-duplicate at end of stream).
   */
  private _upsample8kTo16k(buf: Buffer): Buffer {
    const sampleCount = buf.length >> 1;
    if (sampleCount === 0) return Buffer.alloc(0);

    const outArr: number[] = [];

    if (this.upsampleHasHistory) {
      // Emit the deferred sample + its midpoint toward sample[0].
      // sample[0] then becomes the first current in the loop below.
      const next = buf.readInt16LE(0);
      outArr.push(this.upsampleLast);
      outArr.push(Math.round((this.upsampleLast + next) / 2));
    }

    // Emit each sample and its midpoint toward the next.  The last sample is
    // deferred (startIdx 0 means s[0] is included in the loop).
    for (let i = 0; i < sampleCount - 1; i++) {
      const s0 = buf.readInt16LE(i * 2);
      const s1 = buf.readInt16LE((i + 1) * 2);
      outArr.push(s0);
      outArr.push(Math.round((s0 + s1) / 2));
    }

    // Defer the last sample of this chunk.
    this.upsampleLast = buf.readInt16LE((sampleCount - 1) * 2);
    this.upsampleHasHistory = true;

    const outBuf = Buffer.alloc(outArr.length * 2);
    for (let j = 0; j < outArr.length; j++) outBuf.writeInt16LE(outArr[j], j * 2);
    return outBuf;
  }

  // ---------------------------------------------------------------------------
  // Private: 24 kHz → 16 kHz / 8 kHz
  // ---------------------------------------------------------------------------

  /**
   * 3:2 linear-interpolation decimator (ratio srcRate/dstRate = 1.5).
   *
   * `resample24Phase` tracks the fractional input position of the next output
   * sample relative to the START of the next chunk. Negative phase means the
   * next output straddles the previous/current chunk boundary; those are
   * handled using `resample24Last`.
   */
  private _resample24kTo16k(buf: Buffer): Buffer {
    return this._resample24kStep(buf, 24000 / 16000);
  }

  /** 3:1 decimation — collapses the 24k→16k→8k chain into a single step. */
  private _resample24kTo8k(buf: Buffer): Buffer {
    return this._resample24kStep(buf, 24000 / 8000);
  }

  /** Shared phase-stepping resampler used by 24→16 (step 1.5) and 24→8 (step 3). */
  private _resample24kStep(buf: Buffer, step: number): Buffer {
    const sampleCount = buf.length >> 1;
    if (sampleCount === 0) return Buffer.alloc(0);

    const outArr: number[] = [];
    let phase = this.resample24Phase;

    while (true) {
      const idx = Math.floor(phase);
      if (idx >= sampleCount) break;

      const frac = phase - idx;
      let s0: number;
      let s1: number;

      if (idx < 0) {
        s0 = this.resample24HasHistory ? this.resample24Last : 0;
        s1 = buf.readInt16LE(0);
      } else {
        s0 = buf.readInt16LE(idx * 2);
        s1 = idx + 1 < sampleCount ? buf.readInt16LE((idx + 1) * 2) : s0;
      }

      const interp = Math.round(s0 + (s1 - s0) * frac);
      outArr.push(Math.max(-32768, Math.min(32767, interp)));
      phase += step;
    }

    this.resample24Last = buf.readInt16LE((sampleCount - 1) * 2);
    this.resample24HasHistory = true;
    this.resample24Phase = phase - sampleCount;

    const outBuf = Buffer.alloc(outArr.length * 2);
    for (let j = 0; j < outArr.length; j++) outBuf.writeInt16LE(outArr[j], j * 2);
    return outBuf;
  }
}

/** Create a stateful 16 kHz → 8 kHz downsampling resampler. */
export function createResampler16kTo8k(): StatefulResampler {
  return new StatefulResampler({ srcRate: 16000, dstRate: 8000 });
}

/** Create a stateful 8 kHz → 16 kHz upsampling resampler. */
export function createResampler8kTo16k(): StatefulResampler {
  return new StatefulResampler({ srcRate: 8000, dstRate: 16000 });
}

/** Create a stateful 24 kHz → 16 kHz resampler (3:2 linear interpolation). */
export function createResampler24kTo16k(): StatefulResampler {
  return new StatefulResampler({ srcRate: 24000, dstRate: 16000 });
}

/** Create a stateful 24 kHz → 8 kHz resampler (3:1 decimation, fix #46). */
export function createResampler24kTo8k(): StatefulResampler {
  return new StatefulResampler({ srcRate: 24000, dstRate: 8000 });
}

// ---------- Legacy stateless helpers (deprecated) ----------
// These create a one-shot StatefulResampler per call and are retained only for
// backwards compatibility.  A process-level warning fires at most once per
// function to avoid log spam in long-running processes.

let _warnedResample8kTo16k = false;
let _warnedResample16kTo8k = false;
let _warnedResample24kTo16k = false;

/**
 * Upsample 8 kHz PCM16 to 16 kHz using linear interpolation.
 *
 * For each pair of consecutive samples (s[n], s[n+1]) the output
 * contains s[n] followed by (s[n] + s[n+1]) / 2. The last sample
 * is duplicated to fill the final position.
 *
 * Output length = input length * 2.
 *
 * @deprecated Use {@link StatefulResampler} or {@link createResampler8kTo16k}
 * for streaming pipelines where chunk-boundary continuity matters.
 */
export function resample8kTo16k(pcm8k: Buffer): Buffer {
  if (!_warnedResample8kTo16k) {
    _warnedResample8kTo16k = true;
    getLogger().warn(
      '[patter] resample8kTo16k() is deprecated. ' +
      'Use createResampler8kTo16k() (StatefulResampler) to eliminate chunk-boundary discontinuities.',
    );
  }
  if (pcm8k.length === 0) return Buffer.alloc(0);
  const r = createResampler8kTo16k();
  const main = r.process(pcm8k);
  const tail = r.flush();
  return tail.length > 0 ? Buffer.concat([main, tail]) : main;
}

/**
 * Downsample 16 kHz PCM16 to 8 kHz with anti-aliasing.
 *
 * Uses a 5-tap binomial low-pass FIR filter ([1, 4, 6, 4, 1] / 16) applied
 * to every pair of input samples before decimating by 2.
 *
 * Output length = input length / 2.
 *
 * @deprecated Use {@link StatefulResampler} or {@link createResampler16kTo8k}
 * for streaming pipelines where chunk-boundary continuity matters.
 */
export function resample16kTo8k(pcm16k: Buffer): Buffer {
  if (!_warnedResample16kTo8k) {
    _warnedResample16kTo8k = true;
    getLogger().warn(
      '[patter] resample16kTo8k() is deprecated. ' +
      'Use createResampler16kTo8k() (StatefulResampler) to eliminate chunk-boundary discontinuities.',
    );
  }
  if (pcm16k.length === 0) return Buffer.alloc(0);
  const r = createResampler16kTo8k();
  const out = r.process(pcm16k);
  const tail = r.flush();
  return tail.length > 0 ? Buffer.concat([out, tail]) : out;
}

/**
 * Downsample 24 kHz PCM16 to 16 kHz with linear interpolation.
 *
 * For a 3:2 ratio, each output sample is a weighted blend of the two
 * neighbouring input samples rather than a raw pick-every-third.
 *
 * Output length = floor(inputSamples * 2 / 3) * 2 bytes.
 *
 * @deprecated Use {@link StatefulResampler} or {@link OpenAITTS.resampleStreaming}
 * for anti-aliased resampling.
 */
export function resample24kTo16k(pcm24k: Buffer): Buffer {
  if (!_warnedResample24kTo16k) {
    _warnedResample24kTo16k = true;
    getLogger().warn(
      '[patter] resample24kTo16k() is deprecated. ' +
      'Use createResampler24kTo16k() (StatefulResampler) or OpenAITTS.resampleStreaming for anti-aliased resampling.',
    );
  }
  if (pcm24k.length === 0) return Buffer.alloc(0);
  // Preserve original floor(N * 2/3) output-count semantics for backwards compat.
  const sampleCount = Math.floor(pcm24k.length / 2);
  const outSamples = Math.floor(sampleCount * 2 / 3);
  const out = Buffer.alloc(outSamples * 2);
  for (let i = 0; i < outSamples; i++) {
    const pos = i * 1.5;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    const s0 = pcm24k.readInt16LE(idx * 2);
    const s1 = idx + 1 < sampleCount ? pcm24k.readInt16LE((idx + 1) * 2) : s0;
    const interp = Math.round(s0 + (s1 - s0) * frac);
    out.writeInt16LE(Math.max(-32768, Math.min(32767, interp)), i * 2);
  }
  return out;
}

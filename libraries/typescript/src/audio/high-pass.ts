/**
 * High-pass / DC-block biquad for the inbound audio-processing chain.
 *
 * The first canonical stage of a WebRTC-style audio-processing module
 * (APM) is a high-pass filter: it strips DC offset, mains hum (50/60 Hz),
 * and sub-100 Hz handling rumble BEFORE echo cancellation, noise
 * suppression, VAD, and STT run. Those low-frequency components carry no
 * speech information yet bias the AEC's adaptive filter, inflate the VAD's
 * energy estimate, and waste STT compute — removing them up front improves
 * every downstream stage at negligible cost.
 *
 * Implements a 2nd-order RBJ ("Audio EQ Cookbook") high-pass biquad in
 * Direct-Form I, stateful across chunk boundaries so a streamed signal
 * filters identically to a one-shot pass. A 2nd-order section rolls off at
 * 12 dB/octave below the cutoff; with the default Butterworth Q (0.707) the
 * pass-band is maximally flat, so a 440 Hz tone above a 100 Hz cutoff passes
 * essentially unchanged while a 50 Hz component is attenuated ~12 dB.
 *
 * An IIR recursion cannot be vectorised (each output depends on the
 * previous one); a 2nd-order section is only ~5 multiply-adds per sample —
 * far below 1 % of one core for a 16 kHz stream.
 *
 * Mirrors Python ``getpatter/audio/high_pass.py``.
 */

/** Butterworth quality factor — 1/sqrt(2). Maximally flat pass-band. */
const DEFAULT_Q = 0.7071067811865476;

function clampInt16(value: number): number {
  if (value > 32767) return 32767;
  if (value < -32768) return -32768;
  return value;
}

/** Options for {@link BiquadHighPass}. */
export interface BiquadHighPassOptions {
  /** -3 dB corner frequency in Hz. Must be in ``(0, sampleRate/2)``. */
  readonly cutoffHz: number;
  /** Sample rate of the stream being filtered, in Hz. */
  readonly sampleRate: number;
  /** Quality factor. Defaults to Butterworth (0.707). */
  readonly q?: number;
}

/** Stateful 2nd-order high-pass / DC-block biquad (RBJ cookbook). */
export class BiquadHighPass {
  private readonly b0: number;
  private readonly b1: number;
  private readonly b2: number;
  private readonly a1: number;
  private readonly a2: number;
  // Direct-Form I delay line (previous two inputs / outputs).
  private x1 = 0;
  private x2 = 0;
  private y1 = 0;
  private y2 = 0;
  // Trailing odd byte carried across calls (decoded PCM16 is even, but stay
  // robust to upstream mis-framing).
  private carry: Buffer = Buffer.alloc(0);

  constructor(opts: BiquadHighPassOptions) {
    const { cutoffHz, sampleRate } = opts;
    const q = opts.q ?? DEFAULT_Q;
    if (sampleRate <= 0) {
      throw new Error(`BiquadHighPass: sampleRate must be > 0, got ${sampleRate}`);
    }
    const nyquist = sampleRate / 2;
    if (cutoffHz <= 0 || cutoffHz >= nyquist) {
      throw new Error(
        `BiquadHighPass: cutoffHz must be in (0, ${nyquist}), got ${cutoffHz}`,
      );
    }
    if (q <= 0) {
      throw new Error(`BiquadHighPass: q must be > 0, got ${q}`);
    }

    const w0 = (2 * Math.PI * cutoffHz) / sampleRate;
    const cosW0 = Math.cos(w0);
    const alpha = Math.sin(w0) / (2 * q);

    const b0 = (1 + cosW0) / 2;
    const b1 = -(1 + cosW0);
    const b2 = (1 + cosW0) / 2;
    const a0 = 1 + alpha;
    const a1 = -2 * cosW0;
    const a2 = 1 - alpha;

    // Normalise by a0 so the recursion needs no per-sample division.
    this.b0 = b0 / a0;
    this.b1 = b1 / a0;
    this.b2 = b2 / a0;
    this.a1 = a1 / a0;
    this.a2 = a2 / a0;
  }

  /**
   * Filter a chunk of PCM16-LE mono samples. Returns the same number of
   * samples it consumed (minus any trailing odd byte, carried to the next
   * call). State persists across calls so chunked == one-shot.
   */
  process(pcm: Buffer): Buffer {
    if (pcm.length === 0) return pcm;
    let input = pcm;
    if (this.carry.length > 0) {
      input = Buffer.concat([this.carry, input]);
      this.carry = Buffer.alloc(0);
    }
    if (input.length % 2 === 1) {
      this.carry = input.subarray(input.length - 1);
      input = input.subarray(0, input.length - 1);
    }
    if (input.length === 0) return Buffer.alloc(0);

    const n = input.length / 2;
    const out = Buffer.alloc(input.length);
    const { b0, b1, b2, a1, a2 } = this;
    let { x1, x2, y1, y2 } = this;

    for (let i = 0; i < n; i += 1) {
      const x0 = input.readInt16LE(i * 2);
      const y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
      x2 = x1;
      x1 = x0;
      y2 = y1;
      y1 = y0;
      out.writeInt16LE(clampInt16(Math.round(y0)), i * 2);
    }

    this.x1 = x1;
    this.x2 = x2;
    this.y1 = y1;
    this.y2 = y2;
    return out;
  }

  /** Clear the delay line and byte carry (e.g. at a call boundary). */
  reset(): void {
    this.x1 = 0;
    this.x2 = 0;
    this.y1 = 0;
    this.y2 = 0;
    this.carry = Buffer.alloc(0);
  }
}

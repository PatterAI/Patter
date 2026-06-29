/**
 * Speech-selective automatic gain control (AGC) for the inbound chain.
 *
 * Cheap STT models and small TTS pipelines are sensitive to input level:
 * a caller far from the mic (-35 dBFS) and one speaking right into it
 * (-10 dBFS) hit the recogniser at wildly different energies, and energy
 * normalisation toward a consistent target is a well-established way to cut
 * word-error rate on quiet/variable input. This AGC normalises the inbound
 * PCM16 stream toward a target RMS just before VAD/STT.
 *
 * Two properties keep it safe on a phone line:
 * - **Speech-selective.** Gain is only driven *up* on frames whose RMS sits
 *   above a speech floor. On silence / line-noise frames the gain is
 *   released back toward unity, so the quiet gaps between words are never
 *   amplified into a hiss (the classic "AGC pumping the noise floor"
 *   artefact).
 * - **Limited.** After gain is applied, a per-frame peak limiter scales the
 *   frame so its peak never exceeds a ceiling (default 99 % of full scale),
 *   preventing the hard digital clipping an aggressive boost would produce.
 *
 * Gain changes are smoothed with separate attack (gain decreasing — fast)
 * and release (gain increasing — slow) time constants.
 *
 * Mirrors Python ``getpatter/audio/agc.py``.
 */

/** int16 full-scale reference (0 dBFS) — a full-scale sine peaks at this. */
const FULL_SCALE = 32768;

function dbfsToLinear(dbfs: number): number {
  return FULL_SCALE * 10 ** (dbfs / 20);
}

function clampInt16(value: number): number {
  if (value > 32767) return 32767;
  if (value < -32768) return -32768;
  return value;
}

/** Resolved options for {@link Agc} (all required — the chain fills defaults). */
export interface AgcOptions {
  readonly sampleRate?: number;
  readonly targetRmsDbfs?: number;
  readonly maxGainDb?: number;
  readonly speechFloorDbfs?: number;
  readonly attackMs?: number;
  readonly releaseMs?: number;
  readonly limiterCeiling?: number;
}

/** Stateful speech-selective AGC for PCM16-LE mono frames. */
export class Agc {
  private readonly sampleRate: number;
  private readonly targetRms: number;
  private readonly maxGain: number;
  private readonly minGain: number;
  private readonly speechFloor: number;
  private readonly attackMs: number;
  private readonly releaseMs: number;
  private readonly ceiling: number;
  // Current smoothed gain (linear). Starts at unity so the first frame is
  // untouched and the gain ramps in over the next frames.
  private gain = 1.0;

  constructor(opts: AgcOptions = {}) {
    const sampleRate = opts.sampleRate ?? 16000;
    const targetRmsDbfs = opts.targetRmsDbfs ?? -18.0;
    const maxGainDb = opts.maxGainDb ?? 30.0;
    const speechFloorDbfs = opts.speechFloorDbfs ?? -45.0;
    const attackMs = opts.attackMs ?? 10.0;
    const releaseMs = opts.releaseMs ?? 200.0;
    const limiterCeiling = opts.limiterCeiling ?? 0.99;

    if (sampleRate <= 0) throw new Error(`Agc: sampleRate must be > 0, got ${sampleRate}`);
    if (targetRmsDbfs >= 0) {
      throw new Error(`Agc: targetRmsDbfs must be < 0 dBFS, got ${targetRmsDbfs}`);
    }
    if (maxGainDb <= 0) throw new Error(`Agc: maxGainDb must be > 0, got ${maxGainDb}`);
    if (attackMs <= 0 || releaseMs <= 0) {
      throw new Error(`Agc: attackMs/releaseMs must be > 0, got ${attackMs}/${releaseMs}`);
    }
    if (!(limiterCeiling > 0 && limiterCeiling <= 1)) {
      throw new Error(`Agc: limiterCeiling must be in (0, 1], got ${limiterCeiling}`);
    }

    this.sampleRate = sampleRate;
    this.targetRms = dbfsToLinear(targetRmsDbfs);
    this.maxGain = 10 ** (maxGainDb / 20);
    this.minGain = 10 ** (-maxGainDb / 20);
    this.speechFloor = dbfsToLinear(speechFloorDbfs);
    this.attackMs = attackMs;
    this.releaseMs = releaseMs;
    this.ceiling = limiterCeiling * FULL_SCALE;
  }

  /**
   * Normalise one PCM16-LE frame toward the target RMS. Returns the
   * gain-adjusted, peak-limited frame (same sample count).
   */
  process(pcm: Buffer): Buffer {
    if (pcm.length === 0) return pcm;
    const usable = pcm.length % 2 === 0 ? pcm : pcm.subarray(0, pcm.length - 1);
    const n = usable.length / 2;
    if (n === 0) return pcm;

    // Frame RMS.
    let acc = 0;
    for (let i = 0; i < n; i += 1) {
      const s = usable.readInt16LE(i * 2);
      acc += s * s;
    }
    const rms = Math.sqrt(acc / n);

    // Desired instantaneous gain. On non-speech frames release toward unity
    // so the noise floor is never pumped up.
    let desired: number;
    if (rms >= this.speechFloor && rms > 0) {
      desired = this.targetRms / rms;
      if (desired > this.maxGain) desired = this.maxGain;
      else if (desired < this.minGain) desired = this.minGain;
    } else {
      desired = 1.0;
    }

    // Smooth: fast attack when gain decreases (signal got louder), slow
    // release when gain increases (signal got quieter).
    const frameMs = (1000 * n) / this.sampleRate;
    const tau = desired > this.gain ? this.releaseMs : this.attackMs;
    const coef = 1 - Math.exp(-frameMs / tau);
    this.gain += (desired - this.gain) * coef;
    const gain = this.gain;

    // Peak limiter: never let the gained frame exceed the ceiling.
    let peak = 0;
    for (let i = 0; i < n; i += 1) {
      const mag = Math.abs(usable.readInt16LE(i * 2)) * gain;
      if (mag > peak) peak = mag;
    }
    const limitScale = peak > this.ceiling ? this.ceiling / peak : 1.0;
    const applied = gain * limitScale;

    const out = Buffer.alloc(usable.length);
    for (let i = 0; i < n; i += 1) {
      out.writeInt16LE(clampInt16(Math.round(usable.readInt16LE(i * 2) * applied)), i * 2);
    }
    return out;
  }

  /** Reset the smoothed gain to unity (e.g. at a call boundary). */
  reset(): void {
    this.gain = 1.0;
  }
}

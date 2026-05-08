/**
 * Acoustic echo cancellation for speakerphone telephony.
 *
 * The pipeline-mode VAD runs on the inbound mic stream. On a
 * speakerphone or laptop-mic deployment, the agent's outbound TTS leaks
 * back into the mic — VAD then sees continuous voice-like energy from
 * the bleed and cannot detect when the caller starts speaking. The
 * barge-in only fires during natural pauses in the TTS, which produces
 * the intermittent "interrupt sometimes works, other times the agent
 * keeps talking" symptom.
 *
 * This module implements an NLMS (normalised least-mean-squares)
 * adaptive filter that subtracts the estimated echo from the inbound
 * signal. It is NOT a drop-in replacement for production-grade echo
 * cancellation (WebRTC's AEC3, Speex AEC). For tight integration with
 * battle-tested DSP, wrap a binding to `webrtc-audio-processing-2`
 * externally.
 *
 * @example
 * ```ts
 * const aec = new NlmsEchoCanceller({ sampleRate: 16000 });
 * // In TTS path: every chunk we ship to the carrier is also fed to AEC.
 * aec.pushFarEnd(ttsPcmBuffer);
 * // In mic path: subtract estimated echo before VAD/STT.
 * const cleaned = aec.processNearEnd(micPcmBuffer);
 * await vad.processFrame(cleaned, 16000);
 * ```
 */

/**
 * Length of the adaptive filter in samples. 512 taps @ 16 kHz = 32 ms,
 * which covers the typical cellular / VoIP echo path (RT60 < 50 ms after
 * the carrier's own echo suppression has trimmed the bulk of it). 2048
 * taps were tested first but produced 8–12 s convergence on real
 * cellular calls; 512 taps converge ~4× faster with no measurable
 * cancellation loss on the paths the SDK targets. Pass
 * `filterTaps: 2048` explicitly for landline hairpin loops where the
 * tail extends beyond 32 ms.
 */
const DEFAULT_FILTER_TAPS = 512;

/**
 * NLMS step size during the steady-state phase (post-warmup). 0.1 is the
 * textbook narrowband-voice value.
 */
const DEFAULT_STEP_SIZE = 0.1;

/**
 * NLMS step size during the warm-up phase (first `warmupSeconds` of TTS
 * playback). Aggressive 5× ramp pulls the filter towards a usable echo
 * estimate within ~0.5 s instead of the 5–10 s required at the
 * steady-state step. The Geigel double-talk detector still gates updates
 * so the larger step does not drag the user's voice into the echo model.
 */
const DEFAULT_WARMUP_STEP_SIZE = 0.5;

/** Duration of the warm-up phase in seconds. */
const DEFAULT_WARMUP_SECONDS = 0.5;

/** Per-iteration leakage on the filter weights (forget stale taps). */
const DEFAULT_LEAKAGE = 0.9999;

/**
 * Geigel double-talk threshold. When `max(|near|) > rho * max(|far|)` the
 * near-end signal contains energy the far-end alone cannot explain →
 * freeze adaptation so the filter does not learn the caller's voice as
 * part of the echo path.
 */
const DEFAULT_DOUBLE_TALK_RHO = 0.6;

/** How much past far-end (TTS) audio we retain. */
const FAR_END_BUFFER_SECONDS = 0.5;

/** Constructor options for {@link NlmsEchoCanceller}. */
export interface NlmsEchoCancellerOptions {
  /** Sample rate of the inbound and outbound streams. 8000 or 16000. */
  sampleRate?: 8000 | 16000;
  /** Number of taps in the adaptive filter. Default: 512 (= 32 ms @ 16 kHz). */
  filterTaps?: number;
  /** Steady-state NLMS step in `(0, 1]`. Default: 0.1. */
  stepSize?: number;
  /** Aggressive NLMS step during the warmup window. Default: 0.5. */
  warmupStepSize?: number;
  /** Duration of the warmup window in seconds. Default: 0.5. */
  warmupSeconds?: number;
  /** Per-iteration weight leakage in `(0, 1]`. Default: 0.9999. */
  leakage?: number;
  /** Geigel rho — double-talk detector sensitivity. Default: 0.6. */
  doubleTalkRho?: number;
}

/**
 * Time-domain NLMS adaptive filter with Geigel double-talk detection.
 *
 * Designed for narrowband mono 16 kHz PCM (the format Patter's pipeline
 * pushes between transcoding and STT). 8 kHz is also accepted but the
 * default tap count translates to a 256 ms history at that rate which
 * costs more CPU per frame for diminishing return — for 8 kHz callers
 * pass `filterTaps: 1024` explicitly.
 *
 * Thread-safety: NOT thread-safe. Each call session must own its own
 * instance. The stream handler creates one per `StreamHandler`.
 */
export class NlmsEchoCanceller {
  private readonly taps: number;
  private readonly step: number;
  private readonly warmupStep: number;
  private readonly warmupSamples: number;
  private readonly leakage: number;
  private readonly rho: number;

  /** Filter coefficients — adapt to match the channel impulse response. */
  private readonly w: Float32Array;
  /** Far-end ring buffer of past TTS samples, in chronological order. */
  private readonly farBuf: Float32Array;
  private farWriteIdx = 0;
  private farFilled = 0;
  /**
   * Sample counter used to taper the step from `warmupStep` down to
   * `step` over the first `warmupSamples` of processed near-end audio.
   * Counted from the first `processNearEnd` call (not construction
   * time) so the warmup window aligns with the actual start of TTS
   * playback rather than agent setup.
   */
  private processedSamples = 0;

  /** Stats — used for diagnostics, never read on the hot path. */
  framesProcessed = 0;
  doubleTalkFrames = 0;

  constructor(opts: NlmsEchoCancellerOptions = {}) {
    const sampleRate = opts.sampleRate ?? 16000;
    if (sampleRate !== 8000 && sampleRate !== 16000) {
      throw new Error(
        `NlmsEchoCanceller supports 8000 Hz or 16000 Hz only; got ${sampleRate}.`,
      );
    }
    const taps = opts.filterTaps ?? DEFAULT_FILTER_TAPS;
    if (taps < 64) {
      throw new Error(
        `filterTaps must be >= 64 to model a meaningful echo path; got ${taps}.`,
      );
    }
    const step = opts.stepSize ?? DEFAULT_STEP_SIZE;
    if (!(step > 0 && step <= 1)) {
      throw new Error(`stepSize must be in (0, 1]; got ${step}.`);
    }
    const warmupStep = opts.warmupStepSize ?? DEFAULT_WARMUP_STEP_SIZE;
    if (!(warmupStep > 0 && warmupStep <= 1)) {
      throw new Error(
        `warmupStepSize must be in (0, 1]; got ${warmupStep}.`,
      );
    }
    const warmupSeconds = opts.warmupSeconds ?? DEFAULT_WARMUP_SECONDS;
    if (warmupSeconds < 0) {
      throw new Error(
        `warmupSeconds must be >= 0; got ${warmupSeconds}.`,
      );
    }
    const leakage = opts.leakage ?? DEFAULT_LEAKAGE;
    if (!(leakage > 0 && leakage <= 1)) {
      throw new Error(`leakage must be in (0, 1]; got ${leakage}.`);
    }

    this.taps = taps;
    this.step = step;
    this.warmupStep = warmupStep;
    this.warmupSamples = Math.floor(warmupSeconds * sampleRate);
    this.leakage = leakage;
    this.rho = opts.doubleTalkRho ?? DEFAULT_DOUBLE_TALK_RHO;

    this.w = new Float32Array(taps);
    const farBufSize = Math.max(
      taps * 2,
      Math.floor(sampleRate * FAR_END_BUFFER_SECONDS),
    );
    this.farBuf = new Float32Array(farBufSize);
  }

  /**
   * Append far-end (TTS) audio to the reference ring buffer.
   *
   * Accepts raw int16 little-endian mono PCM at the configured sample
   * rate — same shape as what we hand off to the audio sender before the
   * carrier-specific transcode.
   */
  pushFarEnd(pcm: Buffer): void {
    if (pcm.length === 0) return;
    const samples = int16BufferToFloat32(pcm);
    const n = samples.length;
    const bufLen = this.farBuf.length;
    if (n >= bufLen) {
      // Caller pushed more than we can hold — keep the most recent
      // ``bufLen`` samples and reset the head.
      this.farBuf.set(samples.subarray(n - bufLen));
      this.farWriteIdx = 0;
      this.farFilled = bufLen;
      return;
    }
    const end = this.farWriteIdx + n;
    if (end <= bufLen) {
      this.farBuf.set(samples, this.farWriteIdx);
    } else {
      const head = bufLen - this.farWriteIdx;
      this.farBuf.set(samples.subarray(0, head), this.farWriteIdx);
      this.farBuf.set(samples.subarray(head), 0);
    }
    this.farWriteIdx = (this.farWriteIdx + n) % bufLen;
    this.farFilled = Math.min(this.farFilled + n, bufLen);
  }

  /**
   * Subtract estimated echo from the near-end (mic) signal.
   *
   * Returns int16 little-endian mono PCM with the estimated echo
   * removed. When the far-end buffer hasn't been primed yet (no TTS has
   * played) the call is a pass-through — there is nothing to cancel.
   */
  processNearEnd(pcm: Buffer): Buffer {
    if (pcm.length === 0) return pcm;
    if (this.farFilled < this.taps) return pcm;

    const near = int16BufferToFloat32(pcm);
    const cleaned = this.blockNlms(near);
    this.framesProcessed += 1;
    return float32ToInt16Buffer(cleaned);
  }

  /**
   * Clear filter coefficients and far-end history.
   *
   * Useful between unrelated turns when the echo path may have changed
   * (e.g. caller switched from speakerphone to handset).
   */
  reset(): void {
    this.w.fill(0);
    this.farBuf.fill(0);
    this.farWriteIdx = 0;
    this.farFilled = 0;
    this.processedSamples = 0;
    this.framesProcessed = 0;
    this.doubleTalkFrames = 0;
  }

  // --------------------------------------------------------------------
  // Internals
  // --------------------------------------------------------------------

  /** Most-recent ``length`` far-end samples in chronological order. */
  private farWindow(length: number): Float32Array {
    const bufLen = this.farBuf.length;
    const len = Math.min(length, this.farFilled);
    const end = this.farWriteIdx;
    if (end >= len) {
      return this.farBuf.subarray(end - len, end);
    }
    const head = bufLen - (len - end);
    const out = new Float32Array(len);
    out.set(this.farBuf.subarray(head));
    out.set(this.farBuf.subarray(0, end), bufLen - head);
    return out;
  }

  /** Sample-by-sample NLMS over a frame of near-end samples. */
  private blockNlms(near: Float32Array): Float32Array {
    const taps = this.taps;
    const desiredLen = taps + near.length - 1;
    let farWindow = this.farWindow(desiredLen);
    if (farWindow.length < desiredLen) {
      const padded = new Float32Array(desiredLen);
      padded.set(farWindow, desiredLen - farWindow.length);
      farWindow = padded;
    }

    // Geigel double-talk detector — frame-wise.
    let farMax = 0;
    for (let i = 0; i < farWindow.length; i++) {
      const a = Math.abs(farWindow[i]);
      if (a > farMax) farMax = a;
    }
    let nearMax = 0;
    for (let i = 0; i < near.length; i++) {
      const a = Math.abs(near[i]);
      if (a > nearMax) nearMax = a;
    }
    const doubleTalk = farMax > 1e-6 ? nearMax > this.rho * farMax : false;
    if (doubleTalk) this.doubleTalkFrames += 1;

    const out = new Float32Array(near.length);
    const w = this.w;
    const leakage = this.leakage;
    // Per-frame step. During the warmup window the aggressive
    // ``warmupStep`` pulls the filter towards a usable echo estimate
    // within ~0.5 s; afterwards we taper to ``step`` for stable
    // steady-state tracking. Step is constant within a frame so the
    // inner loop stays branch-free.
    const step =
      this.processedSamples < this.warmupSamples ? this.warmupStep : this.step;
    for (let i = 0; i < near.length; i++) {
      // ``x`` is the most recent ``taps`` samples ending at this output's
      // emission time (slid forward one position per output sample).
      let yEst = 0;
      let norm = 0;
      const base = i;
      for (let k = 0; k < taps; k++) {
        const xk = farWindow[base + k];
        yEst += w[k] * xk;
        norm += xk * xk;
      }
      const e = near[i] - yEst;
      out[i] = e;
      if (!doubleTalk) {
        const denom = norm + 1e-6;
        const factor = (step * e) / denom;
        for (let k = 0; k < taps; k++) {
          w[k] = leakage * w[k] + factor * farWindow[base + k];
        }
      }
    }
    this.processedSamples += near.length;
    return out;
  }
}

// ----------------------------------------------------------------------
// PCM helpers
// ----------------------------------------------------------------------

/** Decode int16 little-endian PCM bytes into a Float32 array in [-1, 1]. */
function int16BufferToFloat32(buf: Buffer): Float32Array {
  const samples = new Float32Array(buf.length / 2);
  for (let i = 0; i < samples.length; i++) {
    samples[i] = buf.readInt16LE(i * 2) / 32768;
  }
  return samples;
}

/** Encode a Float32 array (normalised to ~[-1, 1]) into int16 LE PCM. */
function float32ToInt16Buffer(samples: Float32Array): Buffer {
  const out = Buffer.alloc(samples.length * 2);
  for (let i = 0; i < samples.length; i++) {
    let v = Math.round(samples[i] * 32768);
    if (v > 32767) v = 32767;
    else if (v < -32768) v = -32768;
    out.writeInt16LE(v, i * 2);
  }
  return out;
}

/**
 * Inbound audio processing chain for pipeline mode (slice 1 of the
 * StreamHandler decomposition — see ``docs/architecture/pipeline-stages.md``).
 *
 * Owns the stateless-to-STT half of ``handleAudio``:
 *
 *     decode (mulaw -> PCM16) -> high-pass/DC-block (opt) -> resample
 *     -> AEC near-end (opt) -> ``agent.audioFilter``/NS (opt)
 *     -> AGC (opt) -> VAD frame feed
 *
 * and returns the processed frame plus at most one VAD event per frame. The
 * handler keeps everything downstream for this slice (VAD-event handling,
 * self-hearing gate, inbound ring buffer, ``beforeSendToStt`` hook, STT feed)
 * so the change stays reviewable.
 *
 * Stage-order contract (fixed) — canonical WebRTC APM order
 * ``HPF -> AEC -> NS -> VAD -> AGC`` adapted for telephony (AGC pulled ahead
 * of VAD/STT so the recogniser sees a level-normalised signal):
 * - The high-pass / DC-block runs FIRST, on the decoded signal BEFORE the
 *   resampler, so DC / sub-cutoff rumble never reaches the echo canceller.
 * - AEC runs before the noise suppressor so the suppressor never disturbs the
 *   canceller's far-end/near-end alignment.
 * - ``audioFilter`` (noise suppression) runs AFTER AEC and BEFORE VAD, per the
 *   ``AudioFilter`` interface contract — the VAD then benefits from the
 *   cleaned signal.
 * - AGC runs AFTER noise suppression and BEFORE VAD/STT so it normalises the
 *   cleaned signal (never amplifying suppressed noise) and the level-normalised
 *   frame helps both the VAD energy gate and the STT recogniser.
 *
 * Every new stage (HPF, AGC) is OPT-IN, provider-agnostic, and fail-open
 * (a raising stage degrades to passthrough, warns once, then DEBUG).
 *
 * The filter wrapper is fail-open with a warn-once policy: a rejecting (or
 * non-Buffer-returning) filter degrades to passthrough of the pre-filter PCM,
 * logs one WARNING, keeps logging at DEBUG, and keeps being attempted — a
 * transient provider hiccup must not permanently strip noise suppression, and
 * a permanent one must never break the call audio path.
 *
 * AEC / audio-filter / VAD are resolved through late-bound getter callables
 * rather than captured at construction: ``StreamHandler`` populates ``aec`` /
 * ``autoVad`` during ``initPipeline`` (after this chain is constructed) and
 * the unit suites assign them directly on handler instances.
 *
 * Mirrors Python ``getpatter/services/input_chain.py``.
 */

import { getLogger } from '../logger';
import { mulawToPcm16, StatefulResampler } from '../audio/transcoding';
import { BiquadHighPass } from '../audio/high-pass';
import { Agc } from '../audio/agc';
import type { AudioFilter, VADEvent, VADProvider, AgcConfig } from '../types';

/** The pipeline's internal processing rate: inbound carrier audio is always
 *  normalised to PCM16 mono @ 16 kHz before AEC / filter / VAD / STT. */
export const PIPELINE_SAMPLE_RATE = 16000;

/** Minimal AEC surface the chain needs (avoids a hard import of audio/aec). */
export interface NearEndProcessor {
  processNearEnd(pcm: Buffer): Buffer;
}

/** Result of pushing one carrier media frame through the input chain. */
export interface ProcessedInputFrame {
  /** Decoded / resampled / AEC'd / filtered PCM16 16 kHz bytes — exactly
   *  what should reach the self-hearing gate and STT. */
  readonly pcm16k: Buffer;
  /** The VAD event emitted for this frame, if any. */
  readonly vadEvent: VADEvent | null;
  /** True when a VAD provider is configured for this call — the handler's
   *  self-hearing gate applies even while VAD is error-disabled. */
  readonly vadConfigured: boolean;
}

/** Constructor options for {@link InputProcessingChain}. */
export interface InputProcessingChainOptions {
  /** The per-call stateful 8k→16k resampler (injected so the handler can
   *  flush its tail on call close — see ``flushResamplers``). Used for the
   *  mu-law decode path. */
  readonly resampler: StatefulResampler;
  /** Late-bound accessor for the optional NLMS echo canceller. */
  readonly getAec: () => NearEndProcessor | null | undefined;
  /** Late-bound accessor for the optional ``agent.audioFilter``. */
  readonly getAudioFilter: () => AudioFilter | null | undefined;
  /** Late-bound accessor for the active VAD (``agent.vad`` ?? auto-Silero). */
  readonly getVad: () => VADProvider | null | undefined;
  /** H4 hot-path guard: max ms to wait for VAD inference before treating
   *  the frame as silent. Defaults to 25 ms (the pre-extraction value). */
  readonly vadTimeoutMs?: number;
  /** When ``true`` (default) inbound frames are G.711 mu-law @ 8 kHz and are
   *  decoded + upsampled to 16 kHz. When ``false`` they are linear PCM16 at
   *  {@link inputSampleRate}. All current carriers are mu-law. */
  readonly inputIsMulaw8k?: boolean;
  /** Sample rate (Hz) of the inbound PCM16 stream when ``inputIsMulaw8k`` is
   *  ``false``. Default 16000. When not 16 kHz the chain resamples to 16 kHz
   *  instead of forwarding the bytes at the wrong rate. */
  readonly inputSampleRate?: number;
  /** Optional high-pass / DC-block cutoff (Hz). Runs as the FIRST stage, on
   *  the decoded signal before the resampler. Undefined disables it. */
  readonly highPassHz?: number;
  /** Optional inbound AGC. ``true`` uses {@link AgcConfig} defaults, an object
   *  tunes it, ``false``/undefined disables it. Runs after noise suppression
   *  and before VAD/STT. */
  readonly agc?: boolean | AgcConfig;
}

/** HPF -> resample -> AEC -> audioFilter -> AGC -> VAD for inbound audio. */
export class InputProcessingChain {
  private readonly opts: InputProcessingChainOptions;
  /** Warn-once latch for the fail-open audio-filter wrapper. */
  private filterWarned = false;
  /** Warn-once latches for the fail-open HPF / AGC wrappers. */
  private hpfWarned = false;
  private agcWarned = false;
  /** Set after a VAD failure to suppress log spam for the rest of the call. */
  private vadDisabled = false;

  private readonly inputIsMulaw8k: boolean;
  private readonly inputSampleRate: number;
  /** Optional first-stage high-pass / DC-block (built once, reused). */
  private readonly hpf: BiquadHighPass | null = null;
  /** Optional AGC (built once, reused). */
  private readonly agc: Agc | null = null;
  /** Lazily-created resampler for the non-mulaw, non-16 kHz PCM path. */
  private pcmResampler: StatefulResampler | null = null;

  constructor(opts: InputProcessingChainOptions) {
    this.opts = opts;
    this.inputIsMulaw8k = opts.inputIsMulaw8k ?? true;
    this.inputSampleRate = opts.inputSampleRate ?? PIPELINE_SAMPLE_RATE;
    if (this.inputSampleRate <= 0) {
      throw new Error(`inputSampleRate must be > 0, got ${this.inputSampleRate}`);
    }

    // High-pass runs on the decoded signal BEFORE the resampler, so its
    // operating rate is the carrier rate (8 kHz for mu-law, else the PCM
    // input rate). A bad cutoff disables the stage (fail-open).
    if (opts.highPassHz !== undefined) {
      const hpfRate = this.inputIsMulaw8k ? 8000 : this.inputSampleRate;
      try {
        this.hpf = new BiquadHighPass({ cutoffHz: opts.highPassHz, sampleRate: hpfRate });
      } catch (err) {
        getLogger().warn(
          `highPassHz=${opts.highPassHz} could not be initialised at ${hpfRate} Hz ` +
            `(stage disabled): ${String(err)}`,
        );
      }
    }

    // AGC runs at the pipeline rate (16 kHz) after noise suppression.
    if (opts.agc) {
      const cfg: AgcConfig = opts.agc === true ? {} : opts.agc;
      try {
        this.agc = new Agc({
          sampleRate: PIPELINE_SAMPLE_RATE,
          targetRmsDbfs: cfg.targetRmsDbfs,
          maxGainDb: cfg.maxGainDb,
          speechFloorDbfs: cfg.speechFloorDbfs,
          attackMs: cfg.attackMs,
          releaseMs: cfg.releaseMs,
          limiterCeiling: cfg.limiterCeiling,
        });
      } catch (err) {
        getLogger().warn(`agc could not be initialised (stage disabled): ${String(err)}`);
      }
    }
  }

  /**
   * Run one inbound media frame through decode -> AEC -> filter -> VAD.
   *
   * Never rejects for filter failures (fail-open passthrough, warn once).
   * A VAD failure disables VAD for the rest of the call (warn once) —
   * parity with the pre-extraction handler behaviour.
   */
  async process(audioBuffer: Buffer): Promise<ProcessedInputFrame> {
    // Decode -> HPF -> resample. Run the optional high-pass / DC-block on the
    // decoded signal at the CARRIER rate (before the resampler), then
    // normalise to 16 kHz. Running the HPF first keeps DC / rumble out of the
    // interpolation and matches the canonical APM order.
    let pcm16k: Buffer;
    if (this.inputIsMulaw8k) {
      // All current carriers (Twilio, Plivo, Telnyx-PCMU) deliver mu-law 8 kHz.
      let pcm8k = mulawToPcm16(audioBuffer);
      pcm8k = this.applyHighPass(pcm8k);
      pcm16k = this.opts.resampler.process(pcm8k);
    } else {
      pcm16k = this.applyHighPass(audioBuffer);
      if (this.inputSampleRate !== PIPELINE_SAMPLE_RATE) {
        // PCM16 at a non-16 kHz rate: resample instead of forwarding the bytes
        // at the wrong rate (which pitches the caller and corrupts STT).
        if (!this.pcmResampler) {
          this.pcmResampler = new StatefulResampler({
            srcRate: this.inputSampleRate,
            dstRate: PIPELINE_SAMPLE_RATE,
          });
        }
        pcm16k = this.pcmResampler.process(pcm16k);
      }
    }

    // Acoustic echo cancellation — subtract estimated TTS bleed from the
    // mic stream before filter/VAD/STT see it. Pass-through until the
    // canceller has enough far-end history to fill its filter window
    // (~128 ms), then converges over the next 0.5–2 s of TTS-only frames.
    const aec = this.opts.getAec();
    if (aec) {
      pcm16k = aec.processNearEnd(pcm16k);
    }

    // Noise suppression (``agent.audioFilter`` — DeepFilterNet/Krisp).
    // AFTER AEC, BEFORE VAD per the AudioFilter contract. Fail-open: a
    // broken filter must never take down the call audio path.
    const audioFilter = this.opts.getAudioFilter();
    if (audioFilter) {
      try {
        const filtered = await audioFilter.process(pcm16k, PIPELINE_SAMPLE_RATE);
        if (Buffer.isBuffer(filtered)) {
          pcm16k = filtered;
        } else {
          this.warnFilterOnce(
            audioFilter,
            `process() returned ${typeof filtered}, expected Buffer`,
          );
        }
      } catch (err) {
        this.warnFilterOnce(audioFilter, String(err));
      }
    }

    // AGC — normalise level AFTER noise suppression, BEFORE VAD/STT.
    // Speech-selective + peak-limited; fail-open like audioFilter.
    if (this.agc) {
      try {
        const gained = this.agc.process(pcm16k);
        if (Buffer.isBuffer(gained)) {
          pcm16k = gained;
        } else {
          this.warnAgcOnce(`process() returned ${typeof gained}, expected Buffer`);
        }
      } catch (err) {
        this.warnAgcOnce(String(err));
      }
    }

    // External VAD (e.g. Silero) when configured — runs on the (AEC'd,
    // filtered, AGC'd) frame so noise suppression improves barge-in robustness.
    const activeVad = this.opts.getVad();
    let vadEvent: VADEvent | null = null;
    if (activeVad && !this.vadDisabled) {
      try {
        // H4: protect hot path against slow ONNX inference — if VAD takes
        // longer than ``vadTimeoutMs``, treat the frame as silent and continue.
        const vadPromise = activeVad.processFrame(pcm16k, PIPELINE_SAMPLE_RATE);
        let vadTimeoutId: ReturnType<typeof setTimeout>;
        const timeoutPromise = new Promise<null>((resolve) => {
          vadTimeoutId = setTimeout(() => resolve(null), this.opts.vadTimeoutMs ?? 25);
        });
        vadEvent = (await Promise.race([vadPromise, timeoutPromise])) ?? null;
        clearTimeout(vadTimeoutId!);
      } catch (err) {
        this.disableVad();
        getLogger().warn(
          `VAD processFrame failed — disabling VAD for this call: ${String(err)}`,
        );
      }
    }

    return { pcm16k, vadEvent, vadConfigured: Boolean(activeVad) };
  }

  /**
   * Disable VAD for the rest of the call. Also invoked by the handler when
   * VAD-event handling throws, preserving the pre-extraction semantics where
   * one try/catch covered both inference and event handling.
   */
  disableVad(): void {
    this.vadDisabled = true;
  }

  /** Whether VAD has been error-disabled for this call. */
  isVadDisabled(): boolean {
    return this.vadDisabled;
  }

  /** Run the optional high-pass stage (fail-open passthrough, warn once). */
  private applyHighPass(pcm: Buffer): Buffer {
    if (!this.hpf) return pcm;
    try {
      const out = this.hpf.process(pcm);
      return Buffer.isBuffer(out) ? out : pcm;
    } catch (err) {
      if (!this.hpfWarned) {
        this.hpfWarned = true;
        getLogger().warn(
          `high-pass stage failed; passing audio through unfiltered ` +
            `(further failures logged at DEBUG): ${String(err)}`,
        );
      } else {
        getLogger().debug(`high-pass stage failed (passthrough): ${String(err)}`);
      }
      return pcm;
    }
  }

  private warnAgcOnce(detail: string): void {
    if (!this.agcWarned) {
      this.agcWarned = true;
      getLogger().warn(
        `AGC stage failed; passing audio through without gain ` +
          `(further failures logged at DEBUG): ${detail}`,
      );
    } else {
      getLogger().debug(`AGC stage failed (passthrough): ${detail}`);
    }
  }

  private warnFilterOnce(audioFilter: AudioFilter, detail: string): void {
    const name = audioFilter.constructor?.name ?? 'AudioFilter';
    if (!this.filterWarned) {
      this.filterWarned = true;
      getLogger().warn(
        `audioFilter ${name} failed; passing audio through unfiltered ` +
          `(further failures logged at DEBUG): ${detail}`,
      );
    } else {
      getLogger().debug(`audioFilter ${name} failed (passthrough): ${detail}`);
    }
  }
}

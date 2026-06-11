/**
 * Inbound audio processing chain for pipeline mode (slice 1 of the
 * StreamHandler decomposition — see ``docs/architecture/pipeline-stages.md``).
 *
 * Owns the stateless-to-STT half of ``handleAudio``:
 *
 *     decode (mulaw -> PCM16) -> resample 8 kHz -> 16 kHz (stateful)
 *     -> AEC near-end -> ``agent.audioFilter`` -> VAD frame feed
 *
 * and returns the processed frame plus at most one VAD event per frame. The
 * handler keeps everything downstream for this slice (VAD-event handling,
 * self-hearing gate, inbound ring buffer, ``beforeSendToStt`` hook, STT feed)
 * so the change stays reviewable.
 *
 * Stage-order contract (fixed):
 * - AEC runs FIRST so the noise suppressor never disturbs the canceller's
 *   far-end/near-end alignment.
 * - ``audioFilter`` runs AFTER AEC and BEFORE VAD, per the ``AudioFilter``
 *   interface contract ("Pre-STT audio filter", integrated before VAD and
 *   STT) — the VAD then benefits from the cleaned signal.
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
import { mulawToPcm16 } from '../audio/transcoding';
import type { StatefulResampler } from '../audio/transcoding';
import type { AudioFilter, VADEvent, VADProvider } from '../types';

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
   *  flush its tail on call close — see ``flushResamplers``). */
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
}

/** Decode -> AEC -> audioFilter -> VAD for one call's inbound audio. */
export class InputProcessingChain {
  private readonly opts: InputProcessingChainOptions;
  /** Warn-once latch for the fail-open audio-filter wrapper. */
  private filterWarned = false;
  /** Set after a VAD failure to suppress log spam for the rest of the call. */
  private vadDisabled = false;

  constructor(opts: InputProcessingChainOptions) {
    this.opts = opts;
  }

  /**
   * Run one inbound media frame through decode -> AEC -> filter -> VAD.
   *
   * Never rejects for filter failures (fail-open passthrough, warn once).
   * A VAD failure disables VAD for the rest of the call (warn once) —
   * parity with the pre-extraction handler behaviour.
   */
  async process(audioBuffer: Buffer): Promise<ProcessedInputFrame> {
    // Both Twilio and Telnyx (with default streaming_start PCMU bidirectional)
    // deliver mulaw 8 kHz — always transcode to PCM16 16 kHz before STT.
    const pcm8k = mulawToPcm16(audioBuffer);
    let pcm16k = this.opts.resampler.process(pcm8k);

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

    // External VAD (e.g. Silero) when configured — runs on the (AEC'd,
    // filtered) frame so noise suppression improves barge-in robustness.
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

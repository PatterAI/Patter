/** Soniox TTS for Patter pipeline mode. */
import {
  SonioxTTS as _SonioxTTS,
  SonioxTTSAudioFormat,
  type SonioxTTSModel,
  type SonioxTTSSampleRate,
} from '../providers/soniox-tts';

/** Constructor options for the Soniox `TTS` adapter. */
export interface SonioxTTSOptions {
  /** API key. Falls back to SONIOX_API_KEY env var when omitted (shared with Soniox STT). */
  apiKey?: string;
  model?: SonioxTTSModel | string;
  voice?: string;
  language?: string;
  /** Audio encoding. Default `pcm_s16le`; `pcm_mulaw` @ 8 kHz = carrier-native. */
  audioFormat?: SonioxTTSAudioFormat | string;
  sampleRate?: SonioxTTSSampleRate | number;
  bitrate?: number;
  speed?: number;
  baseUrl?: string;
}

/** Options for the carrier-specific factories — same as the constructor minus `sampleRate`/`audioFormat`. */
export type SonioxCarrierOptions = Omit<
  SonioxTTSOptions,
  'sampleRate' | 'audioFormat'
>;

function resolveApiKey(apiKey: string | undefined): string {
  // Shared credential with Soniox STT — same SONIOX_API_KEY env var.
  const key = apiKey ?? process.env.SONIOX_API_KEY;
  if (!key) {
    throw new Error(
      "Soniox TTS requires an apiKey. Pass { apiKey: '...' } or " +
        'set SONIOX_API_KEY in the environment.',
    );
  }
  return key;
}

/**
 * Soniox TTS (`tts-rt-v1`, default voice `Adrian`).
 *
 * Shares the `SONIOX_API_KEY` credential with the Soniox STT adapter.
 *
 * @example
 * ```ts
 * import * as soniox from "getpatter/tts/soniox";
 * const tts = new soniox.TTS();                          // reads SONIOX_API_KEY
 * const tts = new soniox.TTS({ apiKey: "...", voice: "Maya", language: "en" });
 * ```
 *
 * **Telephony** — use {@link TTS.forTwilio} or {@link TTS.forTelnyx} on phone
 * calls. Both emit μ-law @ 8 kHz natively (Soniox supports G.711 directly), so
 * the pipeline skips resampling and PCM → μ-law encoding entirely (bit-clean
 * passthrough).
 */
export class TTS extends _SonioxTTS {
  static readonly providerKey = 'soniox_tts';
  constructor(opts: SonioxTTSOptions = {}) {
    const key = resolveApiKey(opts.apiKey);
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (μ-law @ 8 kHz native — carrier-wire passthrough, no resample/encode). */
  static override forTwilio(opts?: SonioxCarrierOptions): TTS;
  // Parent-compatible overload — accepts the legacy positional form too.
  static override forTwilio(apiKey: string, options?: SonioxCarrierOptions): TTS;
  static override forTwilio(
    arg1?: string | SonioxCarrierOptions,
    arg2?: SonioxCarrierOptions,
  ): TTS {
    const opts: SonioxCarrierOptions =
      typeof arg1 === 'string' ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({
      ...opts,
      audioFormat: SonioxTTSAudioFormat.PCM_MULAW,
      sampleRate: 8000,
    });
  }

  /** Pipeline TTS pre-configured for Telnyx (μ-law @ 8 kHz native — PCMU wire passthrough). */
  static override forTelnyx(opts?: SonioxCarrierOptions): TTS;
  static override forTelnyx(apiKey: string, options?: SonioxCarrierOptions): TTS;
  static override forTelnyx(
    arg1?: string | SonioxCarrierOptions,
    arg2?: SonioxCarrierOptions,
  ): TTS {
    const opts: SonioxCarrierOptions =
      typeof arg1 === 'string' ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({
      ...opts,
      audioFormat: SonioxTTSAudioFormat.PCM_MULAW,
      sampleRate: 8000,
    });
  }
}

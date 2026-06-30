/** Sarvam AI TTS for Patter pipeline mode. */
import {
  SarvamTTS as _SarvamTTS,
  SarvamAudioCodec,
  SarvamSampleRate,
  type SarvamLanguage,
  type SarvamModel,
} from '../providers/sarvam-tts';

/** Constructor options for the Sarvam `TTS` adapter. */
export interface SarvamTTSOptions {
  /** API key. Falls back to SARVAM_API_KEY env var when omitted. */
  apiKey?: string;
  model?: SarvamModel | string;
  speaker?: string;
  /** BCP-47 language code, e.g. `"hi-IN"`, `"ta-IN"`, `"en-IN"`. Default `"en-IN"`. */
  language?: SarvamLanguage | string;
  /** Audio codec. Default `linear16`; `mulaw` @ 8 kHz = carrier-native. */
  codec?: SarvamAudioCodec | string;
  sampleRate?: SarvamSampleRate | number;
  pace?: number;
  pitch?: number;
  loudness?: number;
  temperature?: number;
  enablePreprocessing?: boolean;
  dictId?: string;
  baseUrl?: string;
}

/** Options for the carrier-specific factories — same as the constructor minus `codec`/`sampleRate`. */
export type SarvamCarrierOptions = Omit<SarvamTTSOptions, 'codec' | 'sampleRate'>;

function resolveApiKey(apiKey: string | undefined): string {
  const key = apiKey ?? process.env.SARVAM_API_KEY;
  if (!key) {
    throw new Error(
      "Sarvam TTS requires an apiKey. Pass { apiKey: '...' } or " +
        'set SARVAM_API_KEY in the environment.',
    );
  }
  return key;
}

/**
 * Sarvam Bulbul TTS for Indian languages — defaults to the bulbul:v3 model.
 *
 * Synthesizes Hindi, Bengali, Tamil, Telugu, Kannada, Malayalam, Marathi,
 * Gujarati, Punjabi, Odia and Indian English (plus code-mixed text). Select the
 * language via `language` (a Sarvam BCP-47 code such as `"hi-IN"`).
 *
 * @example
 * ```ts
 * import * as sarvam from "getpatter/tts/sarvam";
 * const tts = new sarvam.TTS();                               // reads SARVAM_API_KEY
 * const tts = new sarvam.TTS({ language: "hi-IN", speaker: "shubh" });
 * ```
 *
 * **Telephony** — use {@link TTS.forTwilio} or {@link TTS.forTelnyx} on phone
 * calls. Both emit μ-law @ 8 kHz natively (Sarvam supports it), so the pipeline
 * skips resampling and PCM → μ-law encoding entirely (bit-clean passthrough).
 */
export class TTS extends _SarvamTTS {
  static readonly providerKey = 'sarvam';
  constructor(opts: SarvamTTSOptions = {}) {
    const key = resolveApiKey(opts.apiKey);
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (μ-law @ 8 kHz native — carrier-wire passthrough). */
  static override forTwilio(opts?: SarvamCarrierOptions): TTS;
  // Parent-compatible overload — accepts the legacy positional form too.
  static override forTwilio(apiKey: string, options?: SarvamCarrierOptions): TTS;
  static override forTwilio(
    arg1?: string | SarvamCarrierOptions,
    arg2?: SarvamCarrierOptions,
  ): TTS {
    const opts: SarvamCarrierOptions =
      typeof arg1 === 'string' ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({
      ...opts,
      codec: SarvamAudioCodec.MULAW,
      sampleRate: SarvamSampleRate.HZ_8000,
    });
  }

  /** Pipeline TTS pre-configured for Telnyx (μ-law @ 8 kHz native — PCMU wire passthrough). */
  static override forTelnyx(opts?: SarvamCarrierOptions): TTS;
  static override forTelnyx(apiKey: string, options?: SarvamCarrierOptions): TTS;
  static override forTelnyx(
    arg1?: string | SarvamCarrierOptions,
    arg2?: SarvamCarrierOptions,
  ): TTS {
    const opts: SarvamCarrierOptions =
      typeof arg1 === 'string' ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({
      ...opts,
      codec: SarvamAudioCodec.MULAW,
      sampleRate: SarvamSampleRate.HZ_8000,
    });
  }
}

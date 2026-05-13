/** ElevenLabs TTS for Patter pipeline mode. */
import {
  ElevenLabsTTS as _ElevenLabsTTS,
  type ElevenLabsModel,
  type ElevenLabsOutputFormat,
} from "../providers/elevenlabs-tts";

export type { ElevenLabsModel };

/** Constructor options for the ElevenLabs `TTS` adapter. */
export interface ElevenLabsTTSOptions {
  /** API key. Falls back to ELEVENLABS_API_KEY env var when omitted. */
  readonly apiKey?: string;
  readonly voiceId?: string;
  /**
   * ElevenLabs voice model ID. Default is ``eleven_flash_v2_5`` (lowest TTFT).
   * Pass ``eleven_v3`` for highest quality, or any string for forward-compat.
   */
  readonly modelId?: ElevenLabsModel | string;
  readonly outputFormat?: string;
  /**
   * BCP-47 language code (e.g. `"it"`, `"es"`). Forwarded to ElevenLabs as
   * the `language_code` request body field — required for multilingual /
   * Flash v2.5 voices to render the right accent.
   */
  readonly languageCode?: string;
  /** ElevenLabs `voice_settings` object (stability, similarity_boost, …). */
  readonly voiceSettings?: Record<string, unknown>;
}

/** Options for the carrier-specific factories — same as the constructor minus `outputFormat`. */
export type ElevenLabsCarrierOptions = Omit<ElevenLabsTTSOptions, "outputFormat">;

function resolveApiKey(apiKey: string | undefined): string {
  const key = apiKey ?? process.env.ELEVENLABS_API_KEY;
  if (!key) {
    throw new Error(
      "ElevenLabs TTS requires an apiKey. Pass { apiKey: '...' } or " +
        "set ELEVENLABS_API_KEY in the environment.",
    );
  }
  return key;
}

/**
 * ElevenLabs TTS.
 *
 * @example
 * ```ts
 * import * as elevenlabs from "getpatter/tts/elevenlabs";
 * const tts = new elevenlabs.TTS();              // reads ELEVENLABS_API_KEY
 * const tts = new elevenlabs.TTS({ apiKey: "...", voiceId: "rachel" });
 * ```
 *
 * **Telephony optimization** — use {@link TTS.forTwilio} (μ-law @ 8 kHz,
 * native Twilio Media Streams format) or {@link TTS.forTelnyx} (PCM @
 * 16 kHz, native Telnyx default) on phone calls to skip the SDK-side
 * resampling / transcoding step.
 */
export class TTS extends _ElevenLabsTTS {
  static readonly providerKey = "elevenlabs";
  constructor(opts: ElevenLabsTTSOptions = {}) {
    // Use the parent's options-object overload so optional fields
    // (languageCode, voiceSettings) reach the underlying provider —
    // the legacy positional signature drops them silently.
    super(resolveApiKey(opts.apiKey), {
      voiceId: opts.voiceId ?? "EXAVITQu4vr4xnSDxMaL",
      modelId: opts.modelId ?? "eleven_flash_v2_5",
      outputFormat: (opts.outputFormat ?? "pcm_16000") as ElevenLabsOutputFormat,
      languageCode: opts.languageCode,
      voiceSettings: opts.voiceSettings as never,
    });
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (`ulaw_8000`). */
  static override forTwilio(opts?: ElevenLabsCarrierOptions): TTS;
  // Parent-compatible overload — accepts the legacy positional form too.
  static override forTwilio(
    apiKey: string,
    options?: Omit<ElevenLabsTTSOptions, "outputFormat">,
  ): TTS;
  static override forTwilio(
    arg1?: string | ElevenLabsCarrierOptions,
    arg2?: Omit<ElevenLabsTTSOptions, "outputFormat">,
  ): TTS {
    const opts: ElevenLabsCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, outputFormat: "ulaw_8000" });
  }

  /** Pipeline TTS pre-configured for Telnyx (`pcm_16000`). */
  static override forTelnyx(opts?: ElevenLabsCarrierOptions): TTS;
  static override forTelnyx(
    apiKey: string,
    options?: Omit<ElevenLabsTTSOptions, "outputFormat">,
  ): TTS;
  static override forTelnyx(
    arg1?: string | ElevenLabsCarrierOptions,
    arg2?: Omit<ElevenLabsTTSOptions, "outputFormat">,
  ): TTS {
    const opts: ElevenLabsCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, outputFormat: "pcm_16000" });
  }
}

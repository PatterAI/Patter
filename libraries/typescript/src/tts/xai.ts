/** xAI TTS for Patter pipeline mode. */
import {
  XaiTTS as _XaiTTS,
  XaiTTSCodec,
  type XaiTTSSampleRate,
} from "../providers/xai-tts";

/** Constructor options for the xAI `TTS` adapter. */
export interface XaiTTSOptions {
  /** API key. Falls back to XAI_API_KEY env var when omitted. */
  apiKey?: string;
  voice?: string;
  /** BCP-47 language tag, or `"auto"` (default). */
  language?: string;
  /** Output codec. Default `pcm`; `mulaw` @ 8 kHz = carrier-native. */
  codec?: XaiTTSCodec | string;
  sampleRate?: XaiTTSSampleRate | number;
  bitRate?: number;
  speed?: number;
  optimizeStreamingLatency?: number;
  textNormalization?: boolean;
  baseUrl?: string;
}

/** Options for the carrier-specific factories — same as the constructor minus `sampleRate`/`codec`. */
export type XaiCarrierOptions = Omit<XaiTTSOptions, "sampleRate" | "codec">;

function resolveApiKey(apiKey: string | undefined): string {
  const key = apiKey ?? process.env.XAI_API_KEY;
  if (!key) {
    throw new Error(
      "xAI TTS requires an apiKey. Pass { apiKey: '...' } or " +
        "set XAI_API_KEY in the environment.",
    );
  }
  return key;
}

/**
 * xAI TTS (default voice `eve`) — Beta.
 *
 * @example
 * ```ts
 * import * as xai from "getpatter/tts/xai";
 * const tts = new xai.TTS();                          // reads XAI_API_KEY
 * const tts = new xai.TTS({ apiKey: "...", voice: "leo", language: "en" });
 * ```
 *
 * **Telephony** — use {@link TTS.forTwilio} (μ-law @ 8 kHz native) or
 * {@link TTS.forTelnyx} (pcm @ 16 kHz) on phone calls.
 */
export class TTS extends _XaiTTS {
  static readonly providerKey = "xai_tts";
  constructor(opts: XaiTTSOptions = {}) {
    const key = resolveApiKey(opts.apiKey);
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (μ-law @ 8 kHz native passthrough). */
  static override forTwilio(opts?: XaiCarrierOptions): TTS;
  // Parent-compatible overload — accepts the legacy positional form too.
  static override forTwilio(apiKey: string, options?: XaiCarrierOptions): TTS;
  static override forTwilio(
    arg1?: string | XaiCarrierOptions,
    arg2?: XaiCarrierOptions,
  ): TTS {
    const opts: XaiCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({
      ...opts,
      codec: XaiTTSCodec.MULAW,
      sampleRate: 8000,
    });
  }

  /** Pipeline TTS pre-configured for Telnyx (pcm @ 16 kHz). */
  static override forTelnyx(opts?: XaiCarrierOptions): TTS;
  static override forTelnyx(apiKey: string, options?: XaiCarrierOptions): TTS;
  static override forTelnyx(
    arg1?: string | XaiCarrierOptions,
    arg2?: XaiCarrierOptions,
  ): TTS {
    const opts: XaiCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({
      ...opts,
      codec: XaiTTSCodec.PCM,
      sampleRate: 16000,
    });
  }
}

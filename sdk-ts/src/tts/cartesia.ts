/** Cartesia TTS for Patter pipeline mode. */
import { CartesiaTTS as _CartesiaTTS } from "../providers/cartesia-tts";

export interface CartesiaTTSOptions {
  /** API key. Falls back to CARTESIA_API_KEY env var when omitted. */
  apiKey?: string;
  model?: string;
  voice?: string;
  language?: string;
  sampleRate?: number;
  speed?: string | number;
  emotion?: string | string[];
  volume?: number;
  baseUrl?: string;
  apiVersion?: string;
}

/** Options for the carrier-specific factories — same as the constructor minus `sampleRate`. */
export type CartesiaCarrierOptions = Omit<CartesiaTTSOptions, "sampleRate">;

function resolveApiKey(apiKey: string | undefined): string {
  const key = apiKey ?? process.env.CARTESIA_API_KEY;
  if (!key) {
    throw new Error(
      "Cartesia TTS requires an apiKey. Pass { apiKey: '...' } or " +
        "set CARTESIA_API_KEY in the environment.",
    );
  }
  return key;
}

/**
 * Cartesia TTS (sonic-3 GA, ~90 ms TTFB).
 *
 * The default model is `sonic-3` — Cartesia's current GA model. Voice IDs
 * from the previous `sonic-2` family (including the default Katie voice)
 * remain compatible.
 *
 * @example
 * ```ts
 * import * as cartesia from "getpatter/tts/cartesia";
 * const tts = new cartesia.TTS();              // reads CARTESIA_API_KEY
 * const tts = new cartesia.TTS({ apiKey: "..." });
 * ```
 *
 * **Telephony optimization** — use {@link TTS.forTwilio} (PCM @ 8 kHz,
 * skipping the SDK-side 16 kHz → 8 kHz resample before μ-law transcoding)
 * or {@link TTS.forTelnyx} (PCM @ 16 kHz, native Telnyx default) on
 * phone calls.
 */
export class TTS extends _CartesiaTTS {
  static readonly providerKey = "cartesia_tts";
  constructor(opts: CartesiaTTSOptions = {}) {
    const key = resolveApiKey(opts.apiKey);
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (PCM @ 8 kHz). */
  static override forTwilio(opts?: CartesiaCarrierOptions): TTS;
  // Parent-compatible overload — accepts the legacy positional form too.
  static override forTwilio(
    apiKey: string,
    options?: Omit<CartesiaTTSOptions, "sampleRate">,
  ): TTS;
  static override forTwilio(
    arg1?: string | CartesiaCarrierOptions,
    arg2?: Omit<CartesiaTTSOptions, "sampleRate">,
  ): TTS {
    const opts: CartesiaCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, sampleRate: 8000 });
  }

  /** Pipeline TTS pre-configured for Telnyx (PCM @ 16 kHz). */
  static override forTelnyx(opts?: CartesiaCarrierOptions): TTS;
  static override forTelnyx(
    apiKey: string,
    options?: Omit<CartesiaTTSOptions, "sampleRate">,
  ): TTS;
  static override forTelnyx(
    arg1?: string | CartesiaCarrierOptions,
    arg2?: Omit<CartesiaTTSOptions, "sampleRate">,
  ): TTS {
    const opts: CartesiaCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, sampleRate: 16000 });
  }
}

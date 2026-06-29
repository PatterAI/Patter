/** Cartesia TTS for Patter pipeline mode. */
import { CartesiaTTS as _CartesiaTTS, CartesiaTTSEncoding } from "../providers/cartesia-tts";

/** Constructor options for the Cartesia `TTS` adapter. */
export interface CartesiaTTSOptions {
  /** API key. Falls back to CARTESIA_API_KEY env var when omitted. */
  apiKey?: string;
  model?: string;
  voice?: string;
  language?: string;
  sampleRate?: number;
  /** Audio encoding. Default `pcm_s16le`; `pcm_mulaw` @ 8 kHz = carrier-native. */
  encoding?: CartesiaTTSEncoding;
  speed?: string | number;
  emotion?: string | string[];
  volume?: number;
  baseUrl?: string;
  apiVersion?: string;
}

/** Options for the carrier-specific factories — same as the constructor minus `sampleRate`/`encoding`. */
export type CartesiaCarrierOptions = Omit<CartesiaTTSOptions, "sampleRate" | "encoding">;

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
 * **Telephony** — use {@link TTS.forTwilio} or {@link TTS.forTelnyx} on phone
 * calls. Both emit μ-law @ 8 kHz natively (the carrier wire codec), so the
 * pipeline skips resampling and PCM → μ-law encoding entirely (bit-clean
 * passthrough).
 */
export class TTS extends _CartesiaTTS {
  static readonly providerKey = "cartesia_tts";
  constructor(opts: CartesiaTTSOptions = {}) {
    const key = resolveApiKey(opts.apiKey);
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (μ-law @ 8 kHz native — carrier-wire passthrough, no resample/encode). */
  static override forTwilio(opts?: CartesiaCarrierOptions): TTS;
  // Parent-compatible overload — accepts the legacy positional form too.
  static override forTwilio(
    apiKey: string,
    options?: CartesiaCarrierOptions,
  ): TTS;
  static override forTwilio(
    arg1?: string | CartesiaCarrierOptions,
    arg2?: CartesiaCarrierOptions,
  ): TTS {
    const opts: CartesiaCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, encoding: CartesiaTTSEncoding.PCM_MULAW, sampleRate: 8000 });
  }

  /** Pipeline TTS pre-configured for Telnyx (μ-law @ 8 kHz native — PCMU wire passthrough). */
  static override forTelnyx(opts?: CartesiaCarrierOptions): TTS;
  static override forTelnyx(
    apiKey: string,
    options?: CartesiaCarrierOptions,
  ): TTS;
  static override forTelnyx(
    arg1?: string | CartesiaCarrierOptions,
    arg2?: CartesiaCarrierOptions,
  ): TTS {
    const opts: CartesiaCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, encoding: CartesiaTTSEncoding.PCM_MULAW, sampleRate: 8000 });
  }
}

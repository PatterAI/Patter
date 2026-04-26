/** Cartesia streaming STT for Patter pipeline mode. */
import { CartesiaSTT as _CartesiaSTT, type CartesiaEncoding } from "../providers/cartesia-stt";

export interface CartesiaSTTOptions {
  /** API key. Falls back to CARTESIA_API_KEY env var when omitted. */
  apiKey?: string;
  model?: string;
  language?: string;
  encoding?: CartesiaEncoding;
  sampleRate?: number;
  baseUrl?: string;
}

/**
 * Cartesia streaming STT (ink-whisper).
 *
 * @example
 * ```ts
 * import * as cartesia from "getpatter/stt/cartesia";
 * const stt = new cartesia.STT();              // reads CARTESIA_API_KEY
 * const stt = new cartesia.STT({ apiKey: "..." });
 * ```
 */
export class STT extends _CartesiaSTT {
  static readonly providerKey = "cartesia_stt";
  constructor(opts: CartesiaSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.CARTESIA_API_KEY;
    if (!key) {
      throw new Error(
        "Cartesia STT requires an apiKey. Pass { apiKey: '...' } or " +
          "set CARTESIA_API_KEY in the environment.",
      );
    }
    super(key, {
      model: opts.model,
      language: opts.language,
      encoding: opts.encoding,
      sampleRate: opts.sampleRate,
      baseUrl: opts.baseUrl,
    });
  }
}

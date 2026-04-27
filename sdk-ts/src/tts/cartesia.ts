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
 */
export class TTS extends _CartesiaTTS {
  static readonly providerKey = "cartesia_tts";
  constructor(opts: CartesiaTTSOptions = {}) {
    const key = opts.apiKey ?? process.env.CARTESIA_API_KEY;
    if (!key) {
      throw new Error(
        "Cartesia TTS requires an apiKey. Pass { apiKey: '...' } or " +
          "set CARTESIA_API_KEY in the environment.",
      );
    }
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}

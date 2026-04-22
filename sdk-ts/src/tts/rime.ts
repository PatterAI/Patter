/** Rime TTS for Patter pipeline mode. */
import { RimeTTS as _RimeTTS } from "../providers/rime-tts";

export interface RimeTTSOptions {
  /** API key. Falls back to RIME_API_KEY env var when omitted. */
  apiKey?: string;
  model?: string;
  speaker?: string;
  lang?: string;
  sampleRate?: number;
  repetitionPenalty?: number;
  temperature?: number;
  topP?: number;
  maxTokens?: number;
  speedAlpha?: number;
  reduceLatency?: boolean;
  pauseBetweenBrackets?: boolean;
  phonemizeBetweenBrackets?: boolean;
  baseUrl?: string;
}

/**
 * Rime TTS (Arcana or Mist models).
 *
 * @example
 * ```ts
 * import * as rime from "getpatter/tts/rime";
 * const tts = new rime.TTS();              // reads RIME_API_KEY
 * const tts = new rime.TTS({ apiKey: "...", speaker: "astra" });
 * ```
 */
export class TTS extends _RimeTTS {
  constructor(opts: RimeTTSOptions = {}) {
    const key = opts.apiKey ?? process.env.RIME_API_KEY;
    if (!key) {
      throw new Error(
        "Rime TTS requires an apiKey. Pass { apiKey: '...' } or " +
          "set RIME_API_KEY in the environment.",
      );
    }
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}

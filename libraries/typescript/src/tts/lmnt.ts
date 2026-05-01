/** LMNT TTS for Patter pipeline mode. */
import {
  LMNTTTS as _LMNTTTS,
  type LMNTAudioFormat,
  type LMNTModel,
  type LMNTSampleRate,
} from "../providers/lmnt-tts";

export interface LMNTTTSOptions {
  /** API key. Falls back to LMNT_API_KEY env var when omitted. */
  apiKey?: string;
  model?: LMNTModel;
  voice?: string;
  language?: string;
  format?: LMNTAudioFormat;
  sampleRate?: LMNTSampleRate;
  temperature?: number;
  topP?: number;
  baseUrl?: string;
}

/**
 * LMNT TTS (blizzard/aurora).
 *
 * @example
 * ```ts
 * import * as lmnt from "getpatter/tts/lmnt";
 * const tts = new lmnt.TTS();              // reads LMNT_API_KEY
 * const tts = new lmnt.TTS({ apiKey: "...", voice: "leah" });
 * ```
 */
export class TTS extends _LMNTTTS {
  static readonly providerKey = "lmnt";
  constructor(opts: LMNTTTSOptions = {}) {
    const key = opts.apiKey ?? process.env.LMNT_API_KEY;
    if (!key) {
      throw new Error(
        "LMNT TTS requires an apiKey. Pass { apiKey: '...' } or " +
          "set LMNT_API_KEY in the environment.",
      );
    }
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}

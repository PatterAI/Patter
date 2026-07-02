/** Gemini TTS for Patter pipeline mode. */
import { GeminiTTS as _GeminiTTS } from "../providers/gemini-tts";

/** Constructor options for the Gemini `TTS` adapter. */
export interface GeminiTTSOptions {
  /** API key. Falls back to GEMINI_API_KEY / GOOGLE_API_KEY env var when omitted. */
  apiKey?: string;
  /** Prebuilt voice name (default `Kore`). */
  voice?: string;
  /** TTS model (default `gemini-3.1-flash-tts-preview`). */
  model?: string;
  /** Output PCM rate: 8000 or 16000 (default 16000). */
  targetSampleRate?: number;
}

/**
 * Gemini TTS (`gemini-3.1-flash-tts-preview`).
 *
 * @example
 * ```ts
 * import * as gemini from "getpatter/tts/gemini";
 * const tts = new gemini.TTS();                       // reads GEMINI_API_KEY
 * const tts = new gemini.TTS({ voice: "Kore" });
 * ```
 */
export class TTS extends _GeminiTTS {
  static readonly providerKey = "gemini_tts";
  constructor(opts: GeminiTTSOptions = {}) {
    const key = opts.apiKey ?? process.env.GEMINI_API_KEY ?? process.env.GOOGLE_API_KEY;
    if (!key) {
      throw new Error(
        "Gemini TTS requires an apiKey. Pass { apiKey: '...' } or set " +
          "GEMINI_API_KEY / GOOGLE_API_KEY in the environment.",
      );
    }
    super(key, opts.voice ?? "Kore", opts.model ?? "gemini-3.1-flash-tts-preview", opts.targetSampleRate ?? 16000);
  }
}

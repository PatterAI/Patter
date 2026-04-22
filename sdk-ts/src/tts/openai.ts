/** OpenAI TTS for Patter pipeline mode. */
import { OpenAITTS as _OpenAITTS } from "../providers/openai-tts";

export interface OpenAITTSOptions {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  voice?: string;
  model?: string;
}

/**
 * OpenAI TTS.
 *
 * @example
 * ```ts
 * import * as openai from "getpatter/tts/openai";
 * const tts = new openai.TTS();              // reads OPENAI_API_KEY
 * const tts = new openai.TTS({ apiKey: "sk-...", voice: "alloy" });
 * ```
 */
export class TTS extends _OpenAITTS {
  constructor(opts: OpenAITTSOptions = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "OpenAI TTS requires an apiKey. Pass { apiKey: 'sk-...' } or " +
          "set OPENAI_API_KEY in the environment.",
      );
    }
    super(key, opts.voice ?? "alloy", opts.model ?? "tts-1");
  }
}

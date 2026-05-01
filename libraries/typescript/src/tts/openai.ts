/** OpenAI TTS for Patter pipeline mode. */
import { OpenAITTS as _OpenAITTS } from "../providers/openai-tts";

/** Constructor options for the OpenAI `TTS` adapter. */
export interface OpenAITTSOptions {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  voice?: string;
  model?: string;
  /** Voice-direction prompt (only honoured for gpt-4o-mini-tts and newer). */
  instructions?: string;
  /** Speech speed multiplier, must be in [0.25, 4.0] when set. */
  speed?: number;
  /**
   * Enable anti-aliasing LPF ahead of the 3:2 decimation. Defaults to
   * ``false`` for backwards-compatibility; set to ``true`` for cleaner
   * audio on sibilants / fricatives.
   */
  antiAlias?: boolean;
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
  static readonly providerKey = "openai_tts";
  constructor(opts: OpenAITTSOptions = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "OpenAI TTS requires an apiKey. Pass { apiKey: 'sk-...' } or " +
          "set OPENAI_API_KEY in the environment.",
      );
    }
    super(
      key,
      opts.voice ?? "alloy",
      opts.model ?? "gpt-4o-mini-tts",
      opts.instructions ?? null,
      opts.speed ?? null,
      opts.antiAlias ?? false,
    );
  }
}

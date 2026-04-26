/** OpenAI Whisper STT for Patter pipeline mode. */
import { WhisperSTT as _WhisperSTT, type WhisperResponseFormat } from "../providers/whisper-stt";

export interface WhisperSTTOptions {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  model?: string;
  language?: string;
  bufferSize?: number;
  /** ``"verbose_json"`` exposes segment-level confidence / timestamps. */
  responseFormat?: WhisperResponseFormat;
}

/**
 * OpenAI Whisper STT.
 *
 * @example
 * ```ts
 * import * as whisper from "getpatter/stt/whisper";
 * const stt = new whisper.STT();              // reads OPENAI_API_KEY
 * const stt = new whisper.STT({ apiKey: "sk-...", language: "en" });
 * ```
 */
export class STT extends _WhisperSTT {
  static readonly providerKey = "whisper";
  constructor(opts: WhisperSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "Whisper STT requires an apiKey. Pass { apiKey: 'sk-...' } or " +
          "set OPENAI_API_KEY in the environment.",
      );
    }
    super(key, opts.model ?? "whisper-1", opts.language, opts.bufferSize, opts.responseFormat ?? "json");
  }
}

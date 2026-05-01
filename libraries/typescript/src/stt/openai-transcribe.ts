/** OpenAI GPT-4o Transcribe STT for Patter pipeline mode. */
import { OpenAITranscribeSTT as _OpenAITranscribeSTT } from "../providers/openai-transcribe-stt";
import type { WhisperResponseFormat } from "../providers/whisper-stt";

export interface OpenAITranscribeSTTOptions {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  /** ``gpt-4o-transcribe`` (default) or ``gpt-4o-mini-transcribe``. */
  model?: string;
  language?: string;
  bufferSize?: number;
  /** ``"verbose_json"`` exposes segment-level confidence / timestamps. */
  responseFormat?: WhisperResponseFormat;
}

/**
 * OpenAI GPT-4o Transcribe STT — ~10x faster than Whisper-1.
 *
 * Drop-in replacement for ``whisper.STT`` with stronger multilingual
 * quality and significantly lower latency.
 *
 * @example
 * ```ts
 * import * as openaiTranscribe from "getpatter/stt/openai-transcribe";
 * const stt = new openaiTranscribe.STT();              // reads OPENAI_API_KEY
 * const stt = new openaiTranscribe.STT({ apiKey: "sk-...", language: "en" });
 * ```
 */
export class STT extends _OpenAITranscribeSTT {
  static readonly providerKey = "openai_transcribe";
  constructor(opts: OpenAITranscribeSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "OpenAI Transcribe STT requires an apiKey. Pass { apiKey: 'sk-...' } or " +
          "set OPENAI_API_KEY in the environment.",
      );
    }
    super(key, opts.language, opts.model ?? "gpt-4o-transcribe", opts.bufferSize, opts.responseFormat ?? "json");
  }
}

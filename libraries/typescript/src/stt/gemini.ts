/** Gemini multimodal STT for Patter pipeline mode. */
import { GeminiSTT as _GeminiSTT } from "../providers/gemini-stt";

/** Constructor options for the Gemini `STT` adapter. */
export interface GeminiSTTOptions {
  /** API key. Falls back to GEMINI_API_KEY / GOOGLE_API_KEY env var when omitted. */
  apiKey?: string;
  /** Multimodal model (default `gemini-2.5-flash`). */
  model?: string;
  /** Inbound PCM rate from the pipeline (default 16000). */
  sampleRate?: number;
}

/**
 * Gemini multimodal STT (`gemini-2.5-flash`) — transcribes AND tags the
 * caller's vocal tone (`[tone: ...]`) so the agent can mirror mood.
 *
 * @example
 * ```ts
 * import * as gemini from "getpatter/stt/gemini";
 * const stt = new gemini.STT();              // reads GEMINI_API_KEY
 * ```
 */
export class STT extends _GeminiSTT {
  static readonly providerKey = "gemini_stt";
  constructor(opts: GeminiSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.GEMINI_API_KEY ?? process.env.GOOGLE_API_KEY;
    if (!key) {
      throw new Error(
        "Gemini STT requires an apiKey. Pass { apiKey: '...' } or set " +
          "GEMINI_API_KEY / GOOGLE_API_KEY in the environment.",
      );
    }
    super(key, opts.model ?? "gemini-2.5-flash", opts.sampleRate ?? 16000);
  }
}

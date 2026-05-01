/** Google Gemini LLM for Patter pipeline mode. */
import { GoogleLLMProvider as _GoogleLLM } from "../providers/google-llm";

/** Constructor options for the Google Gemini `LLM` adapter. */
export interface GoogleLLMOptions {
  /**
   * API key. Falls back to ``GEMINI_API_KEY`` first, then ``GOOGLE_API_KEY``.
   * (Google's CLI tooling uses ``GEMINI_API_KEY``; ``GOOGLE_API_KEY`` is the
   * legacy/alt name accepted for parity with other SDKs.)
   */
  apiKey?: string;
  /** Model id (e.g. ``"gemini-2.5-flash"``). */
  model?: string;
  /** Override the Generative Language API base URL (rarely needed). */
  baseUrl?: string;
  /** Sampling temperature. */
  temperature?: number;
  /** Maximum output tokens. */
  maxOutputTokens?: number;
}

/**
 * Google Gemini LLM provider (Developer API, streaming SSE).
 *
 * @example
 * ```ts
 * import * as google from "getpatter/llm/google";
 * const llm = new google.LLM();                                 // reads GEMINI_API_KEY or GOOGLE_API_KEY
 * const llm = new google.LLM({ apiKey: "AIza...", model: "gemini-2.5-flash" });
 * ```
 */
export class LLM extends _GoogleLLM {
  static readonly providerKey = "google";
  constructor(opts: GoogleLLMOptions = {}) {
    const key =
      opts.apiKey ?? process.env.GEMINI_API_KEY ?? process.env.GOOGLE_API_KEY;
    if (!key) {
      throw new Error(
        "Google LLM requires an apiKey. Pass { apiKey: 'AIza...' } or set " +
          "GEMINI_API_KEY (or GOOGLE_API_KEY).",
      );
    }
    super({
      apiKey: key,
      model: opts.model,
      baseUrl: opts.baseUrl,
      temperature: opts.temperature,
      maxOutputTokens: opts.maxOutputTokens,
    });
  }
}

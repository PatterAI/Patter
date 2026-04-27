/** Groq LLM for Patter pipeline mode. */
import { GroqLLMProvider as _GroqLLM } from "../providers/groq-llm";

export interface GroqLLMOptions {
  /** API key. Falls back to GROQ_API_KEY env var when omitted. */
  apiKey?: string;
  /** Model id (e.g. ``"llama-3.3-70b-versatile"``). */
  model?: string;
  /** Override the OpenAI-compatible base URL (rarely needed). */
  baseUrl?: string;
  /** Sampling temperature [0, 2]. */
  temperature?: number;
  /** Max tokens in the assistant response. */
  maxTokens?: number;
}

/**
 * Groq LLM provider (OpenAI-compatible Chat Completions, streaming).
 *
 * @example
 * ```ts
 * import * as groq from "getpatter/llm/groq";
 * const llm = new groq.LLM();                                // reads GROQ_API_KEY
 * const llm = new groq.LLM({ apiKey: "gsk_...", model: "llama-3.3-70b-versatile" });
 * ```
 */
export class LLM extends _GroqLLM {
  static readonly providerKey = "groq";
  constructor(opts: GroqLLMOptions = {}) {
    const key = opts.apiKey ?? process.env.GROQ_API_KEY;
    if (!key) {
      throw new Error(
        "Groq LLM requires an apiKey. Pass { apiKey: 'gsk_...' } or set GROQ_API_KEY.",
      );
    }
    super({
      apiKey: key,
      model: opts.model,
      baseUrl: opts.baseUrl,
      temperature: opts.temperature,
      maxTokens: opts.maxTokens,
    });
  }
}

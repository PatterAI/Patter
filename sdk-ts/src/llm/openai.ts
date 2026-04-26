/** OpenAI LLM for Patter pipeline mode. */
import { OpenAILLMProvider as _OpenAILLM } from "../llm-loop";

export interface OpenAILLMOptions {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  /** Chat Completions model id. Defaults to ``"gpt-4o-mini"``. */
  model?: string;
}

/**
 * OpenAI Chat Completions LLM provider.
 *
 * @example
 * ```ts
 * import * as openai from "getpatter/llm/openai";
 * const llm = new openai.LLM();                           // reads OPENAI_API_KEY
 * const llm = new openai.LLM({ apiKey: "sk-...", model: "gpt-4o-mini" });
 * ```
 */
export class LLM extends _OpenAILLM {
  static readonly providerKey = "openai";
  constructor(opts: OpenAILLMOptions = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "OpenAI LLM requires an apiKey. Pass { apiKey: 'sk-...' } or set OPENAI_API_KEY.",
      );
    }
    super(key, opts.model ?? "gpt-4o-mini");
  }
}

/** Cerebras LLM for Patter pipeline mode. */
import { CerebrasLLMProvider as _CerebrasLLM } from "../providers/cerebras-llm";

export interface CerebrasLLMOptions {
  /** API key. Falls back to CEREBRAS_API_KEY env var when omitted. */
  apiKey?: string;
  /** Model id (e.g. ``"llama-3.3-70b"``). */
  model?: string;
  /** Override the OpenAI-compatible base URL (rarely needed). */
  baseUrl?: string;
  /** Gzip request payloads for faster TTFT on large prompts. */
  gzipCompression?: boolean;
  /** Sampling temperature [0, 2]. */
  temperature?: number;
  /** Max tokens in the assistant response. */
  maxTokens?: number;
}

/**
 * Cerebras LLM provider (OpenAI-compatible Inference API, streaming).
 *
 * @example
 * ```ts
 * import * as cerebras from "getpatter/llm/cerebras";
 * const llm = new cerebras.LLM();                              // reads CEREBRAS_API_KEY
 * const llm = new cerebras.LLM({ apiKey: "csk-...", model: "llama-3.3-70b" });
 * ```
 */
export class LLM extends _CerebrasLLM {
  static readonly providerKey = "cerebras";
  constructor(opts: CerebrasLLMOptions = {}) {
    const key = opts.apiKey ?? process.env.CEREBRAS_API_KEY;
    if (!key) {
      throw new Error(
        "Cerebras LLM requires an apiKey. Pass { apiKey: 'csk-...' } or set CEREBRAS_API_KEY.",
      );
    }
    super({
      apiKey: key,
      model: opts.model,
      baseUrl: opts.baseUrl,
      gzipCompression: opts.gzipCompression,
      temperature: opts.temperature,
      maxTokens: opts.maxTokens,
    });
  }
}

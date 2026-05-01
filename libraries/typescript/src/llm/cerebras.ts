/** Cerebras LLM for Patter pipeline mode. */
import { CerebrasLLMProvider as _CerebrasLLM } from "../providers/cerebras-llm";

export interface CerebrasLLMOptions {
  /** API key. Falls back to CEREBRAS_API_KEY env var when omitted. */
  apiKey?: string;
  /** Model id (e.g. ``"gpt-oss-120b"``). */
  model?: string;
  /** Override the OpenAI-compatible base URL (rarely needed). */
  baseUrl?: string;
  /** Gzip request payloads for faster TTFT on large prompts. */
  gzipCompression?: boolean;
  /** Sampling temperature [0, 2]. */
  temperature?: number;
  /** Max tokens in the assistant response (sent as ``max_completion_tokens``). */
  maxTokens?: number;
  /** OpenAI-style ``response_format`` for JSON mode / structured outputs. */
  responseFormat?: Record<string, unknown>;
  /** Whether to allow parallel tool calls. */
  parallelToolCalls?: boolean;
  /** ``"auto" | "none" | "required"`` or a specific tool object. */
  toolChoice?: string | Record<string, unknown>;
  /** Sampling seed for reproducible outputs. */
  seed?: number;
  /** Nucleus sampling cutoff in [0, 1]. */
  topP?: number;
  /** Penalty in [-2, 2] applied to repeated tokens. */
  frequencyPenalty?: number;
  /** Penalty in [-2, 2] applied to seen tokens. */
  presencePenalty?: number;
  /** Stop sequence(s). */
  stop?: string | string[];
}

/**
 * Cerebras LLM provider (OpenAI-compatible Inference API, streaming).
 *
 * @example
 * ```ts
 * import * as cerebras from "getpatter/llm/cerebras";
 * const llm = new cerebras.LLM();                              // reads CEREBRAS_API_KEY
 * const llm = new cerebras.LLM({ apiKey: "csk-...", model: "gpt-oss-120b" });
 * // smaller-context alternative:
 * const llm = new cerebras.LLM({ apiKey: "csk-...", model: "llama3.1-8b" });
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
      responseFormat: opts.responseFormat,
      parallelToolCalls: opts.parallelToolCalls,
      toolChoice: opts.toolChoice,
      seed: opts.seed,
      topP: opts.topP,
      frequencyPenalty: opts.frequencyPenalty,
      presencePenalty: opts.presencePenalty,
      stop: opts.stop,
    });
  }
}

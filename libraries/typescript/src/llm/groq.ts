/** Groq LLM for Patter pipeline mode. */
import { GroqLLMProvider as _GroqLLM } from "../providers/groq-llm";

/** Constructor options for the Groq `LLM` adapter. */
export interface GroqLLMOptions {
  /** API key. Falls back to GROQ_API_KEY env var when omitted. */
  apiKey?: string;
  /** Model id (e.g. ``"llama-3.3-70b-versatile"``). */
  model?: string;
  /** Override the OpenAI-compatible base URL (rarely needed). */
  baseUrl?: string;
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
  /** Sampling seed. */
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

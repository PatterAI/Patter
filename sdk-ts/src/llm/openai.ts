/** OpenAI LLM for Patter pipeline mode. */
import { OpenAILLMProvider as _OpenAILLM } from "../llm-loop";

export interface OpenAILLMOptions {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  /** Chat Completions model id. Defaults to ``"gpt-4o-mini"``. */
  model?: string;
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
 * OpenAI Chat Completions LLM provider.
 *
 * @example
 * ```ts
 * import * as openai from "getpatter/llm/openai";
 * const llm = new openai.LLM();                           // reads OPENAI_API_KEY
 * const llm = new openai.LLM({ apiKey: "sk-...", model: "gpt-4o-mini", temperature: 0.4 });
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
    super(key, opts.model ?? "gpt-4o-mini", {
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

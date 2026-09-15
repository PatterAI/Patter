/** Atlas Cloud LLM for Patter pipeline mode. */
import { AtlasCloudLLMProvider as _AtlasCloudLLM } from "../providers/atlascloud-llm";

/** Constructor options for the Atlas Cloud `LLM` adapter. */
export interface AtlasCloudLLMOptions {
  /** API key. Falls back to ATLASCLOUD_API_KEY env var when omitted. */
  apiKey?: string;
  /** Model id (e.g. ``"openai/gpt-4.1-mini"``). */
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
 * Atlas Cloud LLM provider (OpenAI-compatible Chat Completions, streaming).
 *
 * @example
 * ```ts
 * import * as atlascloud from "getpatter/llm/atlascloud";
 * const llm = new atlascloud.LLM();                          // reads ATLASCLOUD_API_KEY
 * const llm = new atlascloud.LLM({ apiKey: "apikey-...", model: "deepseek-ai/deepseek-v3.2" });
 * ```
 */
export class LLM extends _AtlasCloudLLM {
  static readonly providerKey = "atlascloud";
  constructor(opts: AtlasCloudLLMOptions = {}) {
    const key = opts.apiKey ?? process.env.ATLASCLOUD_API_KEY;
    if (!key) {
      throw new Error(
        "Atlas Cloud LLM requires an apiKey. Pass { apiKey: 'apikey-...' } or set ATLASCLOUD_API_KEY.",
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

/** Anthropic Claude LLM for Patter pipeline mode. */
import { AnthropicLLMProvider as _AnthropicLLM } from "../providers/anthropic-llm";

export interface AnthropicLLMOptions {
  /** API key. Falls back to ANTHROPIC_API_KEY env var when omitted. */
  apiKey?: string;
  /** Anthropic Messages API model id (e.g. ``"claude-haiku-4-5-20251001"``). */
  model?: string;
  /** Maximum number of tokens to sample. Defaults to the adapter default. */
  maxTokens?: number;
  /** Sampling temperature. */
  temperature?: number;
  /** Override the Messages API base URL (rarely needed). */
  baseUrl?: string;
  /** ``anthropic-version`` header override. */
  anthropicVersion?: string;
  /**
   * Enable Anthropic prompt caching (default: ``true``). For voice
   * agents with long instruction-dense system prompts, the cache saves
   * ~100-400 ms TTFT and ~90% input-token cost per cached turn. Disable
   * if your system prompt + tools are below Anthropic's minimum
   * cacheable size (~1024 tokens for Sonnet/Opus, ~2048 for Haiku) —
   * caching has no effect below that threshold.
   */
  promptCaching?: boolean;
}

/**
 * Anthropic Claude LLM provider (Messages API, streaming).
 *
 * Prompt caching is **enabled by default**. The first request writes
 * the cache; subsequent requests within ~5 minutes hit it. Pass
 * ``{ promptCaching: false }`` to opt out.
 *
 * @example
 * ```ts
 * import * as anthropic from "getpatter/llm/anthropic";
 * const llm = new anthropic.LLM();                                   // reads ANTHROPIC_API_KEY
 * const llm = new anthropic.LLM({ apiKey: "sk-ant-...", model: "claude-haiku-4-5-20251001" });
 * const llm = new anthropic.LLM({ promptCaching: false });           // opt out of caching
 * ```
 */
export class LLM extends _AnthropicLLM {
  static readonly providerKey = "anthropic";
  constructor(opts: AnthropicLLMOptions = {}) {
    const key = opts.apiKey ?? process.env.ANTHROPIC_API_KEY;
    if (!key) {
      throw new Error(
        "Anthropic LLM requires an apiKey. Pass { apiKey: 'sk-ant-...' } or set ANTHROPIC_API_KEY.",
      );
    }
    super({
      apiKey: key,
      model: opts.model,
      maxTokens: opts.maxTokens,
      temperature: opts.temperature,
      baseUrl: opts.baseUrl,
      anthropicVersion: opts.anthropicVersion,
      promptCaching: opts.promptCaching,
    });
  }
}

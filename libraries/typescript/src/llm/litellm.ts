/**
 * LiteLLM LLM provider for Patter's pipeline mode.
 *
 * Thin preset over {@link OpenAICompatibleLLMProvider} that points at a
 * LiteLLM proxy endpoint. LiteLLM exposes an OpenAI-compatible
 * ``/chat/completions`` API that routes to 100+ providers (OpenAI,
 * Anthropic, Google, Azure, Bedrock, Ollama, and more).
 *
 * The proxy must be running and reachable at ``baseUrl`` (default:
 * ``http://localhost:4000/v1`` or ``LITELLM_BASE_URL`` env var).
 */
import {
  OpenAICompatibleLLMProvider,
  type OpenAICompatibleLLMOptions,
} from './openai-compatible';

/** Default LiteLLM proxy base URL. */
const DEFAULT_BASE_URL = 'http://localhost:4000/v1';
/** Env var for the LiteLLM proxy base URL. */
const BASE_URL_ENV = 'LITELLM_BASE_URL';
/** Env var for the LiteLLM proxy API key (master/virtual key). */
const API_KEY_ENV = 'LITELLM_API_KEY';

/** Constructor options for the LiteLLM ``LLM`` preset. */
export interface LiteLLMLLMOptions {
  /** API key for the LiteLLM proxy. Falls back to ``LITELLM_API_KEY`` env var. */
  apiKey?: string;
  /** LiteLLM proxy base URL. Falls back to ``LITELLM_BASE_URL`` env, then ``http://localhost:4000/v1``. */
  baseUrl?: string;
  /** Model identifier routed by the LiteLLM proxy (e.g. ``"gpt-4o"``, ``"anthropic/claude-sonnet-4-6"``). */
  model?: string;
  /** Per-request timeout in seconds. Default ``60``. */
  timeout?: number;
  /** Extra headers merged after the SDK ``User-Agent``. */
  extraHeaders?: Record<string, string>;
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
 * LiteLLM proxy LLM provider (OpenAI-compatible, streaming).
 *
 * @example
 * ```ts
 * import * as litellm from "getpatter/llm/litellm";
 * const llm = new litellm.LLM({ model: "gpt-4o" });
 * const llm = new litellm.LLM({ model: "anthropic/claude-sonnet-4-6" });
 * ```
 */
export class LLM extends OpenAICompatibleLLMProvider {
  static readonly providerKey = 'litellm';

  constructor(opts: LiteLLMLLMOptions = {}) {
    const baseUrl =
      opts.baseUrl ?? process.env[BASE_URL_ENV] ?? DEFAULT_BASE_URL;
    const model = opts.model ?? 'gpt-4o-mini';
    const options: OpenAICompatibleLLMOptions = {
      apiKey: opts.apiKey,
      apiKeyEnv: API_KEY_ENV,
      baseUrl,
      model,
      timeout: opts.timeout,
      extraHeaders: opts.extraHeaders,
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
    };
    super(options);
  }
}

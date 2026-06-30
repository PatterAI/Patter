/**
 * Inworld Realtime Router LLM for Patter pipeline mode.
 *
 * Inworld Router exposes an OpenAI-compatible `/chat/completions` endpoint at
 * `https://api.inworld.ai/v1` that routes to 220+ models from OpenAI,
 * Anthropic, Google, Mistral, DeepSeek, Meta and more behind one key — see
 * https://inworld.ai/models and
 * https://docs.inworld.ai/router/openai-compatibility. Thin preset over
 * {@link OpenAICompatibleLLMProvider}: defaults the base URL and reads
 * `INWORLD_API_KEY`. The model string is the Inworld target — a configured
 * router (`"inworld/<router-id>"`) or a request-level `"<provider>/<model>"`
 * slug from the catalogue.
 */
import {
  OpenAICompatibleLLMProvider,
  type OpenAICompatibleLLMOptions,
} from "./openai-compatible";

/** OpenAI-compatible Router endpoint (base URL only — vs api.openai.com). */
export const INWORLD_ROUTER_BASE_URL = "https://api.inworld.ai/v1";

/** Constructor options for the Inworld Router `LLM` (base URL pre-filled). */
export interface InworldLLMOptions extends Omit<OpenAICompatibleLLMOptions, "baseUrl"> {
  /** Override the Router base URL (rarely needed). */
  baseUrl?: string;
}

/**
 * Inworld Realtime Router LLM (OpenAI-compatible, streaming).
 *
 * @example
 * ```ts
 * import * as inworld from "getpatter/llm/inworld";
 * const llm = new inworld.LLM({ model: "openai/gpt-4o-mini" });   // reads INWORLD_API_KEY
 * const llm = new inworld.LLM({ apiKey: "...", model: "anthropic/claude-haiku-4-5-20251001" });
 * ```
 */
export class LLM extends OpenAICompatibleLLMProvider {
  static readonly providerKey = "inworld_router";
  constructor(opts: InworldLLMOptions) {
    const key = opts.apiKey ?? process.env.INWORLD_API_KEY;
    if (!key) {
      throw new Error(
        "Inworld Router LLM requires an apiKey. Pass { apiKey: '...' } or set INWORLD_API_KEY.",
      );
    }
    super({ ...opts, apiKey: key, baseUrl: opts.baseUrl ?? INWORLD_ROUTER_BASE_URL });
  }
}

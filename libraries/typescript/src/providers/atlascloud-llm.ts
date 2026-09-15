/**
 * Atlas Cloud LLM provider for Patter's pipeline mode.
 *
 * Atlas Cloud exposes an OpenAI-compatible Chat Completions API. We reuse the
 * streaming code path by implementing the same SSE parser as
 * ``OpenAILLMProvider`` but pointed at ``api.atlascloud.ai``. Defaults to
 * ``openai/gpt-4.1-mini``.
 */

import type { LLMChunk, LLMProvider, LLMStreamOptions } from "../llm-loop";
import {
  mergeAbortSignals,
  createStreamIdleWatchdog,
  LLM_STREAM_IDLE_TIMEOUT_MS,
} from "../llm-loop";
import { parseOpenAISseStream } from './groq-llm';
import { getLogger } from '../logger';
import { VERSION } from '../version';
import { PatterConnectionError } from '../errors';

const ATLASCLOUD_BASE_URL = 'https://api.atlascloud.ai/v1';

/** Known Atlas Cloud Chat Completions models. Availability depends on account tier. */
export const AtlasCloudModel = {
  GPT_4_1_MINI: 'openai/gpt-4.1-mini',
  GPT_5_4_MINI: 'openai/gpt-5.4-mini',
  DEEPSEEK_V3_2: 'deepseek-ai/deepseek-v3.2',
  QWEN_3_235B_INSTRUCT: 'Qwen/Qwen3-235B-A22B-Instruct-2507',
  QWEN_3_5_35B_A3B: 'qwen/qwen3.5-35b-a3b',
  GLM_4_6: 'zai-org/GLM-4.6',
  KIMI_K2_5: 'moonshotai/kimi-k2.5',
  MINIMAX_M2_5: 'minimaxai/minimax-m2.5',
} as const;
/** Union of {@link AtlasCloudModel} string values. */
export type AtlasCloudModel = (typeof AtlasCloudModel)[keyof typeof AtlasCloudModel];

const DEFAULT_MODEL: string = AtlasCloudModel.GPT_4_1_MINI;

/** Constructor options for {@link AtlasCloudLLMProvider}. */
export interface AtlasCloudLLMOptions {
  apiKey: string;
  model?: string;
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

/** LLM provider backed by Atlas Cloud's OpenAI-compatible Chat Completions API. */
export class AtlasCloudLLMProvider implements LLMProvider {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'atlascloud';
  private readonly apiKey: string;
  readonly model: string;
  private readonly baseUrl: string;
  private readonly temperature?: number;
  private readonly maxTokens?: number;
  private readonly responseFormat?: Record<string, unknown>;
  private readonly parallelToolCalls?: boolean;
  private readonly toolChoice?: string | Record<string, unknown>;
  private readonly seed?: number;
  private readonly topP?: number;
  private readonly frequencyPenalty?: number;
  private readonly presencePenalty?: number;
  private readonly stop?: string | string[];

  constructor(options: AtlasCloudLLMOptions) {
    if (!options.apiKey) {
      throw new Error(
        'Atlas Cloud API key is required. Pass it via { apiKey } or read ATLASCLOUD_API_KEY from the environment.',
      );
    }
    this.apiKey = options.apiKey;
    this.model = options.model ?? DEFAULT_MODEL;
    this.baseUrl = options.baseUrl ?? ATLASCLOUD_BASE_URL;
    this.temperature = options.temperature;
    this.maxTokens = options.maxTokens;
    this.responseFormat = options.responseFormat;
    this.parallelToolCalls = options.parallelToolCalls;
    this.toolChoice = options.toolChoice;
    this.seed = options.seed;
    this.topP = options.topP;
    this.frequencyPenalty = options.frequencyPenalty;
    this.presencePenalty = options.presencePenalty;
    this.stop = options.stop;
  }

  /**
   * Pre-call DNS / TLS warmup for the Atlas Cloud inference endpoint.
   * Best-effort: 5 s timeout, all exceptions swallowed at debug level.
   */
  async warmup(): Promise<void> {
    try {
      await fetch(`${this.baseUrl}/models`, {
        method: 'GET',
        headers: { Authorization: `Bearer ${this.apiKey}` },
        signal: AbortSignal.timeout(5_000),
      });
    } catch (err) {
      getLogger().debug(`Atlas Cloud LLM warmup failed (best-effort): ${String(err)}`);
    }
  }

  /** Stream Patter-format LLM chunks from the Atlas Cloud chat completions API. */
  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
    opts?: LLMStreamOptions,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    const body: Record<string, unknown> = {
      model: this.model,
      messages,
      stream: true,
      stream_options: { include_usage: true },
    };
    if (this.temperature !== undefined) body.temperature = this.temperature;
    if (this.maxTokens !== undefined) {
      // Atlas Cloud accepts both, but ``max_completion_tokens`` is the modern OpenAI
      // spec name and matches Cerebras/OpenAI parity.
      body.max_completion_tokens = this.maxTokens;
    }
    if (this.responseFormat !== undefined) body.response_format = this.responseFormat;
    if (this.parallelToolCalls !== undefined) body.parallel_tool_calls = this.parallelToolCalls;
    if (this.toolChoice !== undefined) body.tool_choice = this.toolChoice;
    if (this.seed !== undefined) body.seed = this.seed;
    if (this.topP !== undefined) body.top_p = this.topP;
    if (this.frequencyPenalty !== undefined) body.frequency_penalty = this.frequencyPenalty;
    if (this.presencePenalty !== undefined) body.presence_penalty = this.presencePenalty;
    if (this.stop !== undefined) body.stop = this.stop;
    if (tools) body.tools = tools;

    // Idle watchdog (re-armed per chunk) instead of a whole-stream ceiling —
    // see llm-loop.ts createStreamIdleWatchdog.
    const idle = createStreamIdleWatchdog();
    const response = await fetch(`${this.baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.apiKey}`,
        'User-Agent': `getpatter/${VERSION}`,
      },
      body: JSON.stringify(body),
      signal: mergeAbortSignals(opts?.signal, idle.signal),
    });

    if (!response.ok) {
      const errText = await response.text();
      // Cap the logged/thrown body — provider 401 bodies have been observed to
      // embed the rejected API-key prefix, which would otherwise land in logs.
      getLogger().error(`Atlas Cloud API error: ${response.status} ${errText.slice(0, 200)}`);
      // Throw (don't return silently) so the LLM fallback chain fails over and
      // the spoken error fallback can fire — a silent return looks like success.
      throw new PatterConnectionError(
        `Atlas Cloud API returned ${response.status}: ${errText.slice(0, 200)}`,
      );
    }

    try {
      yield* parseOpenAISseStream(response, idle.touch);
    } catch (err) {
      if (idle.fired && !opts?.signal?.aborted) {
        throw new PatterConnectionError(
          `Atlas Cloud stream idle timeout — no data for ${LLM_STREAM_IDLE_TIMEOUT_MS / 1000}s`,
        );
      }
      throw err;
    } finally {
      idle.clear();
    }
  }
}

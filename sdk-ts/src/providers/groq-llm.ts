/**
 * Groq LLM provider for Patter's pipeline mode.
 *
 * Groq exposes an OpenAI-compatible Chat Completions API. We reuse the
 * streaming code path by implementing the same SSE parser as
 * ``OpenAILLMProvider`` but pointed at ``api.groq.com``.
 *
 * Portions adapted from LiveKit Agents
 * (https://github.com/livekit/agents, commit
 * 78a66bcf79c5cea82989401c408f1dff4b961a5b,
 * file livekit-plugins/livekit-plugins-groq/livekit/plugins/groq/services.py),
 * licensed under Apache License 2.0. Copyright LiveKit, Inc.
 *
 * Adaptations from the LiveKit source:
 *   * Ported the Python ``groq.LLM`` subclass (which subclasses the
 *     LiveKit OpenAI plugin) into a tiny TypeScript wrapper that swaps
 *     the base URL and defaults to ``llama-3.3-70b-versatile``.
 */

import type { LLMChunk, LLMProvider } from '../llm-loop';
import { getLogger } from '../logger';

const GROQ_BASE_URL = 'https://api.groq.com/openai/v1';
const DEFAULT_MODEL = 'llama-3.3-70b-versatile';

export interface GroqLLMOptions {
  apiKey: string;
  model?: string;
  baseUrl?: string;
}

/** LLM provider backed by Groq's OpenAI-compatible Chat Completions API. */
export class GroqLLMProvider implements LLMProvider {
  private readonly apiKey: string;
  readonly model: string;
  private readonly baseUrl: string;

  constructor(options: GroqLLMOptions) {
    if (!options.apiKey) {
      throw new Error(
        'Groq API key is required. Pass it via { apiKey } or read GROQ_API_KEY from the environment.',
      );
    }
    this.apiKey = options.apiKey;
    this.model = options.model ?? DEFAULT_MODEL;
    this.baseUrl = options.baseUrl ?? GROQ_BASE_URL;
  }

  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    const body: Record<string, unknown> = {
      model: this.model,
      messages,
      stream: true,
      stream_options: { include_usage: true },
    };
    if (tools) body.tools = tools;

    const response = await fetch(`${this.baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const errText = await response.text();
      getLogger().error(`Groq API error: ${response.status} ${errText}`);
      return;
    }

    yield* parseOpenAISseStream(response);
  }
}

// ---------------------------------------------------------------------------
// Shared OpenAI-format SSE stream parser
// ---------------------------------------------------------------------------

/**
 * Parse a streaming OpenAI-format Chat Completions response and yield
 * Patter ``LLMChunk`` objects.
 *
 * Exported so ``cerebras-llm.ts`` can reuse the same parser.
 */
export async function* parseOpenAISseStream(
  response: Response,
): AsyncGenerator<LLMChunk, void, unknown> {
  const reader = response.body?.getReader();
  if (!reader) return;

  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || !trimmed.startsWith('data: ')) continue;
      const data = trimmed.slice(6);
      if (data === '[DONE]') continue;

      let chunk: {
        choices?: Array<{
          delta?: {
            content?: string;
            tool_calls?: Array<{
              index: number;
              id?: string;
              function?: { name?: string; arguments?: string };
            }>;
          };
        }>;
        usage?: {
          prompt_tokens?: number;
          completion_tokens?: number;
          prompt_tokens_details?: { cached_tokens?: number };
        };
        // Some Groq deployments return ``x_groq.usage`` in the final chunk.
        x_groq?: {
          usage?: {
            prompt_tokens?: number;
            completion_tokens?: number;
          };
        };
      };
      try {
        chunk = JSON.parse(data);
      } catch {
        continue;
      }

      // Final chunk with usage (choices=[]). Forward for cost attribution.
      const usage = chunk.usage ?? chunk.x_groq?.usage;
      if (usage) {
        const cached = chunk.usage?.prompt_tokens_details?.cached_tokens ?? 0;
        yield {
          type: 'usage',
          inputTokens: usage.prompt_tokens,
          outputTokens: usage.completion_tokens,
          cacheReadInputTokens: cached,
        };
      }

      const delta = chunk.choices?.[0]?.delta;
      if (!delta) continue;

      if (delta.content) {
        yield { type: 'text', content: delta.content };
      }

      if (delta.tool_calls) {
        for (const tc of delta.tool_calls) {
          yield {
            type: 'tool_call',
            index: tc.index,
            id: tc.id,
            name: tc.function?.name,
            arguments: tc.function?.arguments,
          };
        }
      }
    }
  }
}

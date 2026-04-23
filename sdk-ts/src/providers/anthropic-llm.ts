/**
 * Anthropic Claude LLM provider for Patter's pipeline mode.
 *
 * Implements the ``LLMProvider`` interface from ``../llm-loop`` on top
 * of Anthropic's Messages API with streaming via Server-Sent Events.
 * OpenAI-style ``messages`` / ``tools`` inputs are translated into the
 * Anthropic shape and the vendor event stream is normalised back into
 * Patter's ``{ type: 'text' | 'tool_call' | 'done' }`` chunk protocol.
 *
 * Portions adapted from LiveKit Agents
 * (https://github.com/livekit/agents, commit
 * 78a66bcf79c5cea82989401c408f1dff4b961a5b,
 * file livekit-plugins/livekit-plugins-anthropic/livekit/plugins/anthropic/llm.py),
 * licensed under Apache License 2.0. Copyright 2023 LiveKit, Inc.
 *
 * Adaptations from the LiveKit source:
 *   * Ported the Python async class pair (``llm.LLM`` /
 *     ``llm.LLMStream``) into a single TypeScript class that satisfies
 *     Patter's ``LLMProvider`` interface.
 *   * Uses native ``fetch`` + SSE parsing instead of the official
 *     ``@anthropic-ai/sdk`` to keep Patter's runtime dependencies lean
 *     (mirrors how ``OpenAILLMProvider`` is implemented in
 *     ``llm-loop.ts``).
 *   * Maps Anthropic event types (``content_block_start``,
 *     ``content_block_delta``, ``content_block_stop``) to the Patter
 *     chunk protocol.
 */

import type { LLMChunk, LLMProvider } from '../llm-loop';
import { getLogger } from '../logger';

const DEFAULT_ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages';
const DEFAULT_ANTHROPIC_VERSION = '2023-06-01';
const DEFAULT_MODEL = 'claude-haiku-4-5-20251001';
const DEFAULT_MAX_TOKENS = 1024;

export interface AnthropicLLMOptions {
  apiKey: string;
  model?: string;
  maxTokens?: number;
  temperature?: number;
  baseUrl?: string;
  anthropicVersion?: string;
}

interface OpenAIToolDef {
  type?: string;
  function?: {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
  };
  name?: string;
  description?: string;
  parameters?: Record<string, unknown>;
}

interface AnthropicTool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

interface AnthropicMessage {
  role: 'user' | 'assistant';
  content: string | Array<Record<string, unknown>>;
}

/** LLM provider backed by Anthropic's Messages API (streaming). */
export class AnthropicLLMProvider implements LLMProvider {
  private readonly apiKey: string;
  private readonly model: string;
  private readonly maxTokens: number;
  private readonly temperature?: number;
  private readonly url: string;
  private readonly anthropicVersion: string;

  constructor(options: AnthropicLLMOptions) {
    if (!options.apiKey) {
      throw new Error(
        'Anthropic API key is required. Pass it via { apiKey } or set the ' +
          'ANTHROPIC_API_KEY environment variable before constructing the provider.',
      );
    }
    this.apiKey = options.apiKey;
    this.model = options.model ?? DEFAULT_MODEL;
    this.maxTokens = options.maxTokens ?? DEFAULT_MAX_TOKENS;
    this.temperature = options.temperature;
    this.url = options.baseUrl ?? DEFAULT_ANTHROPIC_URL;
    this.anthropicVersion = options.anthropicVersion ?? DEFAULT_ANTHROPIC_VERSION;
  }

  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    const { system, messages: anthropicMessages } = toAnthropicMessages(messages);
    const anthropicTools = tools ? toAnthropicTools(tools as OpenAIToolDef[]) : null;

    const body: Record<string, unknown> = {
      model: this.model,
      messages: anthropicMessages,
      max_tokens: this.maxTokens,
      stream: true,
    };
    if (system) body.system = system;
    if (anthropicTools && anthropicTools.length > 0) body.tools = anthropicTools;
    if (this.temperature !== undefined) body.temperature = this.temperature;

    const response = await fetch(this.url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': this.apiKey,
        'anthropic-version': this.anthropicVersion,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const errText = await response.text();
      getLogger().error(`Anthropic API error: ${response.status} ${errText}`);
      return;
    }

    const reader = response.body?.getReader();
    if (!reader) return;

    const decoder = new TextDecoder();
    let buffer = '';

    const toolIndexByBlock = new Map<number, number>();
    const toolIdByBlock = new Map<number, string>();
    let nextIndex = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data: ')) continue;
        const data = trimmed.slice(6);
        if (!data || data === '[DONE]') continue;

        let event: {
          type?: string;
          index?: number;
          content_block?: { type?: string; id?: string; name?: string };
          delta?: { type?: string; text?: string; partial_json?: string };
        };
        try {
          event = JSON.parse(data);
        } catch {
          continue;
        }

        if (event.type === 'content_block_start' && event.content_block?.type === 'tool_use') {
          const blockIdx = event.index ?? 0;
          const toolId = event.content_block.id ?? '';
          const toolName = event.content_block.name ?? '';
          const patterIndex = nextIndex++;
          toolIndexByBlock.set(blockIdx, patterIndex);
          toolIdByBlock.set(blockIdx, toolId);
          yield {
            type: 'tool_call',
            index: patterIndex,
            id: toolId,
            name: toolName,
            arguments: '',
          };
          continue;
        }

        if (event.type === 'content_block_delta') {
          if (event.delta?.type === 'text_delta' && event.delta.text) {
            yield { type: 'text', content: event.delta.text };
            continue;
          }
          if (event.delta?.type === 'input_json_delta' && event.delta.partial_json) {
            const blockIdx = event.index ?? 0;
            const patterIndex = toolIndexByBlock.get(blockIdx);
            if (patterIndex !== undefined) {
              yield {
                type: 'tool_call',
                index: patterIndex,
                id: toolIdByBlock.get(blockIdx),
                arguments: event.delta.partial_json,
              };
            }
          }
        }
      }
    }

    yield { type: 'done' };
  }
}

// ---------------------------------------------------------------------------
// Translation helpers (OpenAI format -> Anthropic Messages API)
// ---------------------------------------------------------------------------

function toAnthropicTools(tools: OpenAIToolDef[]): AnthropicTool[] {
  return tools.map((t) => {
    const fn = t.function ?? t;
    return {
      name: String(fn.name ?? ''),
      description: String(fn.description ?? ''),
      input_schema:
        (fn.parameters as Record<string, unknown>) ?? { type: 'object', properties: {} },
    };
  });
}

interface OpenAIStyleMessage {
  role?: string;
  content?: string | Array<Record<string, unknown>>;
  tool_calls?: Array<{
    id?: string;
    function?: { name?: string; arguments?: string };
  }>;
  tool_call_id?: string;
  name?: string;
}

export function toAnthropicMessages(
  messages: Array<Record<string, unknown>>,
): { system: string; messages: AnthropicMessage[] } {
  const systemParts: string[] = [];
  const out: AnthropicMessage[] = [];

  for (const rawMsg of messages as OpenAIStyleMessage[]) {
    const role = rawMsg.role;

    if (role === 'system') {
      if (typeof rawMsg.content === 'string' && rawMsg.content) {
        systemParts.push(rawMsg.content);
      }
      continue;
    }

    if (role === 'user') {
      if (typeof rawMsg.content === 'string') {
        out.push({ role: 'user', content: rawMsg.content });
      } else if (rawMsg.content) {
        out.push({ role: 'user', content: rawMsg.content });
      }
      continue;
    }

    if (role === 'assistant') {
      const blocks: Array<Record<string, unknown>> = [];
      if (typeof rawMsg.content === 'string' && rawMsg.content) {
        blocks.push({ type: 'text', text: rawMsg.content });
      }
      for (const tc of rawMsg.tool_calls ?? []) {
        let args: unknown = {};
        try {
          args = JSON.parse(tc.function?.arguments ?? '{}');
        } catch {
          args = {};
        }
        blocks.push({
          type: 'tool_use',
          id: tc.id ?? '',
          name: tc.function?.name ?? '',
          input: args,
        });
      }
      if (blocks.length > 0) {
        out.push({ role: 'assistant', content: blocks });
      }
      continue;
    }

    if (role === 'tool') {
      const contentStr =
        typeof rawMsg.content === 'string' ? rawMsg.content : JSON.stringify(rawMsg.content);
      out.push({
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: rawMsg.tool_call_id ?? '',
            content: contentStr,
          },
        ],
      });
      continue;
    }
  }

  return { system: systemParts.join('\n\n'), messages: out };
}

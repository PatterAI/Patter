/**
 * Built-in LLM loop for pipeline mode when no onMessage handler is provided.
 *
 * Uses a pluggable ``LLMProvider`` interface so callers can supply OpenAI,
 * Anthropic, Gemini, or any custom provider.  The default provider is
 * ``OpenAILLMProvider`` which preserves full backward compatibility.
 */

import type { ToolDefinition } from './types';
import { getLogger } from './logger';

// ---------------------------------------------------------------------------
// Provider interface
// ---------------------------------------------------------------------------

/** A single streaming chunk yielded by an LLM provider. */
export interface LLMChunk {
  type: 'text' | 'tool_call' | 'done';
  content?: string;
  index?: number;
  id?: string;
  name?: string;
  arguments?: string;
}

/**
 * Interface that any LLM provider must satisfy.
 *
 * Implementors yield streaming ``LLMChunk`` objects:
 * - ``{ type: "text", content: "..." }`` — a text token.
 * - ``{ type: "tool_call", index, id?, name?, arguments? }`` — a (partial) tool
 *   invocation.  Chunks with the same ``index`` are concatenated.
 * - ``{ type: "done" }`` — signals the end of the stream (optional).
 */
export interface LLMProvider {
  stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
  ): AsyncGenerator<LLMChunk, void, unknown>;
}

// ---------------------------------------------------------------------------
// Built-in OpenAI provider
// ---------------------------------------------------------------------------

/** LLM provider backed by OpenAI Chat Completions (streaming). */
export class OpenAILLMProvider implements LLMProvider {
  private readonly apiKey: string;
  private readonly model: string;

  constructor(apiKey: string, model: string) {
    this.apiKey = apiKey;
    this.model = model;
  }

  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    const body: Record<string, unknown> = {
      model: this.model,
      messages,
      stream: true,
    };
    if (tools) {
      body.tools = tools;
    }

    const response = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${this.apiKey}`,
      },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      const errText = await response.text();
      getLogger().error(`LLM API error: ${response.status} ${errText}`);
      return;
    }

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
        };
        try {
          chunk = JSON.parse(data);
        } catch {
          continue;
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
}

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

interface OpenAIMessage {
  role: string;
  content?: string | null;
  tool_calls?: Array<{
    id: string;
    type: string;
    function: { name: string; arguments: string };
  }>;
  tool_call_id?: string;
  [key: string]: unknown;
}

interface ToolCallAccumulator {
  id: string;
  name: string;
  arguments: string;
}

// ---------------------------------------------------------------------------
// LLM loop
// ---------------------------------------------------------------------------

export class LLMLoop {
  private readonly provider: LLMProvider;
  private readonly systemPrompt: string;
  private readonly tools: ToolDefinition[] | null;
  private readonly openaiTools: Array<{
    type: string;
    function: { name: string; description: string; parameters: Record<string, unknown> };
  }> | null;
  private readonly toolMap: Map<string, ToolDefinition>;

  constructor(
    apiKey: string,
    model: string,
    systemPrompt: string,
    tools?: ToolDefinition[] | null,
    llmProvider?: LLMProvider,
  ) {
    this.provider = llmProvider ?? new OpenAILLMProvider(apiKey, model);
    this.systemPrompt = systemPrompt;
    this.tools = tools ?? null;

    this.toolMap = new Map();
    this.openaiTools = null;

    if (this.tools && this.tools.length > 0) {
      this.openaiTools = [];
      for (const t of this.tools) {
        this.openaiTools.push({
          type: 'function',
          function: {
            name: t.name,
            description: t.description || '',
            parameters: t.parameters || { type: 'object', properties: {} },
          },
        });
        this.toolMap.set(t.name, t);
      }
    }
  }

  /**
   * Stream LLM response tokens, handling tool calls automatically.
   * Yields text tokens as they arrive from the LLM.
   */
  async *run(
    userText: string,
    history: Array<{ role: string; text: string }>,
    callContext: Record<string, unknown>,
  ): AsyncGenerator<string, void, unknown> {
    const messages = this.buildMessages(history, userText);
    const maxIterations = 10;

    for (let iter = 0; iter < maxIterations; iter++) {
      const toolCallsAccumulated = new Map<number, ToolCallAccumulator>();
      const textParts: string[] = [];
      let hasToolCalls = false;

      for await (const chunk of this.provider.stream(messages, this.openaiTools)) {
        if (chunk.type === 'text' && chunk.content) {
          textParts.push(chunk.content);
          yield chunk.content;
        } else if (chunk.type === 'tool_call') {
          hasToolCalls = true;
          const idx = chunk.index ?? 0;
          if (!toolCallsAccumulated.has(idx)) {
            toolCallsAccumulated.set(idx, { id: '', name: '', arguments: '' });
          }
          const acc = toolCallsAccumulated.get(idx)!;
          if (chunk.id) acc.id = chunk.id;
          if (chunk.name) acc.name = chunk.name;
          if (chunk.arguments) acc.arguments += chunk.arguments;
        }
      }

      if (!hasToolCalls) return;

      // Execute tool calls and add results to messages
      const assistantMsg: OpenAIMessage = {
        role: 'assistant',
        content: textParts.join('') || null,
        tool_calls: [],
      };

      const sortedIndices = [...toolCallsAccumulated.keys()].sort((a, b) => a - b);
      for (const idx of sortedIndices) {
        const tc = toolCallsAccumulated.get(idx)!;
        assistantMsg.tool_calls!.push({
          id: tc.id,
          type: 'function',
          function: { name: tc.name, arguments: tc.arguments },
        });
      }
      messages.push(assistantMsg);

      for (const tcData of assistantMsg.tool_calls!) {
        const toolName = tcData.function.name;
        let args: Record<string, unknown>;
        try {
          args = JSON.parse(tcData.function.arguments);
        } catch {
          args = {};
        }

        const result = await this.executeTool(toolName, args, callContext);
        messages.push({
          role: 'tool',
          tool_call_id: tcData.id,
          content: result,
        });
      }
    }

    getLogger().warn(`LLM loop hit max iterations (${maxIterations})`);
  }

  private async executeTool(
    toolName: string,
    args: Record<string, unknown>,
    callContext: Record<string, unknown>,
  ): Promise<string> {
    const toolDef = this.toolMap.get(toolName);
    if (!toolDef) {
      return JSON.stringify({ error: `Unknown tool: ${toolName}` });
    }

    // Prefer local handler
    if (toolDef.handler) {
      try {
        return await toolDef.handler(args, callContext);
      } catch (e) {
        return JSON.stringify({ error: `Tool handler error: ${String(e)}` });
      }
    }

    // Fall back to webhook
    if (toolDef.webhookUrl) {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const resp = await fetch(toolDef.webhookUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              tool: toolName,
              arguments: args,
              ...callContext,
              attempt: attempt + 1,
            }),
            signal: AbortSignal.timeout(10_000),
          });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const result = JSON.stringify(await resp.json());
          const MAX_RESPONSE_BYTES = 1 * 1024 * 1024;
          if (result.length > MAX_RESPONSE_BYTES) {
            return JSON.stringify({ error: `Webhook response too large: ${result.length} bytes (max ${MAX_RESPONSE_BYTES})`, fallback: true });
          }
          return result;
        } catch (e) {
          if (attempt < 2) {
            await new Promise<void>((r) => setTimeout(r, 500));
          } else {
            return JSON.stringify({ error: `Tool failed after 3 attempts: ${String(e)}` });
          }
        }
      }
    }

    return JSON.stringify({ error: `No handler or webhookUrl for tool '${toolName}'` });
  }

  private buildMessages(
    history: Array<{ role: string; text: string }>,
    userText: string,
  ): OpenAIMessage[] {
    const messages: OpenAIMessage[] = [
      { role: 'system', content: this.systemPrompt },
    ];

    for (const entry of history) {
      messages.push({
        role: entry.role === 'assistant' ? 'assistant' : 'user',
        content: entry.text,
      });
    }

    messages.push({ role: 'user', content: userText });
    return messages;
  }
}

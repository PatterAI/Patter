/**
 * Built-in LLM loop for pipeline mode when no onMessage handler is provided.
 *
 * Uses a pluggable ``LLMProvider`` interface so callers can supply OpenAI,
 * Anthropic, Gemini, or any custom provider.  The default provider is
 * ``OpenAILLMProvider`` which preserves full backward compatibility.
 */

import type { ToolDefinition } from './types';
import { getLogger } from './logger';
import { validateWebhookUrl } from './server';
import { SPAN_TOOL, withSpan } from './observability/tracing';

// ---------------------------------------------------------------------------
// Tool execution — pluggable policy
// ---------------------------------------------------------------------------

/**
 * Minimal interface for recording LLM usage chunks.
 * Avoids a circular import from metrics.ts.
 */
export interface LlmUsageRecorder {
  recordLlmUsage(
    provider: string,
    model: string,
    inputTokens: number,
    outputTokens: number,
    cacheReadTokens?: number,
    cacheCreationTokens?: number,
  ): void;
}

const DEFAULT_TOOL_MAX_RETRIES = 2;
const DEFAULT_TOOL_RETRY_DELAY_MS = 500;
const DEFAULT_TOOL_TIMEOUT_MS = 10_000;
const TOOL_MAX_RESPONSE_BYTES = 1 * 1024 * 1024;

/**
 * Pluggable tool executor — mirrors the Python ``ToolExecutor`` in
 * ``sdk-py/getpatter/services/tool_executor.py``.
 *
 * Implementors receive a fully-resolved ``ToolDefinition`` (handler +/ webhook
 * URL already validated by the SDK) and MUST return a JSON-stringifiable
 * result. Errors should be returned as JSON like
 * ``{ error: "...", fallback: true }`` rather than thrown.
 */
export interface ToolExecutor {
  execute(
    toolDef: ToolDefinition,
    args: Record<string, unknown>,
    callContext: Record<string, unknown>,
  ): Promise<string>;
}

export interface DefaultToolExecutorOptions {
  /** Total attempts = maxRetries + 1. Default: 2 (i.e. 3 attempts). */
  maxRetries?: number;
  /** Delay between attempts, in ms. */
  retryDelayMs?: number;
  /** Per-request timeout for webhook calls, in ms. */
  requestTimeoutMs?: number;
}

/**
 * Default executor — webhook with retry/fallback and local handler preference.
 *
 * This is the out-of-the-box behavior and is 1:1 equivalent to the previous
 * inline logic in ``LLMLoop.executeTool``.
 */
export class DefaultToolExecutor implements ToolExecutor {
  private readonly maxRetries: number;
  private readonly retryDelayMs: number;
  private readonly requestTimeoutMs: number;

  constructor(opts: DefaultToolExecutorOptions = {}) {
    this.maxRetries = opts.maxRetries ?? DEFAULT_TOOL_MAX_RETRIES;
    this.retryDelayMs = opts.retryDelayMs ?? DEFAULT_TOOL_RETRY_DELAY_MS;
    this.requestTimeoutMs = opts.requestTimeoutMs ?? DEFAULT_TOOL_TIMEOUT_MS;
  }

  async execute(
    toolDef: ToolDefinition,
    args: Record<string, unknown>,
    callContext: Record<string, unknown>,
  ): Promise<string> {
    // Prefer local handler.
    if (toolDef.handler) {
      try {
        return await toolDef.handler(args, callContext);
      } catch (e) {
        return JSON.stringify({
          error: `Tool handler error: ${String(e)}`,
          fallback: true,
        });
      }
    }

    // Fall back to webhook with retry/backoff.
    if (toolDef.webhookUrl) {
      try {
        validateWebhookUrl(toolDef.webhookUrl);
      } catch (e) {
        return JSON.stringify({ error: `Tool webhook URL rejected: ${String(e)}` });
      }
      const callId = typeof callContext.call_id === 'string' ? callContext.call_id : '';
      return await withSpan(
        SPAN_TOOL,
        {
          'patter.tool.name': toolDef.name,
          'patter.tool.transport': 'webhook',
          'patter.call.id': callId,
        },
        async (span) => {
          const totalAttempts = this.maxRetries + 1;
          for (let attempt = 0; attempt < totalAttempts; attempt++) {
            span.setAttribute('patter.tool.attempt', attempt + 1);
            try {
              const resp = await fetch(toolDef.webhookUrl!, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  tool: toolDef.name,
                  arguments: args,
                  ...callContext,
                  attempt: attempt + 1,
                }),
                signal: AbortSignal.timeout(this.requestTimeoutMs),
              });
              if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
              const result = JSON.stringify(await resp.json());
              if (result.length > TOOL_MAX_RESPONSE_BYTES) {
                return JSON.stringify({
                  error: `Webhook response too large: ${result.length} bytes (max ${TOOL_MAX_RESPONSE_BYTES})`,
                  fallback: true,
                });
              }
              return result;
            } catch (e) {
              if (attempt < totalAttempts - 1) {
                getLogger().warn(
                  `Tool webhook '${toolDef.name}' failed (attempt ${attempt + 1}), retrying: ${String(e)}`,
                );
                await new Promise<void>((r) => setTimeout(r, this.retryDelayMs));
              } else {
                span.recordException(e);
                return JSON.stringify({
                  error: `Tool failed after ${totalAttempts} attempts: ${String(e)}`,
                  fallback: true,
                });
              }
            }
          }
          // Unreachable — the for-loop always returns.
          return JSON.stringify({
            error: `Tool '${toolDef.name}' exited retry loop unexpectedly`,
            fallback: true,
          });
        },
      );
    }

    return JSON.stringify({
      error: `No handler or webhookUrl for tool '${toolDef.name}'`,
      fallback: true,
    });
  }
}

// ---------------------------------------------------------------------------
// Provider interface
// ---------------------------------------------------------------------------

/** A single streaming chunk yielded by an LLM provider. */
export interface LLMChunk {
  type: 'text' | 'tool_call' | 'done' | 'usage';
  content?: string;
  index?: number;
  id?: string;
  name?: string;
  arguments?: string;
  // Fix 10: usage chunk fields (emitted by providers that expose token counts)
  inputTokens?: number;
  outputTokens?: number;
  cacheReadInputTokens?: number;
  cacheCreationInputTokens?: number;
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
  readonly model: string;

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
      // Ask OpenAI to include a final usage chunk so we can attribute token
      // cost. Without this the dashboard shows LLM cost = 0 for OpenAI.
      stream_options: { include_usage: true },
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
      signal: AbortSignal.timeout(30_000),
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
          usage?: {
            prompt_tokens?: number;
            completion_tokens?: number;
            prompt_tokens_details?: { cached_tokens?: number };
          };
        };
        try {
          chunk = JSON.parse(data);
        } catch {
          continue;
        }

        // Final usage chunk arrives with choices=[] when stream_options
        // include_usage is set. Forward it for cost attribution.
        if (chunk.usage) {
          const cached = chunk.usage.prompt_tokens_details?.cached_tokens ?? 0;
          // OpenAI's prompt_tokens is the TOTAL input including cached tokens.
          // Subtract cached so inputTokens represents only the uncached portion
          // and calculateLlmCost doesn't bill cached tokens at the full rate.
          const uncachedInput = Math.max(0, (chunk.usage.prompt_tokens ?? 0) - cached);
          yield {
            type: 'usage',
            inputTokens: uncachedInput,
            outputTokens: chunk.usage.completion_tokens,
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
  private toolExecutor: ToolExecutor;
  // Fix 10: track provider/model so usage chunks can be attributed for billing.
  private readonly _providerName: string;
  private readonly _modelName: string;

  constructor(
    apiKey: string,
    model: string,
    systemPrompt: string,
    tools?: ToolDefinition[] | null,
    llmProvider?: LLMProvider,
  ) {
    this.provider = llmProvider ?? new OpenAILLMProvider(apiKey, model);
    this.systemPrompt = systemPrompt;
    // Derive a billing-friendly provider name. Prefer the static
    // ``providerKey`` (stable, matches pricing keys); fall back to the
    // class-name stripping heuristic for custom providers without it.
    if (llmProvider) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const key = (llmProvider.constructor as any)?.providerKey;
      if (key) {
        this._providerName = key;
      } else {
        const stripped = (llmProvider.constructor?.name ?? 'custom')
          .replace(/LLMProvider$/i, '')
          .replace(/LLM$/i, '')
          .replace(/Provider$/i, '')
          .toLowerCase();
        this._providerName = stripped || 'custom';
      }
    } else {
      this._providerName = 'openai';
    }
    this._modelName = model;
    this.tools = tools ?? null;
    this.toolExecutor = new DefaultToolExecutor();

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
   * Swap in a custom tool executor (e.g. different retry policy, metrics
   * wrapping, tenant-aware fan-out). The default is ``DefaultToolExecutor``.
   */
  setToolExecutor(executor: ToolExecutor): void {
    this.toolExecutor = executor;
  }

  /**
   * Stream LLM response tokens, handling tool calls automatically.
   * Yields text tokens as they arrive from the LLM.
   *
   * @param metrics Optional usage recorder — when provided, usage chunks
   *   from the provider are forwarded to {@link LlmUsageRecorder.recordLlmUsage}
   *   so token costs are included in the call cost breakdown (fix 10).
   */
  async *run(
    userText: string,
    history: Array<{ role: string; text: string }>,
    callContext: Record<string, unknown>,
    metrics?: LlmUsageRecorder,
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
        } else if (chunk.type === 'usage') {
          // Fix 10: forward token usage to the metrics accumulator for billing.
          metrics?.recordLlmUsage(
            this._providerName,
            this._modelName,
            chunk.inputTokens ?? 0,
            chunk.outputTokens ?? 0,
            chunk.cacheReadInputTokens ?? 0,
            chunk.cacheCreationInputTokens ?? 0,
          );
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
    return this.toolExecutor.execute(toolDef, args, callContext);
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

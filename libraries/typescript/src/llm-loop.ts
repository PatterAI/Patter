/**
 * Built-in LLM loop for pipeline mode when no onMessage handler is provided.
 *
 * Uses a pluggable ``LLMProvider`` interface so callers can supply OpenAI,
 * Anthropic, Gemini, or any custom provider.  The default provider is
 * ``OpenAILLMProvider`` which preserves full backward compatibility.
 */

import type { ToolDefinition, HookContext } from './types';
import type { PipelineHookExecutor } from './pipeline-hooks';
import type { EventBus } from './observability/event-bus';
import { getLogger } from './logger';
import { validateWebhookUrl } from './server';
import { SPAN_TOOL, withSpan } from './observability/tracing';
import { PatterConnectionError } from './errors';

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

/** Constructor options for `DefaultToolExecutor`. */
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

/** Optional sampling kwargs forwarded into the OpenAI Chat Completions body. */
export interface OpenAILLMSamplingOptions {
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

/** LLM provider backed by OpenAI Chat Completions (streaming). */
export class OpenAILLMProvider implements LLMProvider {
  private readonly apiKey: string;
  readonly model: string;
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

  constructor(apiKey: string, model: string, sampling: OpenAILLMSamplingOptions = {}) {
    this.apiKey = apiKey;
    this.model = model;
    this.temperature = sampling.temperature;
    this.maxTokens = sampling.maxTokens;
    this.responseFormat = sampling.responseFormat;
    this.parallelToolCalls = sampling.parallelToolCalls;
    this.toolChoice = sampling.toolChoice;
    this.seed = sampling.seed;
    this.topP = sampling.topP;
    this.frequencyPenalty = sampling.frequencyPenalty;
    this.presencePenalty = sampling.presencePenalty;
    this.stop = sampling.stop;
  }

  /** Stream OpenAI Chat Completions chunks for the given messages/tools. */
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
    if (this.temperature !== undefined) body.temperature = this.temperature;
    if (this.maxTokens !== undefined) {
      // Current OpenAI spec uses ``max_completion_tokens``; ``max_tokens``
      // is now legacy. Mirrors Cerebras/Groq parity.
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
      throw new PatterConnectionError(
        `LLM API returned ${response.status}: ${errText.slice(0, 200)}`,
      );
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

/** Default phone-friendly preamble prepended to user system prompts unless `disablePhonePreamble` is set. */
export const DEFAULT_PHONE_PREAMBLE =
  'You are speaking on a live phone call. Respond concisely. ' +
  'Do not use markdown, headers, bullet lists, code fences, or emojis. ' +
  'Spell out numbers, currencies, dates, and units in natural spoken language. ' +
  'Keep replies under 2 sentences unless the caller asks for detail.';


/** Pipeline-mode LLM driver: runs the chat loop, dispatches tool calls, and emits text deltas. */
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
  private eventBus?: EventBus;
  // Fix 10: track provider/model so usage chunks can be attributed for billing.
  private readonly _providerName: string;
  private readonly _modelName: string;

  constructor(
    apiKey: string,
    model: string,
    systemPrompt: string,
    tools?: ToolDefinition[] | null,
    llmProvider?: LLMProvider,
    disablePhonePreamble: boolean = false,
  ) {
    this.provider = llmProvider ?? new OpenAILLMProvider(apiKey, model);
    if (disablePhonePreamble) {
      this.systemPrompt = systemPrompt;
    } else {
      this.systemPrompt = systemPrompt
        ? `${DEFAULT_PHONE_PREAMBLE}\n\n${systemPrompt}`
        : DEFAULT_PHONE_PREAMBLE;
    }
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
   * Wire an :class:`EventBus` so the loop emits ``llm_chunk`` per text
   * token and ``tool_call_started`` the first time each tool-call index
   * appears. Set to ``undefined`` to disable.
   */
  setEventBus(bus: EventBus | undefined): void {
    this.eventBus = bus;
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
    hookExecutor?: PipelineHookExecutor,
    hookCtx?: HookContext,
  ): AsyncGenerator<string, void, unknown> {
    let messages = this.buildMessages(history, userText);
    const maxIterations = 10;
    // Run before_llm once on the initial messages list. Subsequent
    // tool-call iterations re-submit augmented messages and skip the
    // hook (running on every iteration would let a poorly written hook
    // trigger an infinite re-write loop).
    if (hookExecutor && hookCtx) {
      // Hooks return ``Record<string, unknown>[]``; the loop tracks them
      // as ``OpenAIMessage[]`` since callers may push tool-call entries
      // with the stricter shape. The runtime fields are identical.
      messages = (await hookExecutor.runBeforeLlm(
        messages as Array<Record<string, unknown>>,
        hookCtx,
      )) as OpenAIMessage[];
    }
    // Tier 3 (`onResponse`) — and the deprecated legacy callable that maps
    // to it — buffer streaming tokens, run the hook against the final
    // assistant text, and yield the (possibly rewritten) text as a single
    // chunk. Tier 1 (`onChunk`) and tier 2 (`onSentence`) keep streaming.
    // Tier 1 transform is applied inline below; tier 2 runs in the
    // sentence chunker / stream-handler downstream.
    const hasAfterLlmResponse = Boolean(hookExecutor?.hasAfterLlmResponse() && hookCtx);
    const hasAfterLlmChunk = Boolean(hookExecutor?.hasAfterLlmChunk());
    const allEmittedText: string[] = [];

    for (let iter = 0; iter < maxIterations; iter++) {
      const toolCallsAccumulated = new Map<number, ToolCallAccumulator>();
      const textParts: string[] = [];
      let hasToolCalls = false;

      for await (const chunk of this.provider.stream(messages, this.openaiTools)) {
        if (chunk.type === 'text' && chunk.content) {
          // Tier 1 — per-token sync transform. Cheap, no buffering.
          const content = hasAfterLlmChunk && hookExecutor
            ? hookExecutor.runAfterLlmChunk(chunk.content)
            : chunk.content;
          textParts.push(content);
          this.eventBus?.emit('llm_chunk', { text: content, iteration: iter });
          if (hasAfterLlmResponse) {
            allEmittedText.push(content);
          } else {
            yield content;
          }
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
            // Emit tool_call_started the first time we see a given index.
            this.eventBus?.emit('tool_call_started', {
              index: idx,
              name: chunk.name ?? '',
              args: chunk.arguments ?? '',
            });
          }
          const acc = toolCallsAccumulated.get(idx)!;
          if (chunk.id) acc.id = chunk.id;
          if (chunk.name) acc.name = chunk.name;
          if (chunk.arguments) acc.arguments += chunk.arguments;
        }
      }

      if (!hasToolCalls) {
        if (hasAfterLlmResponse && hookExecutor && hookCtx) {
          const finalText = allEmittedText.join('');
          const rewritten = await hookExecutor.runAfterLlmResponse(finalText, hookCtx);
          if (rewritten) yield rewritten;
        }
        return;
      }

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

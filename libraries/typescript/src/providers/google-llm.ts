/**
 * Google Gemini LLM provider for Patter's pipeline mode.
 *
 * Implements the ``LLMProvider`` interface against the Gemini Developer
 * API's streaming endpoint (``:streamGenerateContent?alt=sse``).
 * OpenAI-style messages/tools are translated into Gemini's ``contents``
 * and ``tools`` shapes, and streamed response parts are normalised to
 * Patter's ``{ type: 'text' | 'tool_call' | 'done' }`` chunks.
 *
 * Implementation notes:
 *   * Uses native ``fetch`` against the REST SSE endpoint so we don't
 *     pull in a large SDK dependency.
 *   * Single class that satisfies Patter's ``LLMProvider`` interface.
 *   * Vertex AI support (which requires GCP auth) is not included — only
 *     the Developer API (API key) path is supported. Vertex can be added
 *     by a follow-up PR once credential plumbing is in place.
 */

import type { LLMChunk, LLMProvider, LLMStreamOptions } from "../llm-loop";
import {
  mergeAbortSignals,
  createStreamIdleWatchdog,
  LLM_STREAM_IDLE_TIMEOUT_MS,
} from "../llm-loop";
import { getLogger } from '../logger';
import { PatterConnectionError } from '../errors';

/** Known Google Gemini chat models. */
export const GoogleModel = {
  GEMINI_2_5_FLASH: 'gemini-2.5-flash',
  GEMINI_2_5_PRO: 'gemini-2.5-pro',
  GEMINI_2_0_FLASH: 'gemini-2.0-flash',
  GEMINI_2_0_FLASH_LITE: 'gemini-2.0-flash-lite',
  GEMINI_1_5_FLASH: 'gemini-1.5-flash',
  GEMINI_1_5_PRO: 'gemini-1.5-pro',
} as const;
/** Union of {@link GoogleModel} string values. */
export type GoogleModel = (typeof GoogleModel)[keyof typeof GoogleModel];

const DEFAULT_MODEL: string = GoogleModel.GEMINI_2_5_FLASH;
const DEFAULT_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta';

/** Constructor options for {@link GoogleLLMProvider}. */
export interface GoogleLLMOptions {
  apiKey: string;
  model?: string;
  baseUrl?: string;
  temperature?: number;
  maxOutputTokens?: number;
}

interface GeminiPart {
  text?: string;
  functionCall?: { name?: string; args?: Record<string, unknown>; id?: string };
  functionResponse?: {
    name?: string;
    response?: Record<string, unknown>;
    id?: string;
  };
}

interface GeminiContent {
  role: 'user' | 'model';
  parts: GeminiPart[];
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

/** LLM provider backed by Google Gemini (Developer API, streaming SSE). */
export class GoogleLLMProvider implements LLMProvider {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'google';
  private readonly apiKey: string;
  readonly model: string;
  private readonly baseUrl: string;
  private readonly temperature?: number;
  private readonly maxOutputTokens?: number;

  constructor(options: GoogleLLMOptions) {
    if (!options.apiKey) {
      throw new Error(
        'Google API key is required. Pass it via { apiKey } or read GOOGLE_API_KEY from the environment.',
      );
    }
    this.apiKey = options.apiKey;
    this.model = options.model ?? DEFAULT_MODEL;
    this.baseUrl = options.baseUrl ?? DEFAULT_BASE_URL;
    this.temperature = options.temperature;
    this.maxOutputTokens = options.maxOutputTokens;
  }

  /**
   * Pre-call DNS / TLS warmup for the Gemini API.
   * Issues a lightweight ``GET ${baseUrl}/models?key=...`` so DNS, TLS
   * and HTTP/2 are already up by the time the first
   * ``streamGenerateContent`` call lands. Best-effort: 5 s timeout, all
   * exceptions swallowed at debug level.
   */
  async warmup(): Promise<void> {
    try {
      await fetch(`${this.baseUrl}/models?key=${encodeURIComponent(this.apiKey)}`, {
        method: 'GET',
        signal: AbortSignal.timeout(5_000),
      });
    } catch (err) {
      getLogger().debug(`Google LLM warmup failed (best-effort): ${String(err)}`);
    }
  }

  /** Stream Patter-format LLM chunks from the Gemini SSE endpoint. */
  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
    opts?: LLMStreamOptions,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    const { systemInstruction, contents } = toGeminiContents(messages);
    const geminiTools = tools ? toGeminiTools(tools as OpenAIToolDef[]) : null;

    const body: Record<string, unknown> = { contents };
    if (systemInstruction) {
      body.systemInstruction = { role: 'system', parts: [{ text: systemInstruction }] };
    }
    if (geminiTools) body.tools = geminiTools;

    const generationConfig: Record<string, unknown> = {};
    if (this.temperature !== undefined) generationConfig.temperature = this.temperature;
    if (this.maxOutputTokens !== undefined)
      generationConfig.maxOutputTokens = this.maxOutputTokens;
    if (Object.keys(generationConfig).length > 0) body.generationConfig = generationConfig;

    const url =
      `${this.baseUrl}/models/${encodeURIComponent(this.model)}:streamGenerateContent?alt=sse` +
      `&key=${encodeURIComponent(this.apiKey)}`;

    // Idle watchdog (re-armed per chunk) instead of a whole-stream ceiling —
    // see llm-loop.ts createStreamIdleWatchdog.
    const idle = createStreamIdleWatchdog();
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: mergeAbortSignals(opts?.signal, idle.signal),
    });

    if (!response.ok) {
      const errText = await response.text();
      // Cap the logged/thrown body — provider 401 bodies have been observed to
      // embed the rejected API-key prefix, which would otherwise land in logs.
      getLogger().error(`Gemini API error: ${response.status} ${errText.slice(0, 200)}`);
      // Throw (don't return silently) so the LLM fallback chain fails over and
      // the spoken error fallback can fire — a silent return looks like success.
      throw new PatterConnectionError(
        `Gemini API returned ${response.status}: ${errText.slice(0, 200)}`,
      );
    }

    const reader = response.body?.getReader();
    if (!reader) return;

    const decoder = new TextDecoder();
    let buffer = '';
    let nextIndex = 0;
    let lastUsage:
      | { promptTokenCount?: number; candidatesTokenCount?: number; cachedContentTokenCount?: number }
      | undefined;

    try {
      while (true) {
        const { done, value } = await reader.read();
        idle.touch();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data: ')) continue;
          const data = trimmed.slice(6);
          if (!data) continue;

          let payload: {
            candidates?: Array<{
              content?: { parts?: GeminiPart[] };
            }>;
            usageMetadata?: {
              promptTokenCount?: number;
              candidatesTokenCount?: number;
              cachedContentTokenCount?: number;
            };
          };
          try {
            payload = JSON.parse(data);
          } catch {
            continue;
          }

          // Gemini emits usageMetadata on every chunk (running total). Don't
          // yield until the stream is over — otherwise we'd double-count by
          // accumulating partial sums.
          if (payload.usageMetadata) {
            lastUsage = payload.usageMetadata;
          }

          const candidate = payload.candidates?.[0];
          const parts = candidate?.content?.parts ?? [];
          for (const part of parts) {
            if (part.functionCall) {
              const args = part.functionCall.args ?? {};
              const callId =
                part.functionCall.id ?? `gemini_call_${nextIndex}`;
              yield {
                type: 'tool_call',
                index: nextIndex,
                id: callId,
                name: part.functionCall.name ?? '',
                arguments: JSON.stringify(args),
              };
              nextIndex++;
              continue;
            }
            if (part.text) {
              yield { type: 'text', content: part.text };
            }
          }
        }
      }
    } catch (err) {
      if (idle.fired && !opts?.signal?.aborted) {
        throw new PatterConnectionError(
          `Gemini stream idle timeout — no data for ${LLM_STREAM_IDLE_TIMEOUT_MS / 1000}s`,
        );
      }
      throw err;
    } finally {
      idle.clear();
      reader.cancel().catch(() => {});
    }

    if (lastUsage) {
      // ``promptTokenCount`` INCLUDES the cached portion; the usage chunk
      // contract bills ``inputTokens`` at the full input rate and
      // ``cacheReadInputTokens`` at the cache rate, so subtract to avoid
      // double-billing cached tokens (mirrors the OpenAI providers).
      const cached = lastUsage.cachedContentTokenCount ?? 0;
      yield {
        type: 'usage',
        inputTokens: Math.max(0, (lastUsage.promptTokenCount ?? 0) - cached),
        outputTokens: lastUsage.candidatesTokenCount,
        cacheReadInputTokens: cached,
      };
    }

    yield { type: 'done' };
  }
}

// ---------------------------------------------------------------------------
// Translation helpers (OpenAI format -> Gemini REST contents)
// ---------------------------------------------------------------------------

// Keys Gemini's restricted proto ``Schema`` accepts. Anything else
// ($schema, additionalProperties — emitted by strict-mode tools and nearly
// every zod-derived MCP server — oneOf, …) makes the request 400.
const GEMINI_SCHEMA_KEYS = new Set([
  'type',
  'description',
  'properties',
  'items',
  'enum',
  'required',
  'nullable',
  'format',
  'minimum',
  'maximum',
  'minLength',
  'maxLength',
  'minItems',
  'maxItems',
  'pattern',
  'anyOf',
  'default',
  'title',
]);

/** Recursively strip JSON-Schema keys Gemini's proto Schema rejects. */
export function sanitizeGeminiSchema(schema: unknown): unknown {
  if (Array.isArray(schema)) return schema.map(sanitizeGeminiSchema);
  if (schema !== null && typeof schema === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(schema as Record<string, unknown>)) {
      if (GEMINI_SCHEMA_KEYS.has(k)) out[k] = sanitizeGeminiSchema(v);
    }
    return out;
  }
  return schema;
}

function toGeminiTools(tools: OpenAIToolDef[]): Array<Record<string, unknown>> {
  const functionDeclarations = tools.map((t) => {
    const fn = t.function ?? t;
    return {
      name: String(fn.name ?? ''),
      description: String(fn.description ?? ''),
      parameters: sanitizeGeminiSchema(fn.parameters ?? { type: 'object', properties: {} }),
    };
  });
  if (functionDeclarations.length === 0) return [];
  return [{ functionDeclarations }];
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

function toGeminiContents(
  messages: Array<Record<string, unknown>>,
): { systemInstruction: string; contents: GeminiContent[] } {
  const systemParts: string[] = [];
  const contents: GeminiContent[] = [];
  // tool_call_id → function name, harvested from assistant turns so the
  // paired functionResponse can carry the real function name (Gemini requires
  // FunctionResponse.name to match FunctionCall.name; tool messages only
  // carry the call id). Mirrors the Python provider.
  const fnNameByCallId = new Map<string, string>();

  for (const rawMsg of messages as OpenAIStyleMessage[]) {
    const role = rawMsg.role;

    if (role === 'system') {
      if (typeof rawMsg.content === 'string' && rawMsg.content) {
        systemParts.push(rawMsg.content);
      }
      continue;
    }

    if (role === 'user') {
      if (typeof rawMsg.content === 'string' && rawMsg.content) {
        contents.push({ role: 'user', parts: [{ text: rawMsg.content }] });
      }
      continue;
    }

    if (role === 'assistant') {
      const parts: GeminiPart[] = [];
      if (typeof rawMsg.content === 'string' && rawMsg.content) {
        parts.push({ text: rawMsg.content });
      }
      for (const tc of rawMsg.tool_calls ?? []) {
        let args: Record<string, unknown> = {};
        try {
          const parsed = JSON.parse(tc.function?.arguments ?? '{}');
          if (parsed && typeof parsed === 'object') args = parsed as Record<string, unknown>;
        } catch {
          args = {};
        }
        if (tc.id && tc.function?.name) fnNameByCallId.set(tc.id, tc.function.name);
        parts.push({
          functionCall: {
            name: tc.function?.name ?? '',
            args,
            id: tc.id,
          },
        });
      }
      if (parts.length > 0) contents.push({ role: 'model', parts });
      continue;
    }

    if (role === 'tool') {
      const raw = rawMsg.content;
      let response: Record<string, unknown>;
      if (typeof raw === 'string') {
        try {
          const parsed = JSON.parse(raw);
          response =
            parsed && typeof parsed === 'object' && !Array.isArray(parsed)
              ? (parsed as Record<string, unknown>)
              : { result: parsed };
        } catch {
          response = { result: raw };
        }
      } else {
        response = (raw as unknown as Record<string, unknown>) ?? {};
      }
      contents.push({
        role: 'user',
        parts: [
          {
            functionResponse: {
              name:
                rawMsg.name ??
                fnNameByCallId.get(rawMsg.tool_call_id ?? '') ??
                rawMsg.tool_call_id ??
                '',
              response,
              id: rawMsg.tool_call_id,
            },
          },
        ],
      });
      continue;
    }
  }

  // Gemini requires that all functionResponse parts for a single model turn
  // appear in ONE user content entry (multiple parts). Consecutive role='user'
  // entries whose parts are exclusively functionResponse objects are the result
  // of mapping parallel tool-call results one-per-message. Merge them so the
  // API doesn't reject the request.
  const merged: GeminiContent[] = [];
  for (const entry of contents) {
    const prev = merged[merged.length - 1];
    const isFunctionResponseOnly = (c: GeminiContent): boolean =>
      c.role === 'user' && c.parts.every((p) => p.functionResponse !== undefined);
    if (prev && isFunctionResponseOnly(prev) && isFunctionResponseOnly(entry)) {
      prev.parts.push(...entry.parts);
    } else {
      merged.push(entry);
    }
  }

  // Gemini expects the first content to be a user turn. Voice agents almost
  // always open with ``firstMessage`` (a model greeting) — prepend a
  // synthetic user turn so stricter backends don't 400. Mirrors Python.
  if (merged.length > 0 && merged[0].role === 'model') {
    merged.unshift({ role: 'user', parts: [{ text: '(call connected)' }] });
  }

  return { systemInstruction: systemParts.join('\n\n'), contents: merged };
}

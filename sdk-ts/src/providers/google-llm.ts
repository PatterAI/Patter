/**
 * Google Gemini LLM provider for Patter's pipeline mode.
 *
 * Implements the ``LLMProvider`` interface against the Gemini Developer
 * API's streaming endpoint (``:streamGenerateContent?alt=sse``).
 * OpenAI-style messages/tools are translated into Gemini's ``contents``
 * and ``tools`` shapes, and streamed response parts are normalised to
 * Patter's ``{ type: 'text' | 'tool_call' | 'done' }`` chunks.
 *
 * Portions adapted from LiveKit Agents
 * (https://github.com/livekit/agents, commit
 * 78a66bcf79c5cea82989401c408f1dff4b961a5b,
 * file livekit-plugins/livekit-plugins-google/livekit/plugins/google/llm.py),
 * licensed under Apache License 2.0. Copyright 2023 LiveKit, Inc.
 *
 * Adaptations from the LiveKit source:
 *   * LiveKit uses the ``google-genai`` Python SDK. The TypeScript port
 *     uses native ``fetch`` against the REST SSE endpoint so we don't
 *     pull in a large SDK dependency.
 *   * Collapsed the Python ``llm.LLM`` / ``llm.LLMStream`` pair into a
 *     single class that satisfies Patter's ``LLMProvider`` interface.
 *   * Dropped Vertex AI support (which requires GCP auth) — only the
 *     Developer API (API key) path is ported. Vertex can be added by a
 *     follow-up PR once credential plumbing is in place.
 */

import type { LLMChunk, LLMProvider } from '../llm-loop';
import { getLogger } from '../logger';

const DEFAULT_MODEL = 'gemini-2.5-flash';
const DEFAULT_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta';

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

  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
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

    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const errText = await response.text();
      getLogger().error(`Gemini API error: ${response.status} ${errText}`);
      return;
    }

    const reader = response.body?.getReader();
    if (!reader) return;

    const decoder = new TextDecoder();
    let buffer = '';
    let nextIndex = 0;
    let lastUsage:
      | { promptTokenCount?: number; candidatesTokenCount?: number; cachedContentTokenCount?: number }
      | undefined;

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

    if (lastUsage) {
      yield {
        type: 'usage',
        inputTokens: lastUsage.promptTokenCount,
        outputTokens: lastUsage.candidatesTokenCount,
        cacheReadInputTokens: lastUsage.cachedContentTokenCount ?? 0,
      };
    }

    yield { type: 'done' };
  }
}

// ---------------------------------------------------------------------------
// Translation helpers (OpenAI format -> Gemini REST contents)
// ---------------------------------------------------------------------------

function toGeminiTools(tools: OpenAIToolDef[]): Array<Record<string, unknown>> {
  const functionDeclarations = tools.map((t) => {
    const fn = t.function ?? t;
    return {
      name: String(fn.name ?? ''),
      description: String(fn.description ?? ''),
      parameters: fn.parameters ?? { type: 'object', properties: {} },
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
              name: rawMsg.name ?? rawMsg.tool_call_id ?? '',
              response,
              id: rawMsg.tool_call_id,
            },
          },
        ],
      });
      continue;
    }
  }

  return { systemInstruction: systemParts.join('\n\n'), contents };
}

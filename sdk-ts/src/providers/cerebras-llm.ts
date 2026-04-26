/**
 * Cerebras LLM provider for Patter's pipeline mode.
 *
 * Cerebras exposes an OpenAI-compatible Chat Completions API at
 * ``https://api.cerebras.ai/v1``. This provider reuses the OpenAI SSE
 * parser from ``groq-llm.ts`` and optionally enables gzip request-body
 * compression to reduce TTFT for requests with large prompts
 * (see https://inference-docs.cerebras.ai/payload-optimization).
 *
 * Portions adapted from LiveKit Agents
 * (https://github.com/livekit/agents, commit
 * 78a66bcf79c5cea82989401c408f1dff4b961a5b,
 * file livekit-plugins/livekit-plugins-cerebras/livekit/plugins/cerebras/llm.py),
 * licensed under Apache License 2.0. Copyright 2026 LiveKit, Inc.
 *
 * Adaptations from the LiveKit source:
 *   * LiveKit's ``cerebras.LLM`` subclasses the LiveKit OpenAI plugin.
 *     Patter's analogue is a tiny wrapper around ``fetch`` that swaps
 *     the base URL and default model.
 *   * The msgpack payload optimisation from LiveKit is Python-only
 *     (msgpack in Node land isn't as standard); only gzip compression
 *     is ported. Enable with ``gzipCompression: true``.
 */

import type { LLMChunk, LLMProvider } from '../llm-loop';
import { getLogger } from '../logger';
import { parseOpenAISseStream } from './groq-llm';

const CEREBRAS_BASE_URL = 'https://api.cerebras.ai/v1';
// Default to the smallest fast Cerebras model available on the free tier so
// the SDK works out of the box. ``llama-3.3-70b`` exists on Cerebras but is
// gated to paid tiers — using it as default surfaces a confusing 404 for free
// users. ``llama3.1-8b`` is 8B params, sub-100ms TTFT on Cerebras hardware,
// and matches the LiveKit/Pipecat "small and fast for voice" philosophy.
//
// TODO(deprecation 2026-05-27): Cerebras has scheduled both ``llama3.1-8b``
// and ``qwen-3-235b-a22b-instruct-2507`` for retirement on this date. Before
// then, retest the free tier and switch the default to whichever 8B-class
// model replaces them (likely a Llama 4 Scout variant). Track at
// https://inference-docs.cerebras.ai/change-log
const DEFAULT_MODEL = 'llama3.1-8b';

export interface CerebrasLLMOptions {
  apiKey: string;
  model?: string;
  baseUrl?: string;
  /** Gzip request payloads for faster TTFT on large prompts. */
  gzipCompression?: boolean;
}

/** LLM provider backed by Cerebras's OpenAI-compatible Inference API. */
export class CerebrasLLMProvider implements LLMProvider {
  private readonly apiKey: string;
  readonly model: string;
  private readonly baseUrl: string;
  private readonly gzipCompression: boolean;

  constructor(options: CerebrasLLMOptions) {
    if (!options.apiKey) {
      throw new Error(
        'Cerebras API key is required. Pass it via { apiKey } or read CEREBRAS_API_KEY from the environment.',
      );
    }
    this.apiKey = options.apiKey;
    this.model = options.model ?? DEFAULT_MODEL;
    this.baseUrl = options.baseUrl ?? CEREBRAS_BASE_URL;
    this.gzipCompression = options.gzipCompression ?? false;
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

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${this.apiKey}`,
    };

    let payload: BodyInit = JSON.stringify(body);
    if (this.gzipCompression) {
      const compressed = await gzipEncode(payload as string);
      if (compressed) {
        // Cast via BlobPart — Uint8Array is a valid BodyInit at runtime
        // even though the DOM typings omit it in some lib versions.
        payload = compressed as unknown as BodyInit;
        headers['Content-Encoding'] = 'gzip';
      }
    }

    const response = await fetch(`${this.baseUrl}/chat/completions`, {
      method: 'POST',
      headers,
      body: payload,
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const errText = await response.text();
      // 404 on /chat/completions almost always means the model name isn't
      // available on the caller's tier (Cerebras gates models per plan). The
      // generic 404 message is opaque, so add a concrete recovery hint.
      if (response.status === 404 && errText.includes('model_not_found')) {
        getLogger().error(
          `Cerebras: model "${this.model}" not available on your tier. ` +
            `Override via \`new CerebrasLLM({ model: '<id>' })\` and list ` +
            `tier-available ids with \`GET ${this.baseUrl}/models\` ` +
            `(common: llama3.1-8b, qwen-3-235b-a22b-instruct-2507, llama-3.3-70b on paid). ` +
            `Raw response: ${errText}`,
        );
      } else {
        getLogger().error(`Cerebras API error: ${response.status} ${errText}`);
      }
      return;
    }

    yield* parseOpenAISseStream(response);
  }
}

/** Gzip a UTF-8 string using the WHATWG ``CompressionStream`` API. */
async function gzipEncode(data: string): Promise<Uint8Array | null> {
  const CompressionCtor = (
    globalThis as unknown as { CompressionStream?: new (format: string) => TransformStream }
  ).CompressionStream;
  if (!CompressionCtor) return null;

  const stream = new CompressionCtor('gzip');
  const writer = stream.writable.getWriter();
  const encoder = new TextEncoder();
  await writer.write(encoder.encode(data));
  await writer.close();

  const chunks: Uint8Array[] = [];
  const reader = stream.readable.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) chunks.push(value);
  }
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

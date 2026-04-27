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
import { PatterError } from '../errors';
import { VERSION } from '../version';
import { parseOpenAISseStream } from './groq-llm';

const CEREBRAS_BASE_URL = 'https://api.cerebras.ai/v1';
// Default to ``gpt-oss-120b`` — the highest-throughput production model on
// Cerebras's WSE-3 hardware (~3000 tok/sec, well above TTS consumption rate)
// and not on a deprecation schedule. Override via ``model`` if you need a
// smaller context window (``llama3.1-8b``) or a preview model.
const DEFAULT_MODEL = 'gpt-oss-120b';
const RETRY_BACKOFF_BASE_MS = 500;

export interface CerebrasLLMOptions {
  apiKey: string;
  model?: string;
  baseUrl?: string;
  /**
   * Gzip request payloads for faster TTFT on large prompts. Defaults to
   * ``true`` (parity with Python SDK) — set ``false`` to disable.
   *
   * msgpack encoding is Python-only; TS uses gzip alone, which captures
   * ~85% of the TTFT win.
   */
  gzipCompression?: boolean;
  /** Sampling temperature [0, 2]. */
  temperature?: number;
  /** Max tokens in the assistant response (sent as ``max_completion_tokens``). */
  maxTokens?: number;
  /**
   * Optional OpenAI-style ``response_format`` for JSON mode / structured
   * outputs, e.g. ``{ type: 'json_schema', json_schema: { ... } }``.
   * See https://inference-docs.cerebras.ai/capabilities/structured-outputs.
   */
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

/**
 * LLM provider backed by Cerebras's OpenAI-compatible Inference API.
 *
 * Available models on Cerebras (verified against
 * https://inference-docs.cerebras.ai/models/overview):
 *
 *   Production:
 *     - gpt-oss-120b                         (default — highest throughput on Cerebras, no deprecation)
 *     - llama3.1-8b                          (smaller context alternative; deprecating 2026-05-27)
 *
 *   Preview (opt-in):
 *     - qwen-3-235b-a22b-instruct-2507       (multilingual, strong on European languages)
 *     - zai-glm-4.7
 */
export class CerebrasLLMProvider implements LLMProvider {
  private readonly apiKey: string;
  readonly model: string;
  private readonly baseUrl: string;
  private readonly gzipCompression: boolean;
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

  constructor(options: CerebrasLLMOptions) {
    if (!options.apiKey) {
      throw new Error(
        'Cerebras API key is required. Pass it via { apiKey } or read CEREBRAS_API_KEY from the environment.',
      );
    }
    this.apiKey = options.apiKey;
    this.model = options.model ?? DEFAULT_MODEL;
    this.baseUrl = options.baseUrl ?? CEREBRAS_BASE_URL;
    // Default to gzip ON for parity with Python SDK — Cerebras TTFT
    // optimisation is ~15% on large prompts. Pass `gzipCompression: false`
    // to opt out (e.g. when running behind an upstream that already
    // compresses, or to avoid the small per-request CPU cost on tiny
    // prompts where gzip is a net loss).
    this.gzipCompression = options.gzipCompression ?? true;
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
    if (this.temperature !== undefined) body.temperature = this.temperature;
    if (this.maxTokens !== undefined) {
      // Cerebras's current API spec uses ``max_completion_tokens``.
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

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${this.apiKey}`,
      // Identify the SDK in upstream logs/rate-limit attribution.
      'User-Agent': `getpatter/${VERSION}`,
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

    // 1 retry on 5xx and 429 with exponential backoff. Honours Cerebras
    // rate-limit advisory headers (`x-ratelimit-reset-tokens-minute`,
    // `x-ratelimit-reset-requests-minute`) when present — delay is
    // ``max(advisory, exponential)``.
    const maxAttempts = 2;
    let lastErrText = '';
    let lastStatus = 0;

    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      const response = await fetch(`${this.baseUrl}/chat/completions`, {
        method: 'POST',
        headers,
        body: payload,
        signal: AbortSignal.timeout(30_000),
      });

      if (response.ok) {
        yield* parseOpenAISseStream(response);
        return;
      }

      lastStatus = response.status;
      lastErrText = await response.text().catch(() => '');
      const isRetriable = response.status === 429 || response.status >= 500;
      const isLastAttempt = attempt >= maxAttempts - 1;

      if (!isRetriable || isLastAttempt) {
        getLogger().error(`Cerebras API error: ${response.status} ${lastErrText}`);
        throw new PatterError(
          `Cerebras API error ${response.status}: ${lastErrText || 'request failed'}`,
        );
      }

      const advisoryMs = parseRateLimitResetMs(response.headers);
      const exponentialMs = RETRY_BACKOFF_BASE_MS * Math.pow(2, attempt);
      const delayMs = Math.max(advisoryMs, exponentialMs);
      getLogger().warn(
        `Cerebras API ${response.status} (attempt ${attempt + 1}/${maxAttempts}); retrying after ${delayMs}ms`,
      );
      await new Promise<void>((r) => setTimeout(r, delayMs));
    }

    // Defensive — loop above always returns or throws, but TypeScript
    // can't see that without an explicit terminal throw.
    throw new PatterError(`Cerebras API error ${lastStatus}: ${lastErrText || 'request failed'}`);
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

/**
 * Parse Cerebras rate-limit advisory headers and return the recommended
 * wait time in milliseconds. Cerebras emits ``x-ratelimit-reset-tokens-minute``
 * and ``x-ratelimit-reset-requests-minute`` (seconds, fractional). Returns 0
 * when no advisory header is present or it cannot be parsed.
 */
function parseRateLimitResetMs(headers: Headers): number {
  const candidates = [
    headers.get('x-ratelimit-reset-tokens-minute'),
    headers.get('x-ratelimit-reset-requests-minute'),
    // Some upstreams send the standard ``retry-after`` (seconds).
    headers.get('retry-after'),
  ];
  let bestMs = 0;
  for (const raw of candidates) {
    if (!raw) continue;
    const parsed = Number.parseFloat(raw);
    if (Number.isFinite(parsed) && parsed > 0) {
      const ms = parsed * 1000;
      if (ms > bestMs) bestMs = ms;
    }
  }
  return bestMs;
}

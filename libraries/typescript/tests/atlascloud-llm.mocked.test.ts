/**
 * Tests for the Atlas Cloud LLM provider (OpenAI-compatible gateway).
 *
 * Mocked at the paid boundary only: ``globalThis.fetch`` stands in for the Atlas
 * Cloud HTTP endpoint. Everything else — the provider, the shared SSE parser,
 * the namespace adapter, the pricing lookup — is the real code path.
 *
 * Atlas Cloud fronts many upstream model vendors behind one OpenAI-compatible
 * Chat Completions endpoint, so the provider only swaps the base URL and
 * credential source. These tests pin what that swap can get wrong: the request
 * actually goes to Atlas with the Patter User-Agent, the shared SSE parser is
 * reused (cached prompt tokens must not be billed twice), and a non-2xx
 * response throws instead of ending the stream silently — a silent end looks
 * like a successful empty turn and leaves the caller in dead air.
 */

import { describe, expect, it, vi, afterEach } from 'vitest';
import { AtlasCloudLLMProvider, AtlasCloudModel } from '../src/providers/atlascloud-llm';
import { LLM as AtlasCloudLLM } from '../src/llm/atlascloud';
import { PatterConnectionError } from '../src/errors';
import type { LLMChunk } from '../src/llm-loop';

const originalFetch = globalThis.fetch;
const originalKey = process.env.ATLASCLOUD_API_KEY;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalKey === undefined) delete process.env.ATLASCLOUD_API_KEY;
  else process.env.ATLASCLOUD_API_KEY = originalKey;
  vi.restoreAllMocks();
});

/** Build a streaming SSE Response from raw `data:` payload lines. */
function sseResponse(payloads: string[]): Response {
  const body = payloads.map((p) => `data: ${p}\n\n`).join('') + 'data: [DONE]\n\n';
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(body));
        controller.close();
      },
    }),
    { status: 200 },
  );
}

async function drain(
  gen: AsyncGenerator<LLMChunk, void, unknown>,
): Promise<LLMChunk[]> {
  const out: LLMChunk[] = [];
  for await (const chunk of gen) out.push(chunk);
  return out;
}

describe('AtlasCloudLLMProvider construction', () => {
  it('defaults to openai/gpt-4.1-mini', () => {
    expect(new AtlasCloudLLMProvider({ apiKey: 'apikey-test' }).model).toBe(
      'openai/gpt-4.1-mini',
    );
  });

  it('honours an explicit model override', () => {
    const provider = new AtlasCloudLLMProvider({
      apiKey: 'apikey-test',
      model: AtlasCloudModel.DEEPSEEK_V3_2,
    });
    expect(provider.model).toBe('deepseek-ai/deepseek-v3.2');
  });

  it('requires an API key', () => {
    expect(() => new AtlasCloudLLMProvider({ apiKey: '' })).toThrow(/ATLASCLOUD_API_KEY/);
  });

  it('exposes the atlascloud pricing key', () => {
    expect(AtlasCloudLLMProvider.providerKey).toBe('atlascloud');
  });
});

describe('AtlasCloudLLMProvider request shape', () => {
  it('posts to the Atlas Cloud gateway with bearer auth and the Patter UA', async () => {
    const fetchMock = vi.fn().mockResolvedValue(sseResponse([]));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const provider = new AtlasCloudLLMProvider({ apiKey: 'apikey-test', maxTokens: 64 });
    await drain(provider.stream([{ role: 'user', content: 'hi' }]));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('https://api.atlascloud.ai/v1/chat/completions');
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer apikey-test');
    expect(headers['User-Agent']).toMatch(/^getpatter\//);

    const body = JSON.parse(init.body as string);
    expect(body.model).toBe('openai/gpt-4.1-mini');
    expect(body.stream).toBe(true);
    expect(body.stream_options).toEqual({ include_usage: true });
    // Modern OpenAI spec name — Atlas accepts both, parity with Cerebras/OpenAI.
    expect(body.max_completion_tokens).toBe(64);
    expect(body).not.toHaveProperty('temperature');
  });

  it('honours a base URL override', async () => {
    const fetchMock = vi.fn().mockResolvedValue(sseResponse([]));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const provider = new AtlasCloudLLMProvider({
      apiKey: 'apikey-test',
      baseUrl: 'https://gateway.internal/v1',
    });
    await drain(provider.stream([{ role: 'user', content: 'hi' }]));

    expect(fetchMock.mock.calls[0][0]).toBe('https://gateway.internal/v1/chat/completions');
  });
});

describe('AtlasCloudLLMProvider streaming', () => {
  it('yields text and tool-call chunks from the shared OpenAI SSE parser', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      sseResponse([
        JSON.stringify({ choices: [{ delta: { content: 'Hel' } }] }),
        JSON.stringify({ choices: [{ delta: { content: 'lo' } }] }),
        JSON.stringify({
          choices: [
            {
              delta: {
                tool_calls: [
                  { index: 0, id: 'call_1', function: { name: 'lookup', arguments: '{"q":' } },
                ],
              },
            },
          ],
        }),
      ]),
    ) as unknown as typeof fetch;

    const provider = new AtlasCloudLLMProvider({ apiKey: 'apikey-test' });
    const chunks = await drain(provider.stream([{ role: 'user', content: 'hi' }]));

    expect(chunks.filter((c) => c.type === 'text').map((c) => c.content)).toEqual(['Hel', 'lo']);
    const toolCall = chunks.find((c) => c.type === 'tool_call');
    expect(toolCall).toMatchObject({ index: 0, id: 'call_1', name: 'lookup' });
  });

  it('subtracts cached prompt tokens so they are not billed twice', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      sseResponse([
        JSON.stringify({
          choices: [],
          usage: {
            prompt_tokens: 1000,
            completion_tokens: 40,
            prompt_tokens_details: { cached_tokens: 900 },
          },
        }),
      ]),
    ) as unknown as typeof fetch;

    const provider = new AtlasCloudLLMProvider({ apiKey: 'apikey-test' });
    const chunks = await drain(provider.stream([{ role: 'user', content: 'hi' }]));

    // prompt_tokens includes the cached portion; billing the full 1000 at the
    // input rate AND 900 at the cache-read rate would double-count.
    expect(chunks.find((c) => c.type === 'usage')).toMatchObject({
      inputTokens: 100,
      outputTokens: 40,
      cacheReadInputTokens: 900,
    });
  });
});

describe('AtlasCloudLLMProvider error handling', () => {
  it('throws (not silently ends) on a non-2xx response', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(new Response('{"error":"invalid api key"}', { status: 401 })) as unknown as typeof fetch;
    vi.spyOn(console, 'error').mockImplementation(() => {});

    const provider = new AtlasCloudLLMProvider({ apiKey: 'apikey-bad' });
    await expect(drain(provider.stream([{ role: 'user', content: 'hi' }]))).rejects.toThrow(
      PatterConnectionError,
    );
  });

  it('caps the error body it logs and rethrows', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(new Response('x'.repeat(5000), { status: 500 })) as unknown as typeof fetch;
    const logs: string[] = [];
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      logs.push(args.map(String).join(' '));
    });

    const provider = new AtlasCloudLLMProvider({ apiKey: 'apikey-test' });
    await expect(drain(provider.stream([{ role: 'user', content: 'hi' }]))).rejects.toThrow(
      /Atlas Cloud API returned 500/,
    );
    expect(logs.join('\n')).not.toContain('x'.repeat(201));
  });
});

describe('llm/atlascloud namespace adapter', () => {
  it('reads ATLASCLOUD_API_KEY from the environment', () => {
    process.env.ATLASCLOUD_API_KEY = 'apikey-from-env';
    const llm = new AtlasCloudLLM();
    expect(llm).toBeInstanceOf(AtlasCloudLLMProvider);
    expect(llm.model).toBe('openai/gpt-4.1-mini');
  });

  it('throws a named error when no key is available', () => {
    delete process.env.ATLASCLOUD_API_KEY;
    expect(() => new AtlasCloudLLM()).toThrow(/ATLASCLOUD_API_KEY/);
  });
});

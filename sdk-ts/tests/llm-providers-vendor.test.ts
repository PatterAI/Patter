/**
 * Tests for the Anthropic / Groq / Cerebras / Google LLM providers.
 *
 * All tests in this file are MOCK unit tests: ``globalThis.fetch`` is
 * stubbed to return a synthetic SSE stream shaped like the vendor API.
 * They verify that each provider correctly translates vendor-specific
 * events into Patter's ``{ type: 'text' | 'tool_call' | 'done' }``
 * chunk protocol without making network calls.
 *
 * Integration tests that hit real APIs should live in
 * ``tests/integration/`` and skip when the matching env var is absent.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LLMChunk } from '../src/llm-loop';
import { AnthropicLLMProvider } from '../src/providers/anthropic-llm';
import { GroqLLMProvider } from '../src/providers/groq-llm';
import { CerebrasLLMProvider } from '../src/providers/cerebras-llm';
import { GoogleLLMProvider } from '../src/providers/google-llm';

function buildSSEBody(lines: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const line of lines) {
        controller.enqueue(encoder.encode(line));
      }
      controller.close();
    },
  });
}

function mockFetchResponse(body: ReadableStream<Uint8Array>): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers(),
    body,
    text: async () => '',
  } as unknown as Response;
}

async function collect<T>(iter: AsyncGenerator<T, void, unknown>): Promise<T[]> {
  const out: T[] = [];
  for await (const v of iter) out.push(v);
  return out;
}

describe('AnthropicLLMProvider', () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('emits text chunks then done (MOCK Anthropic SSE)', async () => {
    const body = buildSSEBody([
      `data: ${JSON.stringify({ type: 'message_start' })}\n\n`,
      `data: ${JSON.stringify({
        type: 'content_block_start',
        index: 0,
        content_block: { type: 'text' },
      })}\n\n`,
      `data: ${JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'Hello ' },
      })}\n\n`,
      `data: ${JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: 'world!' },
      })}\n\n`,
      `data: ${JSON.stringify({ type: 'content_block_stop', index: 0 })}\n\n`,
      `data: ${JSON.stringify({ type: 'message_stop' })}\n\n`,
    ]);
    globalThis.fetch = vi.fn().mockResolvedValue(mockFetchResponse(body));

    const provider = new AnthropicLLMProvider({ apiKey: 'sk-test' });
    const chunks = await collect<LLMChunk>(
      provider.stream([
        { role: 'system', content: 'Be concise.' },
        { role: 'user', content: 'Hi' },
      ]),
    );

    expect(chunks).toEqual([
      { type: 'text', content: 'Hello ' },
      { type: 'text', content: 'world!' },
      { type: 'done' },
    ]);
  });

  it('emits tool_call chunks for tool_use blocks (MOCK Anthropic SSE)', async () => {
    const body = buildSSEBody([
      `data: ${JSON.stringify({ type: 'message_start' })}\n\n`,
      `data: ${JSON.stringify({
        type: 'content_block_start',
        index: 0,
        content_block: { type: 'tool_use', id: 'toolu_01', name: 'get_weather' },
      })}\n\n`,
      `data: ${JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'input_json_delta', partial_json: '{"city":' },
      })}\n\n`,
      `data: ${JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'input_json_delta', partial_json: '"Paris"}' },
      })}\n\n`,
      `data: ${JSON.stringify({ type: 'content_block_stop', index: 0 })}\n\n`,
      `data: ${JSON.stringify({ type: 'message_stop' })}\n\n`,
    ]);
    globalThis.fetch = vi.fn().mockResolvedValue(mockFetchResponse(body));

    const provider = new AnthropicLLMProvider({ apiKey: 'sk-test' });
    const chunks = await collect<LLMChunk>(
      provider.stream(
        [{ role: 'user', content: 'Weather in Paris?' }],
        [
          {
            type: 'function',
            function: {
              name: 'get_weather',
              description: 'Get weather',
              parameters: { type: 'object', properties: { city: { type: 'string' } } },
            },
          },
        ],
      ),
    );

    expect(chunks[0]).toEqual({
      type: 'tool_call',
      index: 0,
      id: 'toolu_01',
      name: 'get_weather',
      arguments: '',
    });
    expect(chunks[1]).toMatchObject({
      type: 'tool_call',
      index: 0,
      arguments: '{"city":',
    });
    expect(chunks[2]).toMatchObject({
      type: 'tool_call',
      index: 0,
      arguments: '"Paris"}',
    });
    expect(chunks[chunks.length - 1]).toEqual({ type: 'done' });
  });

  it('throws on missing API key', () => {
    expect(() => new AnthropicLLMProvider({ apiKey: '' })).toThrow(/Anthropic API key/);
  });
});

describe('GroqLLMProvider', () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('emits text chunks (MOCK OpenAI-compatible SSE)', async () => {
    const body = buildSSEBody([
      `data: ${JSON.stringify({ choices: [{ delta: { content: 'Hi ' } }] })}\n\n`,
      `data: ${JSON.stringify({ choices: [{ delta: { content: 'there!' } }] })}\n\n`,
      `data: [DONE]\n\n`,
    ]);
    const fetchMock = vi.fn().mockResolvedValue(mockFetchResponse(body));
    globalThis.fetch = fetchMock;

    const provider = new GroqLLMProvider({ apiKey: 'gsk-test' });
    const chunks = await collect<LLMChunk>(
      provider.stream([{ role: 'user', content: 'hi' }]),
    );
    expect(chunks).toEqual([
      { type: 'text', content: 'Hi ' },
      { type: 'text', content: 'there!' },
    ]);
    // URL points at Groq
    expect(fetchMock.mock.calls[0][0]).toBe(
      'https://api.groq.com/openai/v1/chat/completions',
    );
    // Uses bearer auth
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe(
      'Bearer gsk-test',
    );
  });

  it('throws on missing API key', () => {
    expect(() => new GroqLLMProvider({ apiKey: '' })).toThrow(/Groq API key/);
  });
});

describe('CerebrasLLMProvider', () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('emits text chunks with Cerebras base URL (MOCK)', async () => {
    const body = buildSSEBody([
      `data: ${JSON.stringify({ choices: [{ delta: { content: 'Fast.' } }] })}\n\n`,
      `data: [DONE]\n\n`,
    ]);
    const fetchMock = vi.fn().mockResolvedValue(mockFetchResponse(body));
    globalThis.fetch = fetchMock;

    const provider = new CerebrasLLMProvider({ apiKey: 'cb-test' });
    const chunks = await collect<LLMChunk>(
      provider.stream([{ role: 'user', content: 'hi' }]),
    );
    expect(chunks).toEqual([{ type: 'text', content: 'Fast.' }]);
    expect(fetchMock.mock.calls[0][0]).toBe(
      'https://api.cerebras.ai/v1/chat/completions',
    );
  });

  it('throws on missing API key', () => {
    expect(() => new CerebrasLLMProvider({ apiKey: '' })).toThrow(/Cerebras API key/);
  });
});

describe('GoogleLLMProvider', () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('emits text chunks then done (MOCK Gemini SSE)', async () => {
    const body = buildSSEBody([
      `data: ${JSON.stringify({
        candidates: [{ content: { parts: [{ text: 'Hello ' }] } }],
      })}\n\n`,
      `data: ${JSON.stringify({
        candidates: [{ content: { parts: [{ text: 'Gemini!' }] } }],
      })}\n\n`,
    ]);
    const fetchMock = vi.fn().mockResolvedValue(mockFetchResponse(body));
    globalThis.fetch = fetchMock;

    const provider = new GoogleLLMProvider({ apiKey: 'AIza-test' });
    const chunks = await collect<LLMChunk>(
      provider.stream([
        { role: 'system', content: 'Be nice.' },
        { role: 'user', content: 'Hi' },
      ]),
    );
    expect(chunks).toEqual([
      { type: 'text', content: 'Hello ' },
      { type: 'text', content: 'Gemini!' },
      { type: 'done' },
    ]);
    // URL includes SSE alt=sse and model
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain('gemini-2.5-flash:streamGenerateContent');
    expect(url).toContain('alt=sse');
  });

  it('emits tool_call chunks for functionCall parts (MOCK Gemini SSE)', async () => {
    const body = buildSSEBody([
      `data: ${JSON.stringify({
        candidates: [
          {
            content: {
              parts: [
                {
                  functionCall: {
                    name: 'get_weather',
                    args: { city: 'Paris' },
                    id: 'gc1',
                  },
                },
              ],
            },
          },
        ],
      })}\n\n`,
    ]);
    globalThis.fetch = vi.fn().mockResolvedValue(mockFetchResponse(body));

    const provider = new GoogleLLMProvider({ apiKey: 'AIza-test' });
    const chunks = await collect<LLMChunk>(
      provider.stream(
        [{ role: 'user', content: 'weather?' }],
        [
          {
            type: 'function',
            function: {
              name: 'get_weather',
              description: 'x',
              parameters: {
                type: 'object',
                properties: { city: { type: 'string' } },
              },
            },
          },
        ],
      ),
    );
    expect(chunks[0]).toMatchObject({
      type: 'tool_call',
      name: 'get_weather',
      id: 'gc1',
    });
    expect(JSON.parse(chunks[0].arguments!)).toEqual({ city: 'Paris' });
    expect(chunks[chunks.length - 1]).toEqual({ type: 'done' });
  });

  it('throws on missing API key', () => {
    expect(() => new GoogleLLMProvider({ apiKey: '' })).toThrow(/Google API key/);
  });
});

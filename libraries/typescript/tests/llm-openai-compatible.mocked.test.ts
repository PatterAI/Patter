/**
 * Tests for the generic OpenAI-compatible LLM provider.
 *
 * The provider construction, request-body assembly, header assembly, timeout
 * selection, env-key resolution, and SSE normalisation are ALL real code. The
 * only mocked surface is ``global.fetch`` — the paid/external HTTP boundary —
 * stubbed to return either a captured request or a real SSE ``ReadableStream``
 * fixture. Everything inward (``buildBody`` / ``buildHeaders`` /
 * ``parseOpenAISseStream``) runs unmodified.
 */

import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import {
  OpenAICompatibleLLMProvider,
  LLM,
} from '../src/llm/openai-compatible';
import type { LLMChunk } from '../src/llm-loop';
import { PatterConnectionError } from '../src/errors';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
  delete process.env.OAICOMPAT_TEST_KEY;
});

/** Capture the single fetch the provider issues, returning a 200 + empty body. */
function captureFetch(): { calls: Array<{ url: string; init: RequestInit }> } {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  globalThis.fetch = vi.fn(
    async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), init: init ?? {} });
      // Empty SSE body — the stream parser drains it cleanly.
      return new Response('', { status: 200 });
    },
  ) as unknown as typeof fetch;
  return { calls };
}

/** A real streaming OpenAI-format SSE body (text + tool_call + usage). */
function sseFixtureResponse(): Response {
  const lines = [
    'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":" there"}}]}\n\n',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"book","arguments":"{}"}}]}}]}\n\n',
    'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":4}}\n\n',
    'data: [DONE]\n\n',
  ];
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const enc = new TextEncoder();
      for (const l of lines) controller.enqueue(enc.encode(l));
      controller.close();
    },
  });
  return new Response(stream, { status: 200 });
}

async function drainBody(
  provider: OpenAICompatibleLLMProvider,
  callId?: string,
): Promise<{ body: Record<string, unknown>; headers: Record<string, string> }> {
  const { calls } = captureFetch();
  for await (const _ of provider.stream(
    [{ role: 'user', content: 'hi' }],
    null,
    callId ? { callId } : undefined,
  )) {
    // drain
  }
  const init = calls[0].init;
  return {
    body: JSON.parse(init.body as string) as Record<string, unknown>,
    headers: init.headers as Record<string, string>,
  };
}

describe('[unit] OpenAICompatibleLLMProvider construction', () => {
  it('points the request at the configured base URL and model', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
    });
    expect(provider.model).toBe('m');
    const { calls } = captureFetch();
    for await (const _ of provider.stream([{ role: 'user', content: 'hi' }])) {
      // drain
    }
    expect(calls[0].url).toBe('http://127.0.0.1:9/v1/chat/completions');
  });

  it('constructs a keyless gateway without error and omits the Authorization header', async () => {
    // base_url set, no apiKey, no apiKeyEnv → Ollama/vLLM/LM-Studio path.
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:11434/v1',
      model: 'llama3.1',
    });
    const { headers } = await drainBody(provider);
    expect(headers.Authorization).toBeUndefined();
    expect(headers['User-Agent']).toMatch(/^getpatter\//);
  });

  it('resolves the api key from the named environment variable', async () => {
    process.env.OAICOMPAT_TEST_KEY = 'secret-token-xyz';
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      apiKeyEnv: 'OAICOMPAT_TEST_KEY',
    });
    const { headers } = await drainBody(provider);
    expect(headers.Authorization).toBe('Bearer secret-token-xyz');
  });

  it('the LLM alias subclass is constructable and points at its base URL', () => {
    const llm = new LLM({ baseUrl: 'http://127.0.0.1:9/v1', model: 'm' });
    expect(llm).toBeInstanceOf(OpenAICompatibleLLMProvider);
    expect(llm.model).toBe('m');
  });
});

describe('[unit] OpenAICompatibleLLMProvider session continuity', () => {
  it('omits the user field by default (sessionUserPrefix unset)', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
    });
    const { body } = await drainBody(provider, 'abc');
    expect(body.user).toBeUndefined();
  });

  it('emits a stable patter-call user id when sessionUserPrefix + callId set', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionUserPrefix: 'patter-call-',
    });
    const { body } = await drainBody(provider, 'abc');
    expect(body.user).toBe('patter-call-abc');
  });

  it('omits the user field when sessionUserPrefix set but no callId is available', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionUserPrefix: 'patter-call-',
    });
    const { body } = await drainBody(provider); // no callId
    expect(body.user).toBeUndefined();
  });

  it('emits the session-id header as `${prefix}${callId}` INDEPENDENT of sessionUserPrefix', async () => {
    // No sessionUserPrefix → no user field, but the session-id header still fires.
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionIdHeader: 'X-Hermes-Session-Id',
      sessionIdPrefix: 'patter-call-',
    });
    const { body, headers } = await drainBody(provider, 'abc');
    expect(body.user).toBeUndefined();
    expect(headers['X-Hermes-Session-Id']).toBe('patter-call-abc');
  });

  it('defaults the session-id prefix to "" (raw call id) when sessionIdPrefix unset', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionIdHeader: 'x-openclaw-session-key',
    });
    const { headers } = await drainBody(provider, 'c2');
    // Regression: wire-identical to the old session_header behaviour.
    expect(headers['x-openclaw-session-key']).toBe('c2');
  });

  it('omits the session-id header when no callId is available', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionIdHeader: 'X-Hermes-Session-Id',
      sessionIdPrefix: 'patter-call-',
    });
    const { headers } = await drainBody(provider); // no callId
    expect(headers['X-Hermes-Session-Id']).toBeUndefined();
  });

  it('emits a STATIC session-key header (value == sessionKey, no call-id interpolation)', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionKeyHeader: 'X-Hermes-Session-Key',
      sessionKey: 'mem-123',
    });
    // Independent of call id: present with AND without a callId, value unchanged.
    const withCall = await drainBody(provider, 'abc');
    expect(withCall.headers['X-Hermes-Session-Key']).toBe('mem-123');
    const noCall = await drainBody(provider);
    expect(noCall.headers['X-Hermes-Session-Key']).toBe('mem-123');
  });

  it('omits the session-key header when sessionKeyHeader set but sessionKey undefined (opt-in)', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionKeyHeader: 'X-Hermes-Session-Key',
    });
    const { headers } = await drainBody(provider, 'abc');
    expect(headers['X-Hermes-Session-Key']).toBeUndefined();
  });

  it('combines all three signals into one request, preserving extraHeaders (no clobber)', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionUserPrefix: 'patter-call-',
      sessionIdHeader: 'X-Hermes-Session-Id',
      sessionIdPrefix: 'patter-call-',
      sessionKeyHeader: 'X-Hermes-Session-Key',
      sessionKey: 'mem-123',
      extraHeaders: { 'X-Foo': '1' },
    });
    const { body, headers } = await drainBody(provider, 'abc');
    expect(body.user).toBe('patter-call-abc');
    expect(headers['X-Hermes-Session-Id']).toBe('patter-call-abc');
    expect(headers['X-Hermes-Session-Key']).toBe('mem-123');
    // Pre-existing headers survive alongside the session headers.
    expect(headers['X-Foo']).toBe('1');
    expect(headers['User-Agent']).toMatch(/^getpatter\//);
  });

  it('sends no user field and no session headers when none configured (byte-identical baseline)', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
    });
    const { body, headers } = await drainBody(provider, 'abc');
    expect(body.user).toBeUndefined();
    // Only the baseline headers — no session signals.
    expect(Object.keys(headers).sort()).toEqual(['Content-Type', 'User-Agent']);
  });
});

describe('[unit] OpenAICompatibleLLMProvider headers and timeout', () => {
  it('merges extraHeaders alongside the getpatter User-Agent', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      extraHeaders: { 'X-Foo': '1' },
    });
    const { headers } = await drainBody(provider);
    expect(headers['X-Foo']).toBe('1');
    expect(headers['User-Agent']).toMatch(/^getpatter\//);
  });

  it('honours the configurable timeout instead of the base 30 s ceiling', async () => {
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout');
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      timeout: 120,
    });
    const { calls } = captureFetch();
    for await (const _ of provider.stream([{ role: 'user', content: 'hi' }])) {
      // drain
    }
    expect(calls.length).toBe(1);
    // The request timeout (not the 5 s warmup) must be 120_000 ms, proving
    // the base provider's hardcoded 30_000 ms ceiling was replaced.
    expect(timeoutSpy).toHaveBeenCalledWith(120_000);
    expect(timeoutSpy).not.toHaveBeenCalledWith(30_000);
  });

  it('defaults the generic timeout to 60 s', async () => {
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout');
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
    });
    const { calls } = captureFetch();
    for await (const _ of provider.stream([{ role: 'user', content: 'hi' }])) {
      // drain
    }
    expect(calls.length).toBe(1);
    expect(timeoutSpy).toHaveBeenCalledWith(60_000);
  });
});

describe('[mocked] OpenAICompatibleLLMProvider streaming over the HTTP boundary', () => {
  let captured: { url: string; init: RequestInit } | undefined;

  beforeEach(() => {
    captured = undefined;
    globalThis.fetch = vi.fn(
      async (url: string | URL | Request, init?: RequestInit) => {
        captured = { url: String(url), init: init ?? {} };
        return sseFixtureResponse();
      },
    ) as unknown as typeof fetch;
  });

  it('sends the user field + session-id header on the wire and normalises real SSE chunks', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionUserPrefix: 'patter-call-',
      sessionIdHeader: 'X-Hermes-Session-Id',
      sessionIdPrefix: 'patter-call-',
    });

    const chunks: LLMChunk[] = [];
    for await (const c of provider.stream(
      [{ role: 'user', content: 'book me in' }],
      null,
      { callId: 'call-99' },
    )) {
      chunks.push(c);
    }

    // The POST carried the session continuity signals on the wire.
    const body = JSON.parse(captured!.init.body as string) as Record<string, unknown>;
    expect(body.user).toBe('patter-call-call-99');
    const headers = captured!.init.headers as Record<string, string>;
    expect(headers['X-Hermes-Session-Id']).toBe('patter-call-call-99');

    // Real SSE chunks normalised by the shared parser.
    const texts = chunks.filter((c) => c.type === 'text').map((c) => c.content);
    expect(texts.join('')).toBe('Hi there');

    const toolCall = chunks.find((c) => c.type === 'tool_call');
    expect(toolCall).toMatchObject({ type: 'tool_call', name: 'book', id: 'call_1' });

    const usage = chunks.find((c) => c.type === 'usage');
    expect(usage).toMatchObject({ type: 'usage', inputTokens: 11, outputTokens: 4 });
  });

  it('throws PatterConnectionError on a non-OK gateway response instead of yielding an empty turn', async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response('upstream gateway unavailable', {
          status: 503,
          statusText: 'Service Unavailable',
        }),
    ) as unknown as typeof fetch;

    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
    });

    // The stream must surface the failure (matching the base OpenAILLMProvider)
    // so LLMLoop marks the turn errored rather than silently completing empty.
    const drain = async (): Promise<void> => {
      for await (const _ of provider.stream(
        [{ role: 'user', content: 'hi' }],
        null,
      )) {
        // should never reach here
      }
    };

    await expect(drain()).rejects.toBeInstanceOf(PatterConnectionError);
    await expect(drain()).rejects.toThrow('503');
  });
});

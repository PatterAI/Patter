/**
 * Unit tests for the Anthropic prompt-caching opt-out switch.
 *
 * The provider rewrites the request payload when ``promptCaching: true``
 * (the default) so that:
 *   - ``system`` becomes a single ``text`` block tagged with
 *     ``cache_control: { type: 'ephemeral' }``.
 *   - The LAST tool block carries the same marker (caches the whole list).
 *   - The ``anthropic-beta: prompt-caching-2024-07-31`` header is sent.
 *
 * Tests intercept ``fetch`` and inspect the captured request body / headers
 * without touching the network.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { AnthropicLLMProvider } from '../../src/providers/anthropic-llm';
import { LLM as AnthropicLLM } from '../../src/llm/anthropic';

interface CapturedRequest {
  url: string;
  body: Record<string, unknown>;
  headers: Record<string, string>;
}

/**
 * Replace ``globalThis.fetch`` with a spy that captures the request and
 * returns a minimal SSE-shaped response so the provider's stream loop
 * exits cleanly.
 */
function installFetchCapture(): { captured: CapturedRequest; spy: ReturnType<typeof vi.spyOn> } {
  const captured: CapturedRequest = {
    url: '',
    body: {},
    headers: {},
  };

  const spy = vi.spyOn(globalThis, 'fetch');
  spy.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    captured.url = typeof input === 'string' ? input : (input as URL).toString();
    if (init?.body) {
      captured.body = JSON.parse(init.body as string);
    }
    if (init?.headers) {
      const h = init.headers as Record<string, string>;
      for (const k of Object.keys(h)) {
        captured.headers[k.toLowerCase()] = h[k];
      }
    }

    // Empty SSE body — provider will read 0 events and yield "done".
    const stream = new ReadableStream({
      start(controller) {
        controller.close();
      },
    });

    return {
      ok: true,
      status: 200,
      body: stream,
      json: async () => ({}),
      text: async () => '',
    } as unknown as Response;
  });

  return { captured, spy };
}

async function drain(provider: AnthropicLLMProvider, messages: Array<Record<string, unknown>>, tools?: Array<Record<string, unknown>>): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  for await (const _ of provider.stream(messages, tools ?? null)) {
    // discard
  }
}

describe('AnthropicLLMProvider prompt caching', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('default-on wraps the system prompt in a cache_control text block', async () => {
    const { captured } = installFetchCapture();
    const provider = new AnthropicLLMProvider({ apiKey: 'sk-test' });

    await drain(provider, [
      { role: 'system', content: 'You are a long instruction-dense agent.' },
      { role: 'user', content: 'Hi' },
    ]);

    expect(Array.isArray(captured.body.system)).toBe(true);
    const sys = captured.body.system as Array<Record<string, unknown>>;
    expect(sys.length).toBe(1);
    expect(sys[0]).toMatchObject({
      type: 'text',
      text: 'You are a long instruction-dense agent.',
      cache_control: { type: 'ephemeral' },
    });
  });

  it('default-on tags ONLY the last tool block with cache_control', async () => {
    const { captured } = installFetchCapture();
    const provider = new AnthropicLLMProvider({ apiKey: 'sk-test' });

    const tools = [
      {
        type: 'function',
        function: {
          name: 'lookup_order',
          description: 'Look up order',
          parameters: { type: 'object', properties: {} },
        },
      },
      {
        type: 'function',
        function: {
          name: 'transfer_to_human',
          description: 'Hand off',
          parameters: { type: 'object', properties: {} },
        },
      },
    ];

    await drain(
      provider,
      [
        { role: 'system', content: 'sys' },
        { role: 'user', content: 'Hi' },
      ],
      tools,
    );

    const sentTools = captured.body.tools as Array<Record<string, unknown>>;
    expect(sentTools.length).toBe(2);
    expect(sentTools[0].cache_control).toBeUndefined();
    expect(sentTools[1].cache_control).toEqual({ type: 'ephemeral' });
    expect(sentTools[1].name).toBe('transfer_to_human');
  });

  it('default-on sends the anthropic-beta prompt-caching header', async () => {
    const { captured } = installFetchCapture();
    const provider = new AnthropicLLMProvider({ apiKey: 'sk-test' });

    await drain(provider, [
      { role: 'system', content: 'sys' },
      { role: 'user', content: 'Hi' },
    ]);

    expect(captured.headers['anthropic-beta']).toBe('prompt-caching-2024-07-31');
  });

  it('promptCaching=false falls back to plain string system + no beta header', async () => {
    const { captured } = installFetchCapture();
    const provider = new AnthropicLLMProvider({ apiKey: 'sk-test', promptCaching: false });

    await drain(
      provider,
      [
        { role: 'system', content: 'sys' },
        { role: 'user', content: 'Hi' },
      ],
      [
        {
          type: 'function',
          function: {
            name: 'noop',
            description: 'noop',
            parameters: { type: 'object', properties: {} },
          },
        },
      ],
    );

    expect(captured.body.system).toBe('sys');
    const sentTools = captured.body.tools as Array<Record<string, unknown>>;
    expect(sentTools[0].cache_control).toBeUndefined();
    expect(captured.headers['anthropic-beta']).toBeUndefined();
  });

  it('public LLM wrapper threads promptCaching=false through to the provider', async () => {
    const { captured } = installFetchCapture();
    const llm = new AnthropicLLM({ apiKey: 'sk-test', promptCaching: false });

    await drain(llm, [
      { role: 'system', content: 'sys' },
      { role: 'user', content: 'Hi' },
    ]);

    expect(captured.body.system).toBe('sys');
    expect(captured.headers['anthropic-beta']).toBeUndefined();
  });
});

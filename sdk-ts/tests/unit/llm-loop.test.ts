import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { LLMLoop, OpenAILLMProvider } from '../../src/llm-loop';
import type { LLMProvider, LLMChunk } from '../../src/llm-loop';
import type { ToolDefinition } from '../../src/types';

// ---------------------------------------------------------------------------
// Mock LLM provider
// ---------------------------------------------------------------------------

function createMockProvider(responses: LLMChunk[][]): LLMProvider {
  let callIndex = 0;
  return {
    async *stream() {
      const chunks = responses[callIndex] ?? [];
      callIndex++;
      for (const chunk of chunks) {
        yield chunk;
      }
    },
  };
}

describe('LLMLoop', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --- Turn completion detection ---

  describe('turn completion detection', () => {
    it('completes when no tool calls are present', async () => {
      const provider = createMockProvider([
        [
          { type: 'text', content: 'Hello ' },
          { type: 'text', content: 'world!' },
        ],
      ]);
      const loop = new LLMLoop('key', 'gpt-4o', 'System prompt', null, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('Hi', [], {})) {
        tokens.push(token);
      }

      expect(tokens).toEqual(['Hello ', 'world!']);
    });
  });

  // --- Streaming token accumulation ---

  describe('streaming token accumulation', () => {
    it('yields text tokens as they arrive', async () => {
      const provider = createMockProvider([
        [
          { type: 'text', content: 'A' },
          { type: 'text', content: 'B' },
          { type: 'text', content: 'C' },
        ],
      ]);
      const loop = new LLMLoop('key', 'model', 'prompt', null, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      expect(tokens).toEqual(['A', 'B', 'C']);
    });

    it('handles empty text content gracefully', async () => {
      const provider = createMockProvider([
        [
          { type: 'text', content: '' },
          { type: 'text', content: 'valid' },
        ],
      ]);
      const loop = new LLMLoop('key', 'model', 'prompt', null, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      expect(tokens).toEqual(['valid']);
    });
  });

  // --- Tool call extraction from partial JSON chunks ---

  describe('tool call extraction from partial JSON chunks', () => {
    it('accumulates tool call arguments from partial chunks', async () => {
      const handler = vi.fn().mockResolvedValue('tool result');
      const tools: ToolDefinition[] = [
        {
          name: 'lookup',
          description: 'Look up data',
          parameters: { type: 'object', properties: {} },
          handler,
        },
      ];

      const provider = createMockProvider([
        // First call: returns tool call chunks
        [
          { type: 'tool_call', index: 0, id: 'tc-1', name: 'lookup', arguments: '{"ke' },
          { type: 'tool_call', index: 0, arguments: 'y":"val' },
          { type: 'tool_call', index: 0, arguments: 'ue"}' },
        ],
        // Second call (after tool execution): returns text
        [{ type: 'text', content: 'Done' }],
      ]);

      const loop = new LLMLoop('key', 'model', 'prompt', tools, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      expect(handler).toHaveBeenCalledWith({ key: 'value' }, {});
      expect(tokens).toEqual(['Done']);
    });
  });

  // --- Tool call accumulator groups by index ---

  describe('tool call accumulator groups by index', () => {
    it('handles multiple concurrent tool calls at different indices', async () => {
      const handler1 = vi.fn().mockResolvedValue('"result1"');
      const handler2 = vi.fn().mockResolvedValue('"result2"');
      const tools: ToolDefinition[] = [
        { name: 'tool_a', description: '', parameters: {}, handler: handler1 },
        { name: 'tool_b', description: '', parameters: {}, handler: handler2 },
      ];

      const provider = createMockProvider([
        [
          { type: 'tool_call', index: 0, id: 'tc-a', name: 'tool_a', arguments: '{}' },
          { type: 'tool_call', index: 1, id: 'tc-b', name: 'tool_b', arguments: '{}' },
        ],
        [{ type: 'text', content: 'Both done' }],
      ]);

      const loop = new LLMLoop('key', 'model', 'prompt', tools, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      expect(handler1).toHaveBeenCalledOnce();
      expect(handler2).toHaveBeenCalledOnce();
      expect(tokens).toEqual(['Both done']);
    });
  });

  // --- Max iterations cap (10) ---

  describe('max iterations cap', () => {
    it('stops after 10 iterations of tool calls', async () => {
      let callCount = 0;
      const provider: LLMProvider = {
        async *stream() {
          callCount++;
          // Always return a tool call to keep iterating
          yield { type: 'tool_call' as const, index: 0, id: `tc-${callCount}`, name: 'loop_tool', arguments: '{}' };
        },
      };

      const tools: ToolDefinition[] = [
        {
          name: 'loop_tool',
          description: '',
          parameters: {},
          handler: vi.fn().mockResolvedValue('"ok"'),
        },
      ];

      const loop = new LLMLoop('key', 'model', 'prompt', tools, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      expect(callCount).toBe(10);
    });
  });

  // --- Tool webhook retry/timeout ---

  describe('tool webhook retry and timeout', () => {
    it('retries webhook 3 times on failure', async () => {
      fetchSpy.mockRejectedValue(new Error('Network error'));

      const tools: ToolDefinition[] = [
        {
          name: 'web_tool',
          description: '',
          parameters: {},
          webhookUrl: 'https://example.com/tool',
        },
      ];

      const provider = createMockProvider([
        [{ type: 'tool_call', index: 0, id: 'tc-w', name: 'web_tool', arguments: '{}' }],
        [{ type: 'text', content: 'After failure' }],
      ]);

      const loop = new LLMLoop('key', 'model', 'prompt', tools, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      // fetch called 3 times (1 + 2 retries)
      expect(fetchSpy).toHaveBeenCalledTimes(3);
      expect(tokens).toEqual(['After failure']);
    });

    it('succeeds on second retry', async () => {
      fetchSpy
        .mockRejectedValueOnce(new Error('Fail 1'))
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          json: async () => ({ result: 'success' }),
          text: async () => '{"result":"success"}',
        } as Response);

      const tools: ToolDefinition[] = [
        {
          name: 'retry_tool',
          description: '',
          parameters: {},
          webhookUrl: 'https://example.com/tool',
        },
      ];

      const provider = createMockProvider([
        [{ type: 'tool_call', index: 0, id: 'tc-r', name: 'retry_tool', arguments: '{}' }],
        [{ type: 'text', content: 'Success' }],
      ]);

      const loop = new LLMLoop('key', 'model', 'prompt', tools, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      expect(fetchSpy).toHaveBeenCalledTimes(2);
      expect(tokens).toEqual(['Success']);
    });

    it('uses 10s timeout signal on webhook fetch', async () => {
      fetchSpy.mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ ok: true }),
        text: async () => '{"ok":true}',
      } as Response);

      const tools: ToolDefinition[] = [
        {
          name: 'timeout_tool',
          description: '',
          parameters: {},
          webhookUrl: 'https://example.com/tool',
        },
      ];

      const provider = createMockProvider([
        [{ type: 'tool_call', index: 0, id: 'tc-t', name: 'timeout_tool', arguments: '{}' }],
        [{ type: 'text', content: 'OK' }],
      ]);

      const loop = new LLMLoop('key', 'model', 'prompt', tools, provider);

      for await (const _ of loop.run('test', [], {})) {
        // consume
      }

      const fetchOpts = fetchSpy.mock.calls[0][1] as RequestInit;
      expect(fetchOpts.signal).toBeDefined();
    });
  });

  // --- Unknown tool handling ---

  describe('unknown tool handling', () => {
    it('returns error for unknown tool name', async () => {
      const provider = createMockProvider([
        [{ type: 'tool_call', index: 0, id: 'tc-u', name: 'nonexistent', arguments: '{}' }],
        [{ type: 'text', content: 'After error' }],
      ]);

      const loop = new LLMLoop('key', 'model', 'prompt', [], provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      expect(tokens).toEqual(['After error']);
    });
  });

  // --- Handler error handling ---

  describe('handler error handling', () => {
    it('catches and returns handler errors as JSON', async () => {
      const handler = vi.fn().mockRejectedValue(new Error('Handler boom'));
      const tools: ToolDefinition[] = [
        { name: 'boom', description: '', parameters: {}, handler },
      ];

      const provider = createMockProvider([
        [{ type: 'tool_call', index: 0, id: 'tc-e', name: 'boom', arguments: '{}' }],
        [{ type: 'text', content: 'Recovered' }],
      ]);

      const loop = new LLMLoop('key', 'model', 'prompt', tools, provider);

      const tokens: string[] = [];
      for await (const token of loop.run('test', [], {})) {
        tokens.push(token);
      }

      expect(handler).toHaveBeenCalledOnce();
      expect(tokens).toEqual(['Recovered']);
    });
  });

  // --- buildMessages includes history ---

  describe('message building', () => {
    it('includes history entries in messages', async () => {
      const messages: unknown[] = [];
      const provider: LLMProvider = {
        async *stream(msgs) {
          messages.push(...msgs);
          yield { type: 'text' as const, content: 'done' };
        },
      };

      const loop = new LLMLoop('key', 'model', 'You are helpful', null, provider);

      const history = [
        { role: 'user', text: 'First message' },
        { role: 'assistant', text: 'First reply' },
      ];

      for await (const _ of loop.run('Second message', history, {})) {
        // consume
      }

      // Should have system, 2 history entries, and the new user message
      expect(messages).toHaveLength(4);
      expect((messages[0] as Record<string, unknown>).role).toBe('system');
      expect((messages[1] as Record<string, unknown>).content).toBe('First message');
      expect((messages[2] as Record<string, unknown>).content).toBe('First reply');
      expect((messages[3] as Record<string, unknown>).content).toBe('Second message');
    });
  });
});

// ---------------------------------------------------------------------------
// OpenAILLMProvider
// ---------------------------------------------------------------------------

describe('OpenAILLMProvider', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('streams text tokens from SSE response', async () => {
    const sseData = [
      'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
      'data: {"choices":[{"delta":{"content":" world"}}]}\n',
      'data: [DONE]\n',
    ].join('\n');

    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(sseData));
        controller.close();
      },
    });

    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: stream,
      json: async () => ({}),
      text: async () => '',
    } as unknown as Response);

    const provider = new OpenAILLMProvider('api-key', 'gpt-4o');
    const tokens: string[] = [];
    for await (const chunk of provider.stream([{ role: 'user', content: 'Hi' }])) {
      if (chunk.type === 'text' && chunk.content) {
        tokens.push(chunk.content);
      }
    }

    expect(tokens).toEqual(['Hello', ' world']);
  });

  it('handles non-ok response gracefully', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: false,
      status: 500,
      body: null,
      json: async () => ({}),
      text: async () => 'Internal Server Error',
    } as unknown as Response);

    const provider = new OpenAILLMProvider('api-key', 'gpt-4o');
    const tokens: LLMChunk[] = [];
    for await (const chunk of provider.stream([{ role: 'user', content: 'Hi' }])) {
      tokens.push(chunk);
    }

    expect(tokens).toHaveLength(0);
  });

  it('streams tool call chunks', async () => {
    const sseData = [
      'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc-1","function":{"name":"get_data","arguments":"{\\"k\\""}}]}}]}\n',
      'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"v\\"}"}}]}}]}\n',
      'data: [DONE]\n',
    ].join('\n');

    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(sseData));
        controller.close();
      },
    });

    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: stream,
      json: async () => ({}),
      text: async () => '',
    } as unknown as Response);

    const provider = new OpenAILLMProvider('api-key', 'gpt-4o');
    const toolChunks: LLMChunk[] = [];
    for await (const chunk of provider.stream([{ role: 'user', content: 'Hi' }])) {
      if (chunk.type === 'tool_call') {
        toolChunks.push(chunk);
      }
    }

    expect(toolChunks).toHaveLength(2);
    expect(toolChunks[0].id).toBe('tc-1');
    expect(toolChunks[0].name).toBe('get_data');
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { LLMLoop, OpenAILLMProvider } from '../src/llm-loop';
import type { LLMProvider, LLMChunk } from '../src/llm-loop';

describe('LLMLoop', () => {
  it('creates with required fields', () => {
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'You are helpful.');
    expect(loop).toBeDefined();
  });

  it('creates with tools', () => {
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'You are helpful.', [
      {
        name: 'lookup',
        description: 'Look up info',
        parameters: { type: 'object', properties: {} },
        webhookUrl: 'https://example.com/lookup',
      },
    ]);
    expect(loop).toBeDefined();
  });

  it('buildMessages constructs correct message array', () => {
    const loop = new LLMLoop(
      'sk-test',
      'gpt-4o-mini',
      'System prompt.',
      undefined,
      undefined,
      true, // disablePhonePreamble — keep verbatim for this assertion
    );
    // Access private method via any
    const messages = (loop as unknown as { buildMessages: (h: Array<{ role: string; text: string }>, t: string) => unknown[] }).buildMessages(
      [
        { role: 'user', text: 'Hello' },
        { role: 'assistant', text: 'Hi there' },
      ],
      'How are you?',
    );

    expect(messages).toHaveLength(4); // system + 2 history + user
    expect((messages[0] as { role: string }).role).toBe('system');
    expect((messages[0] as { content: string }).content).toBe('System prompt.');
    expect((messages[3] as { role: string }).role).toBe('user');
    expect((messages[3] as { content: string }).content).toBe('How are you?');
  });

  it('prepends default phone preamble when not disabled', async () => {
    const { DEFAULT_PHONE_PREAMBLE } = await import('../src/llm-loop');
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'You are helpful.');
    const messages = (loop as unknown as { buildMessages: (h: Array<{ role: string; text: string }>, t: string) => Array<{ role: string; content: string }> }).buildMessages([], 'Hi');
    expect(messages[0].role).toBe('system');
    expect(messages[0].content).toContain(DEFAULT_PHONE_PREAMBLE);
    expect(messages[0].content).toContain('You are helpful.');
  });

  it('executeTool calls handler when available', async () => {
    const handler = vi.fn().mockResolvedValue('handler result');
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'System.', [
      {
        name: 'test_tool',
        description: 'Test',
        parameters: {},
        webhookUrl: '',
        handler,
      },
    ]);

    const result = await (loop as unknown as {
      executeTool: (name: string, args: Record<string, unknown>, ctx: Record<string, unknown>) => Promise<string>;
    }).executeTool('test_tool', { key: 'value' }, { call_id: 'c1' });

    expect(result).toBe('handler result');
    expect(handler).toHaveBeenCalledWith({ key: 'value' }, { call_id: 'c1' });
  });

  it('executeTool returns error for unknown tool', async () => {
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'System.');

    const result = await (loop as unknown as {
      executeTool: (name: string, args: Record<string, unknown>, ctx: Record<string, unknown>) => Promise<string>;
    }).executeTool('unknown', {}, {});

    expect(result).toContain('Unknown tool');
  });

  // --- Streaming run() tests ---

  /**
   * Helper: build an SSE response body from an array of delta objects.
   * Each delta becomes one SSE "data:" line in the stream.
   */
  function buildSSEBody(
    deltas: Array<{
      content?: string;
      tool_calls?: Array<{
        index: number;
        id?: string;
        function?: { name?: string; arguments?: string };
      }>;
    }>,
  ): ReadableStream<Uint8Array> {
    const encoder = new TextEncoder();
    const lines = deltas.map((delta) => {
      const chunk = { choices: [{ delta }] };
      return `data: ${JSON.stringify(chunk)}\n\n`;
    });
    lines.push('data: [DONE]\n\n');

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

  describe('run() streaming', () => {
    let originalFetch: typeof globalThis.fetch;

    beforeEach(() => {
      originalFetch = globalThis.fetch;
    });

    afterEach(() => {
      globalThis.fetch = originalFetch;
    });

    it('yields text tokens from streaming response', async () => {
      const sseBody = buildSSEBody([
        { content: 'Hello ' },
        { content: 'world' },
        { content: '!' },
      ]);

      globalThis.fetch = vi.fn().mockResolvedValue(mockFetchResponse(sseBody));

      const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'You are helpful.');
      const tokens: string[] = [];
      for await (const token of loop.run('Hi', [], {})) {
        tokens.push(token);
      }

      expect(tokens).toEqual(['Hello ', 'world', '!']);
      expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    });

    it('handles tool call iteration (tool_call -> execute -> re-query)', async () => {
      const toolCallBody = buildSSEBody([
        {
          tool_calls: [
            { index: 0, id: 'call_1', function: { name: 'get_weather', arguments: '{"' } },
          ],
        },
        {
          tool_calls: [
            { index: 0, function: { arguments: 'city":"NY"}' } },
          ],
        },
      ]);

      const textBody = buildSSEBody([{ content: 'Sunny in NY.' }]);

      const handler = vi.fn().mockResolvedValue('{"temp": 72}');

      globalThis.fetch = vi
        .fn()
        .mockResolvedValueOnce(mockFetchResponse(toolCallBody))
        .mockResolvedValueOnce(mockFetchResponse(textBody));

      const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'You are helpful.', [
        {
          name: 'get_weather',
          description: 'Get weather info',
          parameters: { type: 'object', properties: { city: { type: 'string' } } },
          webhookUrl: '',
          handler,
        },
      ]);

      const tokens: string[] = [];
      for await (const token of loop.run('Weather in NY?', [], {})) {
        tokens.push(token);
      }

      expect(tokens).toEqual(['Sunny in NY.']);
      expect(handler).toHaveBeenCalledOnce();
      expect(handler).toHaveBeenCalledWith({ city: 'NY' }, {});
      expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    });

    it('stops after max iterations (10) when tool calls loop', async () => {
      const handler = vi.fn().mockResolvedValue('{"ok": true}');

      const makeToolCallBody = () =>
        buildSSEBody([
          {
            tool_calls: [
              { index: 0, id: 'call_loop', function: { name: 'loop_tool', arguments: '{}' } },
            ],
          },
        ]);

      globalThis.fetch = vi.fn().mockImplementation(() =>
        Promise.resolve(mockFetchResponse(makeToolCallBody())),
      );

      const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'System.', [
        {
          name: 'loop_tool',
          description: 'Always called',
          parameters: { type: 'object', properties: {} },
          webhookUrl: '',
          handler,
        },
      ]);

      const tokens: string[] = [];
      for await (const token of loop.run('trigger', [], {})) {
        tokens.push(token);
      }

      // 10 iterations = 10 fetch calls, each returning a tool call
      expect(globalThis.fetch).toHaveBeenCalledTimes(10);
      expect(handler).toHaveBeenCalledTimes(10);
    });

    it('handles empty response (no content, no tool calls)', async () => {
      // Stream with no content or tool call deltas — just [DONE]
      const emptyBody = buildSSEBody([{}]);

      globalThis.fetch = vi.fn().mockResolvedValue(mockFetchResponse(emptyBody));

      const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'System.');
      const tokens: string[] = [];
      for await (const token of loop.run('Hi', [], {})) {
        tokens.push(token);
      }

      expect(tokens).toEqual([]);
      expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    });

    it('throws PatterConnectionError on HTTP error from OpenAI', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: false,
        status: 429,
        text: async () => 'Rate limited',
        headers: new Headers(),
      } as unknown as Response);

      const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'System.');
      const tokens: string[] = [];
      let caught: unknown = null;
      try {
        for await (const token of loop.run('Hi', [], {})) {
          tokens.push(token);
        }
      } catch (err) {
        caught = err;
      }

      expect(caught).toBeInstanceOf(Error);
      expect((caught as Error).message).toContain('429');
      expect(tokens).toEqual([]);
    });
  });

  describe('custom LLMProvider', () => {
    it('accepts a custom LLMProvider and uses it for streaming', async () => {
      const customProvider: LLMProvider = {
        async *stream(_messages, _tools) {
          yield { type: 'text', content: 'Custom ' };
          yield { type: 'text', content: 'response' };
        },
      };

      const loop = new LLMLoop('', '', 'System.', null, customProvider);
      const tokens: string[] = [];
      for await (const token of loop.run('Hi', [], {})) {
        tokens.push(token);
      }

      expect(tokens).toEqual(['Custom ', 'response']);
    });

    it('handles tool calls from a custom provider', async () => {
      let callCount = 0;
      const handler = vi.fn().mockResolvedValue('{"result": "ok"}');

      const customProvider: LLMProvider = {
        async *stream(_messages, _tools) {
          callCount++;
          if (callCount === 1) {
            yield { type: 'tool_call', index: 0, id: 'tc_1', name: 'my_tool', arguments: '{"x":1}' };
          } else {
            yield { type: 'text', content: 'Done.' };
          }
        },
      };

      const loop = new LLMLoop('', '', 'System.', [
        {
          name: 'my_tool',
          description: 'Test tool',
          parameters: { type: 'object', properties: {} },
          webhookUrl: '',
          handler,
        },
      ], customProvider);

      const tokens: string[] = [];
      for await (const token of loop.run('Go', [], {})) {
        tokens.push(token);
      }

      expect(handler).toHaveBeenCalledWith({ x: 1 }, {});
      expect(tokens).toEqual(['Done.']);
    });

    it('OpenAILLMProvider is used by default when no custom provider given', () => {
      const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'System.');
      // Access the private provider field to verify it is an OpenAILLMProvider
      const provider = (loop as unknown as { provider: LLMProvider }).provider;
      expect(provider).toBeInstanceOf(OpenAILLMProvider);
    });
  });

  describe('[mocked] onToolCall observer (pipeline parity with realtime emitToolEvent)', () => {
    it('fires after a successful tool execution with (name, args, result)', async () => {
      let callCount = 0;
      const handler = vi.fn().mockResolvedValue('{"value": 42}');
      const customProvider: LLMProvider = {
        async *stream(_messages, _tools) {
          callCount++;
          if (callCount === 1) {
            yield { type: 'tool_call', index: 0, id: 'tc_1', name: 'lookup', arguments: '{"q":"x"}' };
          } else {
            yield { type: 'text', content: 'Found 42.' };
          }
        },
      };

      const loop = new LLMLoop('', '', 'System.', [
        {
          name: 'lookup',
          description: 'Test',
          parameters: { type: 'object', properties: {} },
          webhookUrl: '',
          handler,
        },
      ], customProvider);

      const observed: Array<[string, Record<string, unknown>, string]> = [];
      loop.setOnToolCall(async (name, args, result) => {
        observed.push([name, args, result]);
      });

      const tokens: string[] = [];
      for await (const token of loop.run('Go', [], {})) tokens.push(token);

      expect(tokens).toEqual(['Found 42.']);
      expect(observed).toHaveLength(1);
      expect(observed[0][0]).toBe('lookup');
      expect(observed[0][1]).toEqual({ q: 'x' });
      expect(observed[0][2]).toBe('{"value": 42}');
    });

    it('observer exceptions do not abort the loop', async () => {
      let callCount = 0;
      const handler = vi.fn().mockResolvedValue('"ok"');
      const customProvider: LLMProvider = {
        async *stream(_messages, _tools) {
          callCount++;
          if (callCount === 1) {
            yield { type: 'tool_call', index: 0, id: 'tc_n', name: 'noisy', arguments: '{}' };
          } else {
            yield { type: 'text', content: 'done' };
          }
        },
      };

      const loop = new LLMLoop('', '', 'System.', [
        {
          name: 'noisy',
          description: 'Test',
          parameters: { type: 'object', properties: {} },
          webhookUrl: '',
          handler,
        },
      ], customProvider);

      loop.setOnToolCall(async () => {
        throw new Error('observer boom');
      });

      const tokens: string[] = [];
      for await (const token of loop.run('Go', [], {})) tokens.push(token);

      // Loop completed despite observer failure
      expect(tokens).toEqual(['done']);
    });

    it('end-to-end pipeline: emits THREE transcript events for one tool turn (call + result + assistant)', async () => {
      // Authentic-test: only the LLM provider stream is mocked (paid
      // external boundary). The StreamHandler `recordToolCall` and
      // `emitAssistantTranscript` helpers run real code paths.
      const { StreamHandler } = await import('../src/stream-handler');
      type TEvt = Record<string, unknown>;

      let callCount = 0;
      const handler = vi.fn().mockResolvedValue('{"value": 42}');
      const customProvider: LLMProvider = {
        async *stream(_messages, _tools) {
          callCount++;
          if (callCount === 1) {
            yield { type: 'tool_call', index: 0, id: 'tc_l', name: 'lookup', arguments: '{"q":"x"}' };
          } else {
            yield { type: 'text', content: 'Found 42.' };
          }
        },
      };

      const loop = new LLMLoop('', '', 'System.', [
        {
          name: 'lookup',
          description: 'Test',
          parameters: { type: 'object', properties: {} },
          webhookUrl: '',
          handler,
        },
      ], customProvider);

      // Build a StreamHandler shell — the helpers we exercise only need
      // `history`, `callId`, and `deps.onTranscript`. No telephony bridge.
      const transcript: TEvt[] = [];
      const handlerShell = Object.create(StreamHandler.prototype) as {
        callId: string;
        history: { entries: TEvt[]; push: (e: TEvt) => void };
        deps: { onTranscript: (e: TEvt) => Promise<void> };
        recordToolCall: (n: string, a: Record<string, unknown>, r: string) => Promise<void>;
        emitAssistantTranscript: (text: string) => Promise<void>;
      };
      handlerShell.callId = 'call-abc';
      const entries: TEvt[] = [];
      handlerShell.history = {
        entries,
        push(e: TEvt) { entries.push(e); },
      };
      handlerShell.deps = {
        onTranscript: async (e: TEvt) => { transcript.push(e); },
      };

      // Wire the loop the same way StreamHandler.start does.
      loop.setOnToolCall(async (name, args, result) =>
        handlerShell.recordToolCall(name, args, result),
      );

      // Drive the loop and forward the final assistant text into
      // `emitAssistantTranscript` (mirrors `runPipelineLlm`'s
      // post-stream history push).
      const parts: string[] = [];
      for await (const token of loop.run('Go', [], { call_id: handlerShell.callId })) {
        parts.push(token);
      }
      const finalText = parts.join('');
      if (finalText) await handlerShell.emitAssistantTranscript(finalText);

      // 3 events in order: tool call, tool result, assistant
      expect(transcript).toHaveLength(3);
      const [callEvt, resultEvt, assistantEvt] = transcript;

      expect(callEvt.role).toBe('tool');
      expect(callEvt.tool_name).toBe('lookup');
      expect(callEvt.tool_args).toEqual({ q: 'x' });
      expect(callEvt.tool_result).toBeNull();
      expect(String(callEvt.text)).toMatch(/^lookup\(/);
      expect(callEvt.call_id).toBe('call-abc');

      expect(resultEvt.role).toBe('tool');
      expect(resultEvt.tool_name).toBe('lookup');
      expect(resultEvt.tool_result).toBe('{"value": 42}');
      expect(String(resultEvt.text)).toContain('→');

      expect(assistantEvt.role).toBe('assistant');
      expect(assistantEvt.text).toBe('Found 42.');
      expect(assistantEvt.call_id).toBe('call-abc');

      // History contains tool-call, tool-result, assistant entries
      const roles = entries.map((e) => e.role);
      expect(roles).toEqual(['tool', 'tool', 'assistant']);
    });
  });
});

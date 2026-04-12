/**
 * Deep tests for OpenAIRealtimeAdapter — connect lifecycle, event dispatching,
 * sendAudio, sendText, sendFunctionResult, cancelResponse, and close.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { OpenAIRealtimeAdapter } from '../../src/providers/openai-realtime';

// ---------------------------------------------------------------------------
// Mock the ws module
// ---------------------------------------------------------------------------

const mockWsInstances: Array<{
  handlers: Map<string, Array<(...args: unknown[]) => void>>;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  readyState: number;
}> = [];

vi.mock('ws', () => {
  const OPEN = 1;
  const CLOSED = 3;

  class MockWebSocket {
    static OPEN = OPEN;
    static CLOSED = CLOSED;
    readyState = OPEN;
    handlers = new Map<string, Array<(...args: unknown[]) => void>>();
    send = vi.fn();
    close = vi.fn();

    constructor(_url: string, _opts?: unknown) {
      mockWsInstances.push(this as unknown as (typeof mockWsInstances)[number]);
    }

    on(event: string, cb: (...args: unknown[]) => void) {
      if (!this.handlers.has(event)) {
        this.handlers.set(event, []);
      }
      this.handlers.get(event)!.push(cb);
    }

    emit(event: string, ...args: unknown[]) {
      const cbs = this.handlers.get(event) ?? [];
      for (const cb of cbs) {
        cb(...args);
      }
    }
  }

  return { default: MockWebSocket, __esModule: true };
});

function latestWs() {
  return mockWsInstances[mockWsInstances.length - 1];
}

/** Helper to simulate the OpenAI connect handshake. */
async function connectAdapter(adapter: OpenAIRealtimeAdapter): Promise<ReturnType<typeof latestWs>> {
  const connectPromise = adapter.connect();
  const ws = latestWs();

  // Simulate session.created
  ws.emit('message', Buffer.from(JSON.stringify({ type: 'session.created' })));

  // The adapter sends session.update, then we simulate session.updated
  ws.emit('message', Buffer.from(JSON.stringify({ type: 'session.updated' })));

  await connectPromise;
  return ws;
}

describe('OpenAIRealtimeAdapter (deep)', () => {
  beforeEach(() => {
    mockWsInstances.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --- connect() ---

  describe('connect()', () => {
    it('resolves after session.created + session.updated handshake', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test', 'gpt-4o-mini-realtime-preview', 'alloy', 'Be helpful.');

      const ws = await connectAdapter(adapter);

      // Should have sent session.update
      const sentMessages = ws.send.mock.calls.map((c: unknown[]) => JSON.parse(c[0] as string));
      const sessionUpdate = sentMessages.find((m: { type: string }) => m.type === 'session.update');
      expect(sessionUpdate).toBeDefined();
      expect(sessionUpdate.session.voice).toBe('alloy');
      expect(sessionUpdate.session.instructions).toBe('Be helpful.');
    });

    it('includes tools in session.update when provided', async () => {
      const tools = [
        { name: 'get_weather', description: 'Get weather', parameters: { type: 'object', properties: {} } },
      ];
      const adapter = new OpenAIRealtimeAdapter('sk-test', undefined, undefined, undefined, tools);

      const ws = await connectAdapter(adapter);

      const sentMessages = ws.send.mock.calls.map((c: unknown[]) => JSON.parse(c[0] as string));
      const sessionUpdate = sentMessages.find((m: { type: string }) => m.type === 'session.update');
      expect(sessionUpdate.session.tools).toHaveLength(1);
      expect(sessionUpdate.session.tools[0].type).toBe('function');
      expect(sessionUpdate.session.tools[0].name).toBe('get_weather');
    });

    it('uses default instructions when none provided', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');

      const ws = await connectAdapter(adapter);

      const sentMessages = ws.send.mock.calls.map((c: unknown[]) => JSON.parse(c[0] as string));
      const sessionUpdate = sentMessages.find((m: { type: string }) => m.type === 'session.update');
      expect(sessionUpdate.session.instructions).toContain('helpful');
    });

    it('rejects on WebSocket error during connect', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');

      const connectPromise = adapter.connect();
      const ws = latestWs();
      ws.emit('error', new Error('Connection refused'));

      await expect(connectPromise).rejects.toThrow('Connection refused');
    });

    it('ignores invalid JSON during connect', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');

      const connectPromise = adapter.connect();
      const ws = latestWs();

      // Send invalid JSON first — should not crash
      ws.emit('message', Buffer.from('not json'));

      // Then complete handshake normally
      ws.emit('message', Buffer.from(JSON.stringify({ type: 'session.created' })));
      ws.emit('message', Buffer.from(JSON.stringify({ type: 'session.updated' })));

      await expect(connectPromise).resolves.toBeUndefined();
    });
  });

  // --- onEvent() ---

  describe('onEvent()', () => {
    it('dispatches response.audio.delta as audio event', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      const cb = vi.fn();
      adapter.onEvent(cb);

      const audioBase64 = Buffer.from('fake-audio').toString('base64');
      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'response.audio.delta',
        delta: audioBase64,
      })));

      expect(cb).toHaveBeenCalledWith('audio', expect.any(Buffer));
    });

    it('dispatches response.audio_transcript.delta as transcript_output', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      const cb = vi.fn();
      adapter.onEvent(cb);

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'response.audio_transcript.delta',
        delta: 'Hello',
      })));

      expect(cb).toHaveBeenCalledWith('transcript_output', 'Hello');
    });

    it('dispatches input_audio_buffer.speech_started', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      const cb = vi.fn();
      adapter.onEvent(cb);

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'input_audio_buffer.speech_started',
      })));

      expect(cb).toHaveBeenCalledWith('speech_started', null);
    });

    it('dispatches conversation.item.input_audio_transcription.completed as transcript_input', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      const cb = vi.fn();
      adapter.onEvent(cb);

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'conversation.item.input_audio_transcription.completed',
        transcript: 'What is the weather?',
      })));

      expect(cb).toHaveBeenCalledWith('transcript_input', 'What is the weather?');
    });

    it('dispatches response.function_call_arguments.done as function_call', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      const cb = vi.fn();
      adapter.onEvent(cb);

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'response.function_call_arguments.done',
        call_id: 'call-1',
        name: 'get_weather',
        arguments: '{"city":"NYC"}',
      })));

      expect(cb).toHaveBeenCalledWith('function_call', {
        call_id: 'call-1',
        name: 'get_weather',
        arguments: '{"city":"NYC"}',
      });
    });

    it('dispatches response.done', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      const cb = vi.fn();
      adapter.onEvent(cb);

      ws.emit('message', Buffer.from(JSON.stringify({ type: 'response.done' })));
      expect(cb).toHaveBeenCalledWith('response_done', null);
    });

    it('dispatches error event', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      const cb = vi.fn();
      adapter.onEvent(cb);

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'error',
        error: { message: 'rate_limited' },
      })));

      expect(cb).toHaveBeenCalledWith('error', { message: 'rate_limited' });
    });

    it('ignores invalid JSON in event messages', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      const cb = vi.fn();
      adapter.onEvent(cb);

      ws.emit('message', Buffer.from('not json'));
      expect(cb).not.toHaveBeenCalled();
    });

    it('does nothing when ws is null', () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      // onEvent with no ws should not throw
      expect(() => adapter.onEvent(() => {})).not.toThrow();
    });
  });

  // --- sendAudio ---

  describe('sendAudio()', () => {
    it('sends base64-encoded audio as input_audio_buffer.append', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);
      ws.send.mockClear();

      const audio = Buffer.from('fake-mulaw-audio');
      adapter.sendAudio(audio);

      expect(ws.send).toHaveBeenCalledOnce();
      const sent = JSON.parse(ws.send.mock.calls[0][0] as string);
      expect(sent.type).toBe('input_audio_buffer.append');
      expect(sent.audio).toBe(audio.toString('base64'));
    });

    it('silently skips when not connected', () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      expect(() => adapter.sendAudio(Buffer.from('test'))).not.toThrow();
    });

    it('silently skips when WebSocket is not OPEN', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);
      ws.send.mockClear();

      ws.readyState = 3; // CLOSED
      adapter.sendAudio(Buffer.from('test'));
      expect(ws.send).not.toHaveBeenCalled();
    });
  });

  // --- sendText ---

  describe('sendText()', () => {
    it('sends conversation.item.create and response.create', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);
      ws.send.mockClear();

      await adapter.sendText('Hello, how are you?');

      expect(ws.send).toHaveBeenCalledTimes(2);
      const msg1 = JSON.parse(ws.send.mock.calls[0][0] as string);
      const msg2 = JSON.parse(ws.send.mock.calls[1][0] as string);

      expect(msg1.type).toBe('conversation.item.create');
      expect(msg1.item.type).toBe('message');
      expect(msg1.item.role).toBe('user');
      expect(msg1.item.content[0].text).toBe('Hello, how are you?');

      expect(msg2.type).toBe('response.create');
    });
  });

  // --- sendFunctionResult ---

  describe('sendFunctionResult()', () => {
    it('sends function_call_output and response.create', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);
      ws.send.mockClear();

      await adapter.sendFunctionResult('call-1', '{"temp": 72}');

      expect(ws.send).toHaveBeenCalledTimes(2);
      const msg1 = JSON.parse(ws.send.mock.calls[0][0] as string);
      const msg2 = JSON.parse(ws.send.mock.calls[1][0] as string);

      expect(msg1.type).toBe('conversation.item.create');
      expect(msg1.item.type).toBe('function_call_output');
      expect(msg1.item.call_id).toBe('call-1');
      expect(msg1.item.output).toBe('{"temp": 72}');

      expect(msg2.type).toBe('response.create');
    });
  });

  // --- cancelResponse ---

  describe('cancelResponse()', () => {
    it('sends response.cancel message', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);
      ws.send.mockClear();

      adapter.cancelResponse();

      expect(ws.send).toHaveBeenCalledOnce();
      const sent = JSON.parse(ws.send.mock.calls[0][0] as string);
      expect(sent.type).toBe('response.cancel');
    });

    it('does not throw when not connected', () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      expect(() => adapter.cancelResponse()).not.toThrow();
    });
  });

  // --- close ---

  describe('close()', () => {
    it('closes the WebSocket', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      const ws = await connectAdapter(adapter);

      adapter.close();

      expect(ws.close).toHaveBeenCalledOnce();
    });

    it('is a no-op when not connected', () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      expect(() => adapter.close()).not.toThrow();
    });

    it('can be called multiple times', async () => {
      const adapter = new OpenAIRealtimeAdapter('sk-test');
      await connectAdapter(adapter);

      adapter.close();
      adapter.close();
      // Should not throw
      expect(true).toBe(true);
    });
  });
});

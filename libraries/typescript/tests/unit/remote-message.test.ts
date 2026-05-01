/**
 * Deep tests for RemoteMessageHandler — callWebhook edge cases,
 * callWebSocket streaming, signPayload, and close().
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import crypto from 'node:crypto';
import { RemoteMessageHandler, isRemoteUrl, isWebSocketUrl } from '../../src/remote-message';

// ---------------------------------------------------------------------------
// Mock ws module for callWebSocket tests
// ---------------------------------------------------------------------------

const mockWsInstances: Array<{
  handlers: Map<string, Array<(...args: unknown[]) => void>>;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
}> = [];

vi.mock('ws', () => {
  class MockWebSocket {
    handlers = new Map<string, Array<(...args: unknown[]) => void>>();
    send = vi.fn();
    close = vi.fn();

    constructor(_url: string) {
      mockWsInstances.push(this);
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

  return { WebSocket: MockWebSocket };
});

function latestWs() {
  return mockWsInstances[mockWsInstances.length - 1];
}

describe('RemoteMessageHandler.callWebhook (extended)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('parses JSON response with text field', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      text: async () => JSON.stringify({ text: 'Bot says hello' }),
    } as unknown as Response);

    const handler = new RemoteMessageHandler();
    const result = await handler.callWebhook('https://example.com/hook', { text: 'Hi' });
    expect(result).toBe('Bot says hello');
  });

  it('returns raw text for non-JSON response', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => 'Plain response',
    } as unknown as Response);

    const handler = new RemoteMessageHandler();
    const result = await handler.callWebhook('https://example.com/hook', { text: 'Hi' });
    expect(result).toBe('Plain response');
  });

  it('handles malformed JSON response gracefully', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      text: async () => 'not valid json',
    } as unknown as Response);

    const handler = new RemoteMessageHandler();
    const result = await handler.callWebhook('https://example.com/hook', { text: 'Hi' });
    // Falls back to returning the raw text
    expect(result).toBe('not valid json');
  });

  it('handles JSON primitive (string) response', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      text: async () => '"just a string"',
    } as unknown as Response);

    const handler = new RemoteMessageHandler();
    const result = await handler.callWebhook('https://example.com/hook', { text: 'Hi' });
    // typeof body is string (not object with 'text'), so String(body) is called
    expect(result).toBe('just a string');
  });

  it('throws on non-OK status', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: false,
      status: 503,
      headers: new Headers(),
      text: async () => 'Service Unavailable',
    } as unknown as Response);

    const handler = new RemoteMessageHandler();
    await expect(
      handler.callWebhook('https://example.com/hook', { text: 'Hi' }),
    ).rejects.toThrow('HTTP 503');
  });

  it('throws on oversized response', async () => {
    const oversized = 'x'.repeat(64 * 1024 + 1);
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => oversized,
    } as unknown as Response);

    const handler = new RemoteMessageHandler();
    await expect(
      handler.callWebhook('https://example.com/hook', { text: 'Hi' }),
    ).rejects.toThrow('too large');
  });

  it('includes HMAC signature when webhookSecret is set', async () => {
    const secret = 'test-secret-456';
    const data = { text: 'Test', call_id: 'c1' };
    const body = JSON.stringify(data);
    const expectedSig = crypto.createHmac('sha256', secret).update(body).digest('hex');

    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => 'ok',
    } as unknown as Response);

    const handler = new RemoteMessageHandler(secret);
    await handler.callWebhook('https://example.com/hook', data);

    const headers = (fetchSpy.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers['X-Patter-Signature']).toBe(expectedSig);
  });

  it('omits signature header when no secret', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => 'ok',
    } as unknown as Response);

    const handler = new RemoteMessageHandler();
    await handler.callWebhook('https://example.com/hook', { text: 'Hi' });

    const headers = (fetchSpy.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers['X-Patter-Signature']).toBeUndefined();
  });

  it('uses 30s timeout signal', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => 'ok',
    } as unknown as Response);

    const handler = new RemoteMessageHandler();
    await handler.callWebhook('https://example.com/hook', { text: 'Hi' });

    const opts = fetchSpy.mock.calls[0][1] as RequestInit;
    expect(opts.signal).toBeDefined();
  });
});

// Helper: flush microtask queue so the async generator can advance
// after we resolve a promise (e.g. after 'open' fires).
const tick = () => new Promise<void>((r) => setTimeout(r, 0));

describe('RemoteMessageHandler.callWebSocket', () => {
  beforeEach(() => {
    mockWsInstances.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('sends data as JSON on open and yields text chunks', async () => {
    const handler = new RemoteMessageHandler();
    const gen = handler.callWebSocket('wss://example.com/stream', { text: 'Hi', call_id: 'c1' });

    // Start the generator — it awaits the 'open' event
    const firstNext = gen.next();

    await vi.waitFor(() => {
      expect(mockWsInstances.length).toBeGreaterThan(0);
    });

    const ws = latestWs();

    // Simulate open — resolves the internal Promise
    ws.emit('open');
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ text: 'Hi', call_id: 'c1' }));

    // Let the generator advance past the await and set up message/close/error listeners
    await tick();

    // Now simulate a text frame
    ws.emit('message', Buffer.from(JSON.stringify({ text: 'Hello back' })));

    const result1 = await firstNext;
    expect(result1.value).toBe('Hello back');
    expect(result1.done).toBe(false);

    // Simulate done frame
    ws.emit('message', Buffer.from(JSON.stringify({ done: true })));

    const result2 = await gen.next();
    expect(result2.done).toBe(true);
  });

  it('handles non-JSON text frames as raw strings', async () => {
    const handler = new RemoteMessageHandler();
    const gen = handler.callWebSocket('wss://example.com/stream', { text: 'Hi' });

    const firstNext = gen.next();

    await vi.waitFor(() => {
      expect(mockWsInstances.length).toBeGreaterThan(0);
    });

    const ws = latestWs();
    ws.emit('open');
    await tick();

    // Simulate raw text frame (not JSON)
    ws.emit('message', Buffer.from('raw text response'));

    const result = await firstNext;
    expect(result.value).toBe('raw text response');

    // Close the stream
    ws.emit('close');
    const end = await gen.next();
    expect(end.done).toBe(true);
  });

  it('handles JSON primitive frame', async () => {
    const handler = new RemoteMessageHandler();
    const gen = handler.callWebSocket('wss://example.com/stream', { text: 'Hi' });

    const firstNext = gen.next();

    await vi.waitFor(() => {
      expect(mockWsInstances.length).toBeGreaterThan(0);
    });

    const ws = latestWs();
    ws.emit('open');
    await tick();

    // Simulate JSON string (not an object)
    ws.emit('message', Buffer.from('"primitive value"'));

    const result = await firstNext;
    expect(result.value).toBe('primitive value');

    ws.emit('close');
    await gen.next();
  });

  it('closes the generator on WebSocket close event', async () => {
    const handler = new RemoteMessageHandler();
    const gen = handler.callWebSocket('wss://example.com/stream', { text: 'Hi' });

    const firstNext = gen.next();

    await vi.waitFor(() => {
      expect(mockWsInstances.length).toBeGreaterThan(0);
    });

    const ws = latestWs();
    ws.emit('open');
    await tick();

    ws.emit('close');

    const result = await firstNext;
    expect(result.done).toBe(true);
  });

  it('throws on WebSocket error after open', async () => {
    const handler = new RemoteMessageHandler();
    const gen = handler.callWebSocket('wss://example.com/stream', { text: 'Hi' });

    const firstNext = gen.next();

    await vi.waitFor(() => {
      expect(mockWsInstances.length).toBeGreaterThan(0);
    });

    const ws = latestWs();
    ws.emit('open');
    await tick();

    ws.emit('error', new Error('Connection lost'));

    // The generator sets error and done=true, resolveNext(null) breaks the loop,
    // then `if (error) throw error` fires — so firstNext should reject.
    await expect(firstNext).rejects.toThrow('Connection lost');
  });

  it('rejects on connection error during open', async () => {
    const handler = new RemoteMessageHandler();
    const gen = handler.callWebSocket('wss://example.com/stream', { text: 'Hi' });

    const firstNext = gen.next();

    await vi.waitFor(() => {
      expect(mockWsInstances.length).toBeGreaterThan(0);
    });

    const ws = latestWs();
    // Error before open — rejects the initial await
    ws.emit('error', new Error('ECONNREFUSED'));

    await expect(firstNext).rejects.toThrow('ECONNREFUSED');
  });
});

describe('RemoteMessageHandler misc', () => {
  it('close() is callable and does not throw', () => {
    const handler = new RemoteMessageHandler();
    expect(() => handler.close()).not.toThrow();
  });

  it('signPayload throws without secret', () => {
    const handler = new RemoteMessageHandler();
    // Access private method via bracket notation
    expect(() => (handler as unknown as { signPayload: (b: string) => string }).signPayload('body')).toThrow(
      'Cannot sign without a webhookSecret',
    );
  });

  it('signPayload produces correct HMAC', () => {
    const secret = 'my-secret';
    const handler = new RemoteMessageHandler(secret);
    const body = '{"test":"data"}';
    const expected = crypto.createHmac('sha256', secret).update(body).digest('hex');

    const result = (handler as unknown as { signPayload: (b: string) => string }).signPayload(body);
    expect(result).toBe(expected);
  });
});

// The isRemoteUrl / isWebSocketUrl tests are already in existing tests,
// but let's add a few edge cases here.
describe('isRemoteUrl / isWebSocketUrl edge cases', () => {
  it('returns false for empty string', () => {
    expect(isRemoteUrl('')).toBe(false);
  });

  it('returns false for path-only strings', () => {
    expect(isRemoteUrl('/api/webhook')).toBe(false);
  });

  it('isWebSocketUrl returns false for empty string', () => {
    expect(isWebSocketUrl('')).toBe(false);
  });
});

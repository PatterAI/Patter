/**
 * Deep tests for PatterConnection — connect/disconnect lifecycle,
 * message sending/receiving, and error handling with mocked WebSocket.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { PatterConnection } from '../../src/connection';
import { PatterConnectionError } from '../../src/errors';
import type WebSocket from 'ws';

// ---------------------------------------------------------------------------
// Mock the ws module to intercept WebSocket constructor
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

  return { default: MockWebSocket, __esModule: true };
});

// Helper to get the most recently created mock WS instance
function latestWs() {
  return mockWsInstances[mockWsInstances.length - 1];
}

describe('PatterConnection (deep)', () => {
  beforeEach(() => {
    mockWsInstances.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --- connect / disconnect lifecycle ---

  describe('connect()', () => {
    it('resolves on WebSocket open event', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('response');

      const connectPromise = conn.connect({ onMessage });

      // Simulate open
      const ws = latestWs();
      ws.emit('open');

      await expect(connectPromise).resolves.toBeUndefined();
      expect(conn.isConnected).toBe(true);
    });

    it('rejects with PatterConnectionError on WebSocket error', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('response');

      const connectPromise = conn.connect({ onMessage });

      const ws = latestWs();
      ws.emit('error', new Error('Connection refused'));

      await expect(connectPromise).rejects.toThrow(PatterConnectionError);
      await expect(connectPromise).rejects.toThrow('Failed to connect');
    });

    it('sets up message, error, and close listeners after open', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      // Should have handlers for open, error (initial + post-open), message, close
      expect(ws.handlers.get('message')).toBeDefined();
      expect(ws.handlers.get('close')).toBeDefined();
    });
  });

  // --- Message dispatching ---

  describe('message handling', () => {
    it('dispatches "message" type to onMessage callback and sends response', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('Hello back');

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      // Simulate incoming message
      const msgData = JSON.stringify({
        type: 'message',
        text: 'Hello',
        call_id: 'call-1',
        caller: '+15551111111',
      });
      ws.emit('message', Buffer.from(msgData));

      // Wait for async handler
      await vi.waitFor(() => {
        expect(onMessage).toHaveBeenCalledOnce();
      });

      expect(onMessage).toHaveBeenCalledWith({
        text: 'Hello',
        callId: 'call-1',
        caller: '+15551111111',
      });

      // Should have sent the response back
      expect(ws.send).toHaveBeenCalledWith(
        JSON.stringify({ type: 'response', call_id: 'call-1', text: 'Hello back' }),
      );
    });

    it('dispatches "call_start" type to onCallStart callback', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');
      const onCallStart = vi.fn().mockResolvedValue(undefined);

      const connectPromise = conn.connect({ onMessage, onCallStart });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      const msgData = JSON.stringify({ type: 'call_start', call_id: 'c1', caller: '+1' });
      ws.emit('message', Buffer.from(msgData));

      await vi.waitFor(() => {
        expect(onCallStart).toHaveBeenCalledOnce();
      });
      expect(onCallStart).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'call_start', call_id: 'c1' }),
      );
    });

    it('dispatches "call_end" type to onCallEnd callback', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');
      const onCallEnd = vi.fn().mockResolvedValue(undefined);

      const connectPromise = conn.connect({ onMessage, onCallEnd });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      const msgData = JSON.stringify({ type: 'call_end', call_id: 'c2' });
      ws.emit('message', Buffer.from(msgData));

      await vi.waitFor(() => {
        expect(onCallEnd).toHaveBeenCalledOnce();
      });
    });

    it('ignores invalid JSON messages', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      // Should not throw
      ws.emit('message', Buffer.from('not valid json'));
      expect(onMessage).not.toHaveBeenCalled();
    });

    it('ignores unknown message types', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');
      const onCallStart = vi.fn().mockResolvedValue(undefined);

      const connectPromise = conn.connect({ onMessage, onCallStart });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({ type: 'unknown_event' })));
      expect(onMessage).not.toHaveBeenCalled();
      expect(onCallStart).not.toHaveBeenCalled();
    });

    it('does not send response when onMessage returns null', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue(null);

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'message', text: 'hi', call_id: 'c1', caller: '+1',
      })));

      await vi.waitFor(() => {
        expect(onMessage).toHaveBeenCalledOnce();
      });

      // No response sent because onMessage returned null
      expect(ws.send).not.toHaveBeenCalled();
    });

    it('swallows handler errors without crashing', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockRejectedValue(new Error('Handler boom'));

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      // Should not throw
      ws.emit('message', Buffer.from(JSON.stringify({
        type: 'message', text: 'hi', call_id: 'c1', caller: '+1',
      })));

      await vi.waitFor(() => {
        expect(onMessage).toHaveBeenCalledOnce();
      });
    });
  });

  // --- WebSocket close handling ---

  describe('close handling', () => {
    it('sets ws to null on close event', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      expect(conn.isConnected).toBe(true);

      ws.readyState = 3; // CLOSED
      ws.emit('close');

      expect(conn.isConnected).toBe(false);
    });
  });

  // --- sendResponse ---

  describe('sendResponse()', () => {
    it('sends JSON response with call_id and text', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      await conn.sendResponse('call-42', 'Thanks!');
      expect(ws.send).toHaveBeenCalledWith(
        JSON.stringify({ type: 'response', call_id: 'call-42', text: 'Thanks!' }),
      );
    });

    it('throws PatterConnectionError when not connected', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      await expect(conn.sendResponse('c1', 'text')).rejects.toThrow(PatterConnectionError);
      await expect(conn.sendResponse('c1', 'text')).rejects.toThrow('Not connected');
    });
  });

  // --- requestCall ---

  describe('requestCall()', () => {
    it('sends call request with from, to, and firstMessage', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      await conn.requestCall('+15551111111', '+15552222222', 'Hi there');
      expect(ws.send).toHaveBeenCalledWith(
        JSON.stringify({
          type: 'call',
          from: '+15551111111',
          to: '+15552222222',
          first_message: 'Hi there',
        }),
      );
    });

    it('defaults firstMessage to empty string', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      await conn.requestCall('+15551111111', '+15552222222');
      const sent = JSON.parse(ws.send.mock.calls[0][0] as string);
      expect(sent.first_message).toBe('');
    });

    it('throws PatterConnectionError when not connected', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      await expect(conn.requestCall('+1', '+2')).rejects.toThrow(PatterConnectionError);
    });
  });

  // --- disconnect ---

  describe('disconnect()', () => {
    it('closes the WebSocket and sets ws to null', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      const onMessage = vi.fn().mockResolvedValue('resp');

      const connectPromise = conn.connect({ onMessage });
      const ws = latestWs();
      ws.emit('open');
      await connectPromise;

      await conn.disconnect();
      expect(ws.close).toHaveBeenCalledOnce();
      expect(conn.isConnected).toBe(false);
    });

    it('is a no-op when not connected', async () => {
      const conn = new PatterConnection('key-123', 'wss://api.test.com');
      // Should not throw
      await expect(conn.disconnect()).resolves.toBeUndefined();
    });
  });

  // --- parseMessage ---

  describe('parseMessage()', () => {
    it('parses valid message JSON', () => {
      const conn = new PatterConnection('key-123');
      const msg = conn.parseMessage(JSON.stringify({
        type: 'message',
        text: 'Hello',
        call_id: 'c1',
        caller: '+1',
      }));
      expect(msg).toEqual({ text: 'Hello', callId: 'c1', caller: '+1' });
    });

    it('defaults caller to empty string', () => {
      const conn = new PatterConnection('key-123');
      const msg = conn.parseMessage(JSON.stringify({
        type: 'message',
        text: 'Hi',
        call_id: 'c1',
      }));
      expect(msg!.caller).toBe('');
    });

    it('returns null for non-message type', () => {
      const conn = new PatterConnection('key-123');
      expect(conn.parseMessage(JSON.stringify({ type: 'call_start' }))).toBeNull();
    });

    it('returns null for invalid JSON', () => {
      const conn = new PatterConnection('key-123');
      expect(conn.parseMessage('bad json')).toBeNull();
    });
  });
});

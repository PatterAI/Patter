import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import crypto from 'node:crypto';
import { isRemoteUrl, isWebSocketUrl, RemoteMessageHandler } from '../src/remote-message';

describe('isRemoteUrl', () => {
  it('returns true for http URLs', () => {
    expect(isRemoteUrl('http://example.com/msg')).toBe(true);
    expect(isRemoteUrl('https://api.company.com/patter')).toBe(true);
  });

  it('returns true for WebSocket URLs', () => {
    expect(isRemoteUrl('ws://localhost:9000/stream')).toBe(true);
    expect(isRemoteUrl('wss://api.company.com/ws')).toBe(true);
  });

  it('returns false for non-URL strings', () => {
    expect(isRemoteUrl('hello')).toBe(false);
    expect(isRemoteUrl('ftp://files.com')).toBe(false);
  });

  it('returns false for non-string values', () => {
    expect(isRemoteUrl(42)).toBe(false);
    expect(isRemoteUrl(null)).toBe(false);
    expect(isRemoteUrl(undefined)).toBe(false);
    expect(isRemoteUrl(() => 'test')).toBe(false);
  });
});

describe('isWebSocketUrl', () => {
  it('returns true for ws:// and wss://', () => {
    expect(isWebSocketUrl('ws://localhost:9000')).toBe(true);
    expect(isWebSocketUrl('wss://api.company.com')).toBe(true);
  });

  it('returns false for http URLs', () => {
    expect(isWebSocketUrl('http://example.com')).toBe(false);
    expect(isWebSocketUrl('https://example.com')).toBe(false);
  });
});

describe('RemoteMessageHandler.callWebhook', () => {
  let originalFetch: typeof globalThis.fetch;
  let handler: RemoteMessageHandler;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    handler = new RemoteMessageHandler();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('returns text from JSON response with text field', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      text: async () => JSON.stringify({ text: 'Hello from webhook' }),
    } as unknown as Response);

    const result = await handler.callWebhook('https://example.com/hook', {
      text: 'Hi',
      call_id: 'c1',
    });

    expect(result).toBe('Hello from webhook');
    expect(globalThis.fetch).toHaveBeenCalledWith(
      'https://example.com/hook',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });

  it('returns raw text for plain text response', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => 'Plain text response',
    } as unknown as Response);

    const result = await handler.callWebhook('https://example.com/hook', {
      text: 'Hi',
    });

    expect(result).toBe('Plain text response');
  });

  it('rejects oversized responses', async () => {
    const oversized = 'x'.repeat(64 * 1024 + 1);

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => oversized,
    } as unknown as Response);

    await expect(
      handler.callWebhook('https://example.com/hook', { text: 'Hi' }),
    ).rejects.toThrow('too large');
  });

  it('throws on non-OK HTTP status', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      headers: new Headers(),
      text: async () => 'Internal Server Error',
    } as unknown as Response);

    await expect(
      handler.callWebhook('https://example.com/hook', { text: 'Hi' }),
    ).rejects.toThrow('HTTP 500');
  });

  it('returns stringified object when JSON has no text field', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      text: async () => JSON.stringify({ result: 42 }),
    } as unknown as Response);

    const result = await handler.callWebhook('https://example.com/hook', {
      text: 'Hi',
    });

    // When body is object without text field, String(body) is called
    expect(result).toBe('[object Object]');
  });

  it('warns about unencrypted http:// URLs', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => 'ok',
    } as unknown as Response);

    await handler.callWebhook('http://example.com/hook', { text: 'Hi' });

    expect(console.warn).toHaveBeenCalledWith(
      expect.stringContaining('unencrypted http://'),
    );
  });

  it('sends X-Patter-Signature header when webhookSecret is set', async () => {
    const secret = 'my-test-secret';
    const signedHandler = new RemoteMessageHandler(secret);
    const data = { text: 'Hello', call_id: 'c1' };
    const body = JSON.stringify(data);
    const expectedSig = crypto.createHmac('sha256', secret).update(body).digest('hex');

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => 'ok',
    } as unknown as Response);

    await signedHandler.callWebhook('https://example.com/hook', data);

    expect(globalThis.fetch).toHaveBeenCalledWith(
      'https://example.com/hook',
      expect.objectContaining({
        headers: {
          'Content-Type': 'application/json',
          'X-Patter-Signature': expectedSig,
        },
      }),
    );
  });

  it('does not send X-Patter-Signature when no secret is set', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/plain' }),
      text: async () => 'ok',
    } as unknown as Response);

    await handler.callWebhook('https://example.com/hook', { text: 'Hi' });

    const fetchCall = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const headers = fetchCall[1].headers as Record<string, string>;
    expect(headers['X-Patter-Signature']).toBeUndefined();
  });

  it('close() is callable', () => {
    expect(() => handler.close()).not.toThrow();
  });
});

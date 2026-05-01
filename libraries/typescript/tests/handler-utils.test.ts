import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createHistoryManager, executeToolWebhook } from '../src/handler-utils';
import type { HistoryEntry, ToolCallContext } from '../src/handler-utils';

// ---------------------------------------------------------------------------
// createHistoryManager
// ---------------------------------------------------------------------------

describe('createHistoryManager', () => {
  const entry = (text: string): HistoryEntry => ({
    role: 'user',
    text,
    timestamp: Date.now(),
  });

  it('push and getHistory returns entries', () => {
    const manager = createHistoryManager(10);
    manager.push(entry('hello'));
    manager.push(entry('world'));

    const history = manager.getHistory();
    expect(history).toHaveLength(2);
    expect(history[0].text).toBe('hello');
    expect(history[1].text).toBe('world');
  });

  it('caps at maxSize, dropping oldest entries', () => {
    const manager = createHistoryManager(3);
    manager.push(entry('a'));
    manager.push(entry('b'));
    manager.push(entry('c'));
    manager.push(entry('d'));
    manager.push(entry('e'));

    const history = manager.getHistory();
    expect(history).toHaveLength(3);
    expect(history[0].text).toBe('c');
    expect(history[1].text).toBe('d');
    expect(history[2].text).toBe('e');
  });

  it('getHistory returns a snapshot, not a reference', () => {
    const manager = createHistoryManager(10);
    manager.push(entry('one'));
    const snapshot = manager.getHistory();

    manager.push(entry('two'));
    expect(snapshot).toHaveLength(1);
    expect(manager.getHistory()).toHaveLength(2);
  });

  it('entries array reflects the live internal state', () => {
    const manager = createHistoryManager(5);
    manager.push(entry('x'));
    expect(manager.entries).toHaveLength(1);
    expect(manager.entries[0].text).toBe('x');

    manager.push(entry('y'));
    expect(manager.entries).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// executeToolWebhook
// ---------------------------------------------------------------------------

describe('executeToolWebhook', () => {
  const ctx: ToolCallContext = { callId: 'call-1', caller: '+1234567890' };

  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('returns JSON-stringified response on success', async () => {
    const payload = { result: 'ok' };
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(payload),
    });

    const result = await executeToolWebhook(
      'https://example.com/hook',
      'lookup',
      { q: 'test' },
      ctx,
    );

    expect(JSON.parse(result)).toEqual(payload);
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it('retries on failure, then succeeds', async () => {
    const payload = { result: 'ok' };
    const mockFetch = vi
      .fn()
      .mockRejectedValueOnce(new Error('network error'))
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(payload),
      });
    globalThis.fetch = mockFetch;

    const result = await executeToolWebhook(
      'https://example.com/hook',
      'lookup',
      {},
      ctx,
    );

    expect(JSON.parse(result)).toEqual(payload);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('returns error envelope after 3 failed attempts', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('boom'));

    const result = await executeToolWebhook(
      'https://example.com/hook',
      'lookup',
      {},
      ctx,
    );

    const parsed = JSON.parse(result);
    expect(parsed.error).toContain('Tool failed after 3 attempts');
    expect(parsed.fallback).toBe(true);
    expect(globalThis.fetch).toHaveBeenCalledTimes(3);
  });

  it('rejects private/internal IP URLs (SSRF)', async () => {
    globalThis.fetch = vi.fn();

    const privateUrls = [
      'https://127.0.0.1/hook',
      'https://10.0.0.1/hook',
      'https://192.168.1.1/hook',
      'https://localhost/hook',
    ];

    for (const url of privateUrls) {
      const result = await executeToolWebhook(url, 'tool', {}, ctx);
      const parsed = JSON.parse(result);
      expect(parsed.error).toBeTruthy();
      expect(parsed.fallback).toBe(true);
    }

    // fetch should never have been called for blocked URLs
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('rejects response body exceeding 1 MB cap', async () => {
    const MAX_RESPONSE_BYTES = 1 * 1024 * 1024;
    // Build a payload whose JSON.stringify result exceeds 1 MB
    const largePayload = { data: 'x'.repeat(MAX_RESPONSE_BYTES + 1000) };
    const serialized = JSON.stringify(largePayload);
    expect(serialized.length).toBeGreaterThan(MAX_RESPONSE_BYTES);

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(largePayload),
    });

    const result = await executeToolWebhook(
      'https://example.com/hook',
      'lookup',
      {},
      ctx,
    );

    const parsed = JSON.parse(result);
    expect(parsed.error).toContain('too large');
    expect(parsed.fallback).toBe(true);
  });
});

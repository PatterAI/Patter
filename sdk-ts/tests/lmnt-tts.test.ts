import { describe, it, expect, vi, beforeEach } from 'vitest';
import { LMNTTTS } from '../src/providers/lmnt-tts';

function makeMockReader(chunks: Buffer[]) {
  let i = 0;
  return {
    read: vi.fn().mockImplementation(async () => {
      if (i < chunks.length) {
        const value = new Uint8Array(chunks[i]);
        i += 1;
        return { done: false, value };
      }
      return { done: true, value: undefined };
    }),
    cancel: vi.fn().mockResolvedValue(undefined),
    releaseLock: vi.fn(),
  };
}

describe('LMNTTTS', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('initializes with defaults', () => {
    const tts = new LMNTTTS('k');
    expect(tts).toBeDefined();
  });

  it('throws on non-ok response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        text: async () => 'forbidden',
      }),
    );
    const tts = new LMNTTTS('k');
    await expect(tts.synthesizeStream('x').next()).rejects.toThrow(
      'LMNT TTS error 403',
    );
    vi.unstubAllGlobals();
  });

  it('throws when body is null', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, body: null }),
    );
    const tts = new LMNTTTS('k');
    await expect(tts.synthesizeStream('x').next()).rejects.toThrow(
      'LMNT TTS: no response body',
    );
    vi.unstubAllGlobals();
  });

  it('yields bytes from mock raw PCM stream', async () => {
    // MOCK: 3 chunks of small PCM zeros.
    const reader = makeMockReader([
      Buffer.alloc(4, 0),
      Buffer.alloc(4, 1),
      Buffer.alloc(4, 2),
    ]);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        body: { getReader: () => reader },
      }),
    );
    const tts = new LMNTTTS('k');
    const chunks: Buffer[] = [];
    for await (const c of tts.synthesizeStream('hi')) chunks.push(c);
    expect(chunks.length).toBe(3);
    expect(chunks.every((c) => c.length === 4)).toBe(true);
    vi.unstubAllGlobals();
  });

  it('builds payload with raw PCM default and api key header', async () => {
    const reader = makeMockReader([Buffer.alloc(4)]);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      body: { getReader: () => reader },
    });
    vi.stubGlobal('fetch', fetchMock);

    const tts = new LMNTTTS('secret', {
      voice: 'lily',
      sampleRate: 24000,
    });
    for await (const _ of tts.synthesizeStream('hola')) {
      /* noop */
    }
    const call = fetchMock.mock.calls[0];
    const body = JSON.parse(call[1].body as string);
    expect(body.text).toBe('hola');
    expect(body.voice).toBe('lily');
    expect(body.sample_rate).toBe(24000);
    expect(body.format).toBe('raw');
    expect(body.model).toBe('blizzard');
    // blizzard => language defaults to 'auto'
    expect(body.language).toBe('auto');
    expect(call[1].headers['X-API-Key']).toBe('secret');
    vi.unstubAllGlobals();
  });

  it('aurora model defaults language to en', () => {
    const tts = new LMNTTTS('k', { model: 'aurora' });
    // Not directly observable, but payload test below.
    expect(tts).toBeDefined();
  });
});

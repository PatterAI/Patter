import { describe, it, expect, vi, beforeEach } from 'vitest';
import { CartesiaTTS } from '../src/providers/cartesia-tts';

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

describe('CartesiaTTS', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('initializes with defaults', () => {
    const tts = new CartesiaTTS('key');
    expect(tts).toBeDefined();
  });

  it('throws on non-ok response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        text: async () => 'Unauthorized',
      }),
    );
    const tts = new CartesiaTTS('bad');
    await expect(tts.synthesizeStream('hello').next()).rejects.toThrow(
      'Cartesia TTS error 401',
    );
    vi.unstubAllGlobals();
  });

  it('throws when response body is null', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, body: null }),
    );
    const tts = new CartesiaTTS('k');
    await expect(tts.synthesizeStream('hi').next()).rejects.toThrow(
      'Cartesia TTS: no response body',
    );
    vi.unstubAllGlobals();
  });

  it('yields bytes chunks from mock stream', async () => {
    // MOCK: 2 chunks of PCM zeros.
    const chunk1 = Buffer.alloc(8, 0);
    const chunk2 = Buffer.alloc(8, 1);
    const reader = makeMockReader([chunk1, chunk2]);

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        body: { getReader: () => reader },
      }),
    );

    const tts = new CartesiaTTS('k');
    const chunks: Buffer[] = [];
    for await (const c of tts.synthesizeStream('hello world')) {
      chunks.push(c);
    }

    expect(chunks.length).toBe(2);
    expect(chunks[0].length).toBe(8);
    expect(chunks[1].length).toBe(8);
    expect(reader.releaseLock).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('sends payload with expected shape', async () => {
    const reader = makeMockReader([Buffer.alloc(4)]);
    const mock = vi.fn().mockResolvedValue({
      ok: true,
      body: { getReader: () => reader },
    });
    vi.stubGlobal('fetch', mock);

    const tts = new CartesiaTTS('secret', {
      model: 'sonic-3',
      voice: 'v',
      language: 'it',
      sampleRate: 24000,
      speed: 1.2,
    });
    // Consume the stream fully.
    for await (const _ of tts.synthesizeStream('hola')) {
      /* noop */
    }

    const call = mock.mock.calls[0];
    expect(call[0]).toContain('/tts/bytes');
    const body = JSON.parse(call[1].body as string);
    expect(body.model_id).toBe('sonic-3');
    expect(body.voice).toEqual({ mode: 'id', id: 'v' });
    expect(body.transcript).toBe('hola');
    expect(body.language).toBe('it');
    expect(body.output_format.sample_rate).toBe(24000);
    expect(body.generation_config.speed).toBe(1.2);
    expect(call[1].headers['X-API-Key']).toBe('secret');
    vi.unstubAllGlobals();
  });

  it('synthesize concatenates chunks', async () => {
    const reader = makeMockReader([Buffer.from([1, 2]), Buffer.from([3, 4])]);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        body: { getReader: () => reader },
      }),
    );
    const tts = new CartesiaTTS('k');
    const buf = await tts.synthesize('x');
    expect(buf.equals(Buffer.from([1, 2, 3, 4]))).toBe(true);
    vi.unstubAllGlobals();
  });
});

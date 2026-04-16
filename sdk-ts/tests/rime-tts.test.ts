import { describe, it, expect, vi, beforeEach } from 'vitest';
import { RimeTTS } from '../src/providers/rime-tts';

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

function mockResponse(body: unknown, contentType = 'audio/pcm') {
  return {
    ok: true,
    headers: {
      get: (key: string) => (key.toLowerCase() === 'content-type' ? contentType : null),
    },
    body,
    text: async () => '',
  };
}

describe('RimeTTS', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('defaults to arcana + astra', () => {
    const tts = new RimeTTS('k');
    expect(tts).toBeDefined();
  });

  it('throws on non-ok response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        text: async () => 'server err',
        headers: { get: () => 'text/plain' },
      }),
    );
    const tts = new RimeTTS('k');
    await expect(tts.synthesizeStream('x').next()).rejects.toThrow(
      'Rime TTS error 500',
    );
    vi.unstubAllGlobals();
  });

  it('throws if non-audio content type', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        headers: { get: () => 'application/json' },
        text: async () => '{"error":"bad"}',
        body: null,
      }),
    );
    const tts = new RimeTTS('k');
    await expect(tts.synthesizeStream('x').next()).rejects.toThrow(
      'Rime returned non-audio response',
    );
    vi.unstubAllGlobals();
  });

  it('yields bytes from audio/pcm response', async () => {
    const reader = makeMockReader([Buffer.alloc(4, 0xaa), Buffer.alloc(4, 0xbb)]);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(mockResponse({ getReader: () => reader })),
    );

    const tts = new RimeTTS('k');
    const chunks: Buffer[] = [];
    for await (const c of tts.synthesizeStream('hi')) chunks.push(c);
    expect(chunks.length).toBe(2);
    expect(chunks.every((c) => c.length > 0)).toBe(true);
    vi.unstubAllGlobals();
  });

  it('builds arcana payload correctly', async () => {
    const reader = makeMockReader([Buffer.alloc(4)]);
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({ getReader: () => reader }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const tts = new RimeTTS('key', {
      model: 'arcana',
      temperature: 0.5,
      topP: 0.9,
      maxTokens: 512,
      sampleRate: 16000,
    });
    for await (const _ of tts.synthesizeStream('hola')) {
      /* noop */
    }
    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body.modelId).toBe('arcana');
    expect(body.speaker).toBe('astra');
    expect(body.temperature).toBe(0.5);
    expect(body.top_p).toBe(0.9);
    expect(body.max_tokens).toBe(512);
    expect(body.samplingRate).toBe(16000);
    vi.unstubAllGlobals();
  });

  it('builds mistv2 payload correctly', async () => {
    const reader = makeMockReader([Buffer.alloc(2)]);
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({ getReader: () => reader }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const tts = new RimeTTS('key', {
      model: 'mistv2',
      speedAlpha: 1.1,
      reduceLatency: true,
      pauseBetweenBrackets: true,
    });
    for await (const _ of tts.synthesizeStream('hi')) {
      /* noop */
    }
    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body.modelId).toBe('mistv2');
    expect(body.speaker).toBe('cove');
    expect(body.speedAlpha).toBe(1.1);
    expect(body.reduceLatency).toBe(true);
    expect(body.pauseBetweenBrackets).toBe(true);
    vi.unstubAllGlobals();
  });
});

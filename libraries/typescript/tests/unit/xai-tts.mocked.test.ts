import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { XaiTTS, xaiCreateCustomVoice, XaiTTSCodec } from '../../src/providers/xai-tts';
import { TTS as XaiPipelineTTS } from '../../src/tts/xai';

/**
 * [mocked] xAI TTS — request shape, Bearer auth, telephony factories, custom
 * voices. The xAI `/tts` and `/custom-voices` endpoints are mocked at the
 * global `fetch` boundary; every other code path (payload assembly, byte
 * streaming, source-format declaration, multipart order, env-var fallback) runs
 * for real.
 */
describe('[mocked] xAI TTS — request + streaming', () => {
  const ORIGINAL_FETCH = global.fetch;
  let lastBody: Record<string, unknown> | null = null;
  let lastHeaders: Record<string, string> | null = null;
  let lastUrl: string | null = null;

  function mockBytesResponse(chunks: string[], status = 200): Response {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const enc = new TextEncoder();
        for (const c of chunks) controller.enqueue(enc.encode(c));
        controller.close();
      },
    });
    return new Response(stream, { status });
  }

  beforeEach(() => {
    lastBody = null;
    lastHeaders = null;
    lastUrl = null;
    global.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      lastUrl = typeof input === 'string' ? input : input.toString();
      lastBody = JSON.parse(init?.body as string);
      lastHeaders = init?.headers as Record<string, string>;
      return mockBytesResponse(['hello', 'world']);
    }) as typeof global.fetch;
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
  });

  it('posts to the REST endpoint with a Bearer auth header and JSON content-type', async () => {
    const tts = new XaiTTS('xai-test-key');
    await tts.synthesize('ciao');
    expect(lastUrl).toBe('https://api.x.ai/v1/tts');
    expect(lastHeaders?.Authorization).toBe('Bearer xai-test-key');
    expect(lastHeaders?.['Content-Type']).toBe('application/json');
  });

  it('defaults to voice=eve, language=auto, pcm @ 16000 (no mp3 default)', async () => {
    const tts = new XaiTTS('xai-test-key');
    await tts.synthesize('hi');
    expect(lastBody).toMatchObject({
      text: 'hi',
      voice_id: 'eve',
      language: 'auto',
      output_format: { codec: 'pcm', sample_rate: 16000 },
    });
    // Optional knobs omitted when unset.
    expect(lastBody).not.toHaveProperty('speed');
    expect(lastBody).not.toHaveProperty('optimize_streaming_latency');
    expect(lastBody).not.toHaveProperty('text_normalization');
  });

  it('threads voice / language / speed / latency / normalization into the payload', async () => {
    const tts = new XaiTTS('xai-test-key', {
      voice: 'leo',
      language: 'it',
      speed: 1.2,
      optimizeStreamingLatency: 2,
      textNormalization: true,
    });
    await tts.synthesize('ciao');
    expect(lastBody).toMatchObject({
      voice_id: 'leo',
      language: 'it',
      speed: 1.2,
      optimize_streaming_latency: 2,
      text_normalization: true,
    });
  });

  it('nests an MP3 bit_rate under output_format when set (omitted otherwise)', async () => {
    const tts = new XaiTTS('xai-test-key', {
      codec: 'mp3',
      sampleRate: 44100,
      bitRate: 192000,
    });
    await tts.synthesize('ciao');
    expect(lastBody).toMatchObject({
      output_format: { codec: 'mp3', sample_rate: 44100, bit_rate: 192000 },
    });
  });

  it('streams the response bytes through synthesizeStream', async () => {
    const tts = new XaiTTS('xai-test-key');
    const chunks: Buffer[] = [];
    for await (const c of tts.synthesizeStream('ciao')) chunks.push(c);
    expect(Buffer.concat(chunks).toString()).toBe('helloworld');
  });

  it('forTwilio emits μ-law @ 8 kHz and declares carrier-native source format', async () => {
    const tts = XaiTTS.forTwilio('xai-test-key');
    expect(tts.sourceAudioFormat()).toEqual({ encoding: 'mulaw', sampleRate: 8000 });
    await tts.synthesize('hi');
    expect(lastBody).toMatchObject({ output_format: { codec: 'mulaw', sample_rate: 8000 } });
  });

  it('forTelnyx emits pcm @ 16 kHz', async () => {
    const tts = XaiTTS.forTelnyx('xai-test-key');
    expect(tts.sourceAudioFormat()).toEqual({ encoding: 'pcm_s16le', sampleRate: 16000 });
    await tts.synthesize('hi');
    expect(lastBody).toMatchObject({ output_format: { codec: 'pcm', sample_rate: 16000 } });
  });

  it('surfaces a non-2xx status as an error', async () => {
    global.fetch = (async () => new Response('nope', { status: 400 })) as typeof global.fetch;
    const tts = new XaiTTS('xai-test-key');
    await expect(async () => {
      for await (const _ of tts.synthesizeStream('hi')) void _;
    }).rejects.toThrow(/xAI TTS error 400/);
  });

  describe('pipeline TTS facade (getpatter/tts/xai)', () => {
    afterEach(() => {
      delete process.env.XAI_API_KEY;
    });

    it('reads XAI_API_KEY from the environment', async () => {
      process.env.XAI_API_KEY = 'xai-env-key';
      const tts = new XaiPipelineTTS();
      await tts.synthesize('hi');
      expect(lastHeaders?.Authorization).toBe('Bearer xai-env-key');
    });

    it('forTwilio on the facade emits μ-law @ 8 kHz', () => {
      const tts = XaiPipelineTTS.forTwilio({ apiKey: 'xai-test-key' });
      expect(tts.sourceAudioFormat()).toEqual({ encoding: 'mulaw', sampleRate: 8000 });
    });
  });
});

describe('[mocked] xaiCreateCustomVoice', () => {
  const ORIGINAL_FETCH = global.fetch;
  let lastUrl: string | null = null;
  let lastHeaders: Record<string, string> | null = null;
  let lastBody: FormData | null = null;

  beforeEach(() => {
    lastUrl = null;
    lastHeaders = null;
    lastBody = null;
    global.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      lastUrl = typeof input === 'string' ? input : input.toString();
      lastHeaders = init?.headers as Record<string, string>;
      lastBody = init?.body as FormData;
      return new Response(JSON.stringify({ voice_id: 'custom-abc123' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof global.fetch;
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
  });

  it('posts multipart (name, language, then file LAST) and returns the voice_id', async () => {
    const voiceId = await xaiCreateCustomVoice({
      name: 'Narrator',
      language: 'en',
      audio: Buffer.from([1, 2, 3]),
      apiKey: 'xai-test-key',
    });
    expect(voiceId).toBe('custom-abc123');
    expect(lastUrl).toBe('https://api.x.ai/v1/custom-voices');
    expect(lastHeaders?.Authorization).toBe('Bearer xai-test-key');
    const keys = [...(lastBody as FormData).keys()];
    expect(keys).toEqual(['name', 'language', 'file']);
    const file = (lastBody as FormData).get('file') as File;
    expect(file).toBeInstanceOf(Blob);
    expect(file.type).toBe('audio/wav');
  });

  it('throws when the response omits a voice_id', async () => {
    global.fetch = (async () =>
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })) as typeof global.fetch;
    await expect(
      xaiCreateCustomVoice({ name: 'x', language: 'en', audio: Buffer.from([1]), apiKey: 'k' }),
    ).rejects.toThrow(/did not include a voice_id/);
  });

  it('exposes the codec enum for callers', () => {
    expect(XaiTTSCodec.MULAW).toBe('mulaw');
    expect(XaiTTSCodec.PCM).toBe('pcm');
  });
});

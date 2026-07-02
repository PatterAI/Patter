import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  SonioxTTS,
  SonioxTTSModel,
} from '../../src/providers/soniox-tts';
import { TTS as SonioxPipelineTTS } from '../../src/tts/soniox';

/**
 * [mocked] Soniox TTS — request shape, Bearer auth, telephony factories.
 *
 * The Soniox REST `/tts` endpoint is mocked at the global `fetch` boundary;
 * every other code path (payload assembly, byte streaming, source-format
 * declaration, env-var fallback) runs for real.
 */
describe('[mocked] Soniox TTS — request + streaming', () => {
  const ORIGINAL_FETCH = global.fetch;
  let lastBody: Record<string, unknown> | null = null;
  let lastHeaders: Record<string, string> | null = null;
  let lastUrl: string | null = null;

  /** Build a Response that streams each string as its own byte chunk. */
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
    global.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      lastUrl = typeof input === 'string' ? input : input.toString();
      lastBody = JSON.parse(init?.body as string);
      lastHeaders = init?.headers as Record<string, string>;
      return mockBytesResponse(['hello', 'world']);
    };
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
  });

  describe('low-level SonioxTTS', () => {
    it('posts to the REST endpoint with a Bearer auth header', async () => {
      const tts = new SonioxTTS('sk-key');
      await tts.synthesize('ciao');
      expect(lastUrl).toBe('https://tts-rt.soniox.com/tts');
      expect(lastHeaders?.Authorization).toBe('Bearer sk-key');
      expect(lastHeaders?.['Content-Type']).toBe('application/json');
    });

    it('defaults to tts-rt-v1, voice Adrian, pcm_s16le @ 16 kHz, language en', async () => {
      const tts = new SonioxTTS('tok');
      await tts.synthesize('hi');
      expect(lastBody?.model).toBe(SonioxTTSModel.TTS_RT_V1);
      expect(lastBody?.voice).toBe('Adrian');
      expect(lastBody?.language).toBe('en');
      expect(lastBody?.audio_format).toBe('pcm_s16le');
      expect(lastBody?.sample_rate).toBe(16000);
    });

    it('forwards optional bitrate / speed only when set', async () => {
      const tts = new SonioxTTS('tok');
      await tts.synthesize('hi');
      expect(lastBody).not.toHaveProperty('bitrate');
      expect(lastBody).not.toHaveProperty('speed');

      const tts2 = new SonioxTTS('tok', { bitrate: 128000, speed: 1.1, voice: 'Maya' });
      await tts2.synthesize('ciao');
      expect(lastBody?.bitrate).toBe(128000);
      expect(lastBody?.speed).toBe(1.1);
      expect(lastBody?.voice).toBe('Maya');
    });

    it('concatenates streamed bytes in order via synthesize', async () => {
      const tts = new SonioxTTS('tok');
      const out = await tts.synthesize('hi');
      expect(out.toString('utf8')).toBe('helloworld');
    });

    it('yields one Buffer per chunk via synthesizeStream', async () => {
      const tts = new SonioxTTS('tok');
      const collected: string[] = [];
      for await (const chunk of tts.synthesizeStream('hi')) {
        collected.push(chunk.toString('utf8'));
      }
      expect(collected).toEqual(['hello', 'world']);
    });

    it('surfaces non-200 responses with status + body', async () => {
      global.fetch = async () => new Response('rate limited', { status: 429 });
      const tts = new SonioxTTS('tok');
      await expect(tts.synthesize('hi')).rejects.toThrow(/Soniox TTS error 429/);
    });

    it('exposes the stable provider key', () => {
      expect(SonioxTTS.providerKey).toBe('soniox_tts');
    });
  });

  describe('source audio format', () => {
    it('default declares pcm_s16le at its configured rate', () => {
      const tts = new SonioxTTS('k', { sampleRate: 24000 });
      expect(tts.sourceAudioFormat()).toEqual({
        encoding: 'pcm_s16le',
        sampleRate: 24000,
      });
    });

    it('pcm_mulaw declares mulaw', () => {
      const tts = new SonioxTTS('k', { audioFormat: 'pcm_mulaw', sampleRate: 8000 });
      expect(tts.sourceAudioFormat()).toEqual({ encoding: 'mulaw', sampleRate: 8000 });
    });

    it('pcm_alaw declares alaw', () => {
      const tts = new SonioxTTS('k', { audioFormat: 'pcm_alaw', sampleRate: 8000 });
      expect(tts.sourceAudioFormat()).toEqual({ encoding: 'alaw', sampleRate: 8000 });
    });
  });

  describe('telephony factories (low-level)', () => {
    it('forTwilio emits μ-law @ 8 kHz natively (carrier-wire passthrough)', async () => {
      const tts = SonioxTTS.forTwilio('sk-key');
      await tts.synthesize('hello');
      expect(lastBody?.audio_format).toBe('pcm_mulaw');
      expect(lastBody?.sample_rate).toBe(8000);
      expect(tts.sourceAudioFormat()).toEqual({ encoding: 'mulaw', sampleRate: 8000 });
    });

    it('forTwilio honours overrides (voice, language, speed) but pins μ-law 8 kHz', async () => {
      const tts = SonioxTTS.forTwilio('sk-key', {
        voice: 'Maya',
        language: 'es',
        speed: 1.2,
      });
      await tts.synthesize('hola');
      expect(lastBody?.audio_format).toBe('pcm_mulaw');
      expect(lastBody?.sample_rate).toBe(8000);
      expect(lastBody?.voice).toBe('Maya');
      expect(lastBody?.language).toBe('es');
    });

    it('forTelnyx emits μ-law @ 8 kHz natively (PCMU wire passthrough)', async () => {
      const tts = SonioxTTS.forTelnyx('sk-key');
      await tts.synthesize('hello');
      expect(lastBody?.audio_format).toBe('pcm_mulaw');
      expect(lastBody?.sample_rate).toBe(8000);
    });

    it('constructor default unchanged (pcm_s16le @ 16000)', async () => {
      const tts = new SonioxTTS('sk-key');
      await tts.synthesize('hello');
      expect(lastBody?.audio_format).toBe('pcm_s16le');
      expect(lastBody?.sample_rate).toBe(16000);
    });
  });

  describe('pipeline-mode TTS class', () => {
    it('requires SONIOX_API_KEY (or apiKey opt) and forwards to provider', async () => {
      const prev = process.env.SONIOX_API_KEY;
      delete process.env.SONIOX_API_KEY;
      try {
        expect(() => new SonioxPipelineTTS()).toThrow(/SONIOX_API_KEY/);

        const tts = new SonioxPipelineTTS({ apiKey: 'tok-from-opt' });
        await tts.synthesize('hi');
        expect(lastHeaders?.Authorization).toBe('Bearer tok-from-opt');

        process.env.SONIOX_API_KEY = 'tok-from-env';
        const tts2 = new SonioxPipelineTTS();
        await tts2.synthesize('hi');
        expect(lastHeaders?.Authorization).toBe('Bearer tok-from-env');
      } finally {
        if (prev === undefined) delete process.env.SONIOX_API_KEY;
        else process.env.SONIOX_API_KEY = prev;
      }
    });

    it('forTwilio (options-object form) emits μ-law @ 8 kHz natively', async () => {
      const tts = SonioxPipelineTTS.forTwilio({ apiKey: 'sk-key' });
      await tts.synthesize('hello');
      expect(lastBody?.audio_format).toBe('pcm_mulaw');
      expect(lastBody?.sample_rate).toBe(8000);
    });

    it('forTwilio (positional form) is parent-compatible', async () => {
      const tts = SonioxPipelineTTS.forTwilio('sk-key');
      await tts.synthesize('hello');
      expect(lastBody?.audio_format).toBe('pcm_mulaw');
      expect(lastBody?.sample_rate).toBe(8000);
    });

    it('forTelnyx (options-object form) emits μ-law @ 8 kHz natively', async () => {
      const tts = SonioxPipelineTTS.forTelnyx({ apiKey: 'sk-key' });
      await tts.synthesize('hello');
      expect(lastBody?.audio_format).toBe('pcm_mulaw');
      expect(lastBody?.sample_rate).toBe(8000);
    });

    it('forTwilio falls back to SONIOX_API_KEY env var', async () => {
      const prev = process.env.SONIOX_API_KEY;
      process.env.SONIOX_API_KEY = 'env-key';
      try {
        const tts = SonioxPipelineTTS.forTwilio();
        await tts.synthesize('hello');
        expect(lastBody?.sample_rate).toBe(8000);
        expect(lastHeaders?.Authorization).toBe('Bearer env-key');
      } finally {
        if (prev === undefined) delete process.env.SONIOX_API_KEY;
        else process.env.SONIOX_API_KEY = prev;
      }
    });

    it('forTwilio without apiKey or env throws', () => {
      const prev = process.env.SONIOX_API_KEY;
      delete process.env.SONIOX_API_KEY;
      try {
        expect(() => SonioxPipelineTTS.forTwilio()).toThrow(/SONIOX_API_KEY/);
      } finally {
        if (prev !== undefined) process.env.SONIOX_API_KEY = prev;
      }
    });
  });
});

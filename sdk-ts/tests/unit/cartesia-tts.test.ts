import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { CartesiaTTS } from '../../src/providers/cartesia-tts';
import { TTS as CartesiaPipelineTTS } from '../../src/tts/cartesia';

/**
 * Default-model + API-version verification for Cartesia TTS.
 *
 * Cartesia's current GA is `sonic-3` (snapshot `sonic-3-2026-01-12`,
 * ~90 ms TTFB). The API version pin moves to `2025-04-16` to match the
 * Cartesia STT integration and the Line skill. Voice IDs from `sonic-2`
 * remain compatible — the default Katie voice still works on `sonic-3`.
 */
describe('Cartesia TTS — model and API version', () => {
  // We poke the private fields by serialising the instance via a JSON
  // payload-builder trip; that path is the public contract the bytes
  // endpoint sees.
  const ORIGINAL_FETCH = global.fetch;
  let lastBody: Record<string, unknown> | null = null;
  let lastHeaders: Record<string, string> | null = null;

  beforeEach(() => {
    lastBody = null;
    lastHeaders = null;
    global.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      lastBody = JSON.parse(init?.body as string);
      lastHeaders = init?.headers as Record<string, string>;
      return new Response(new Uint8Array([1, 2, 3, 4]), { status: 200 });
    };
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
  });

  describe('low-level CartesiaTTS', () => {
    it('defaults model to "sonic-3"', async () => {
      const tts = new CartesiaTTS('ct-key');
      await tts.synthesize('hello');
      expect(lastBody?.model_id).toBe('sonic-3');
    });

    it('sends Cartesia-Version "2025-04-16" header', async () => {
      const tts = new CartesiaTTS('ct-key');
      await tts.synthesize('hello');
      expect(lastHeaders?.['Cartesia-Version']).toBe('2025-04-16');
    });

    it('honours an explicit model override (e.g. sonic-2 for back-compat)', async () => {
      const tts = new CartesiaTTS('ct-key', { model: 'sonic-2' });
      await tts.synthesize('hello');
      expect(lastBody?.model_id).toBe('sonic-2');
    });

    it('honours an explicit apiVersion override', async () => {
      const tts = new CartesiaTTS('ct-key', { apiVersion: '2024-11-13' });
      await tts.synthesize('hello');
      expect(lastHeaders?.['Cartesia-Version']).toBe('2024-11-13');
    });
  });

  describe('pipeline-mode TTS class', () => {
    it('defaults model to "sonic-3"', async () => {
      const tts = new CartesiaPipelineTTS({ apiKey: 'ct-key' });
      await tts.synthesize('hello');
      expect(lastBody?.model_id).toBe('sonic-3');
    });

    it('sends the new Cartesia-Version header', async () => {
      const tts = new CartesiaPipelineTTS({ apiKey: 'ct-key' });
      await tts.synthesize('hello');
      expect(lastHeaders?.['Cartesia-Version']).toBe('2025-04-16');
    });

    it('preserves the default voice ID (sonic-2 → sonic-3 compatible)', async () => {
      const tts = new CartesiaPipelineTTS({ apiKey: 'ct-key' });
      await tts.synthesize('hello');
      const voice = lastBody?.voice as { mode: string; id: string };
      expect(voice.id).toBe('f786b574-daa5-4673-aa0c-cbe3e8534c02');
    });
  });
});

/**
 * Telephony sample-rate factories.
 *
 * Cartesia accepts `sample_rate` directly in the request body. Asking for
 * 8 kHz at the source skips the SDK-side 16 kHz → 8 kHz resample step
 * before PCM → μ-law transcoding for Twilio. Telnyx's L16/16000 default
 * already matches the bare-constructor default.
 */
describe('Cartesia TTS — telephony factories', () => {
  const ORIGINAL_FETCH = global.fetch;
  let lastBody: Record<string, unknown> | null = null;

  beforeEach(() => {
    lastBody = null;
    global.fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
      lastBody = JSON.parse(init?.body as string);
      return new Response(new Uint8Array([1, 2, 3, 4]), { status: 200 });
    };
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
  });

  describe('low-level CartesiaTTS', () => {
    it('forTwilio sets sampleRate to 8000', async () => {
      const tts = CartesiaTTS.forTwilio('ct-key');
      await tts.synthesize('hello');
      const outputFormat = lastBody?.output_format as { sample_rate: number; encoding: string };
      expect(outputFormat.sample_rate).toBe(8000);
      // Encoding stays PCM — μ-law transcoding still happens client-side.
      expect(outputFormat.encoding).toBe('pcm_s16le');
    });

    it('forTwilio honours overrides (voice, language, speed) but pins sampleRate', async () => {
      const tts = CartesiaTTS.forTwilio('ct-key', {
        voice: 'custom-voice-id',
        language: 'es',
        speed: 'fast',
        sampleRate: 24000, // ignored — the factory wins
      } as unknown as Parameters<typeof CartesiaTTS.forTwilio>[1]);
      await tts.synthesize('hola');
      const outputFormat = lastBody?.output_format as { sample_rate: number };
      expect(outputFormat.sample_rate).toBe(8000);
      const voice = lastBody?.voice as { id: string };
      expect(voice.id).toBe('custom-voice-id');
      expect(lastBody?.language).toBe('es');
    });

    it('forTelnyx sets sampleRate to 16000', async () => {
      const tts = CartesiaTTS.forTelnyx('ct-key');
      await tts.synthesize('hello');
      const outputFormat = lastBody?.output_format as { sample_rate: number };
      expect(outputFormat.sample_rate).toBe(16000);
    });

    it('constructor default unchanged (16000)', async () => {
      const tts = new CartesiaTTS('ct-key');
      await tts.synthesize('hello');
      const outputFormat = lastBody?.output_format as { sample_rate: number };
      expect(outputFormat.sample_rate).toBe(16000);
    });
  });

  describe('pipeline-mode TTS class', () => {
    it('forTwilio (options-object form) sets sampleRate to 8000', async () => {
      const tts = CartesiaPipelineTTS.forTwilio({ apiKey: 'ct-key' });
      await tts.synthesize('hello');
      const outputFormat = lastBody?.output_format as { sample_rate: number };
      expect(outputFormat.sample_rate).toBe(8000);
    });

    it('forTwilio (positional form) is parent-compatible', async () => {
      const tts = CartesiaPipelineTTS.forTwilio('ct-key');
      await tts.synthesize('hello');
      const outputFormat = lastBody?.output_format as { sample_rate: number };
      expect(outputFormat.sample_rate).toBe(8000);
    });

    it('forTelnyx (options-object form) sets sampleRate to 16000', async () => {
      const tts = CartesiaPipelineTTS.forTelnyx({ apiKey: 'ct-key' });
      await tts.synthesize('hello');
      const outputFormat = lastBody?.output_format as { sample_rate: number };
      expect(outputFormat.sample_rate).toBe(16000);
    });

    it('forTwilio falls back to CARTESIA_API_KEY env var', async () => {
      const prev = process.env.CARTESIA_API_KEY;
      process.env.CARTESIA_API_KEY = 'env-key';
      try {
        const tts = CartesiaPipelineTTS.forTwilio();
        await tts.synthesize('hello');
        const outputFormat = lastBody?.output_format as { sample_rate: number };
        expect(outputFormat.sample_rate).toBe(8000);
      } finally {
        if (prev === undefined) delete process.env.CARTESIA_API_KEY;
        else process.env.CARTESIA_API_KEY = prev;
      }
    });

    it('forTwilio without apiKey or env throws', () => {
      const prev = process.env.CARTESIA_API_KEY;
      delete process.env.CARTESIA_API_KEY;
      try {
        expect(() => CartesiaPipelineTTS.forTwilio()).toThrow(/CARTESIA_API_KEY/);
      } finally {
        if (prev !== undefined) process.env.CARTESIA_API_KEY = prev;
      }
    });
  });
});

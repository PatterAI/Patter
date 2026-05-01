/**
 * Tests for ElevenLabsTTS — synthesize, synthesizeStream, error handling.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ElevenLabsTTS } from '../../src/providers/elevenlabs-tts';

describe('ElevenLabsTTS', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('constructor', () => {
    it('creates with required apiKey', () => {
      const tts = new ElevenLabsTTS('el-key');
      expect(tts).toBeDefined();
    });

    it('accepts custom voiceId, modelId, and outputFormat', () => {
      const tts = new ElevenLabsTTS('el-key', 'custom-voice', 'eleven_v2', 'pcm_24000');
      expect(tts).toBeDefined();
    });

    it('accepts the typed eleven_v3 model literal', () => {
      const tts = new ElevenLabsTTS('el-key', { modelId: 'eleven_v3' });
      expect(tts).toBeDefined();
    });

    it('defaults outputFormat to pcm_16000 for non-telephony usage', () => {
      const tts = new ElevenLabsTTS('el-key');
      // Tests reach into the private field deliberately — the public API
      // surface is the streaming endpoint, but the internal codec choice
      // is what carrier-side optimisations key off of.
      expect((tts as unknown as { outputFormat: string }).outputFormat).toBe('pcm_16000');
    });
  });

  describe('telephony factories', () => {
    it('forTwilio emits ulaw_8000 natively', () => {
      const tts = ElevenLabsTTS.forTwilio('el-key');
      expect((tts as unknown as { outputFormat: string }).outputFormat).toBe('ulaw_8000');
    });

    it('forTwilio applies low-bandwidth-friendly voice settings by default', () => {
      const tts = ElevenLabsTTS.forTwilio('el-key');
      const settings = (tts as unknown as { voiceSettings: Record<string, unknown> }).voiceSettings;
      expect(settings).toBeDefined();
      expect(settings.use_speaker_boost).toBe(false);
    });

    it('forTwilio honours caller overrides', () => {
      const custom = { stability: 0.3, use_speaker_boost: true };
      const tts = ElevenLabsTTS.forTwilio('el-key', {
        voiceId: 'rachel',
        modelId: 'eleven_v3',
        voiceSettings: custom,
      });
      const internal = tts as unknown as {
        outputFormat: string;
        voiceId: string;
        modelId: string;
        voiceSettings: typeof custom;
      };
      expect(internal.outputFormat).toBe('ulaw_8000');
      expect(internal.modelId).toBe('eleven_v3');
      expect(internal.voiceId).toBe('21m00Tcm4TlvDq8ikWAM');
      expect(internal.voiceSettings).toBe(custom);
    });

    it('forTelnyx emits pcm_16000 natively', () => {
      const tts = ElevenLabsTTS.forTelnyx('el-key');
      expect((tts as unknown as { outputFormat: string }).outputFormat).toBe('pcm_16000');
    });
  });

  describe('synthesizeStream()', () => {
    it('yields audio chunks from streaming response', async () => {
      const chunk1 = new Uint8Array([1, 2, 3]);
      const chunk2 = new Uint8Array([4, 5, 6]);

      let readCount = 0;
      const mockReader = {
        read: vi.fn().mockImplementation(async () => {
          readCount++;
          if (readCount === 1) return { done: false, value: chunk1 };
          if (readCount === 2) return { done: false, value: chunk2 };
          return { done: true, value: undefined };
        }),
        releaseLock: vi.fn(),
      };

      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: { getReader: () => mockReader },
      } as unknown as Response);

      const tts = new ElevenLabsTTS('el-key');
      const chunks: Buffer[] = [];
      for await (const chunk of tts.synthesizeStream('Hello')) {
        chunks.push(chunk);
      }

      expect(chunks).toHaveLength(2);
      expect(chunks[0]).toEqual(Buffer.from(chunk1));
      expect(chunks[1]).toEqual(Buffer.from(chunk2));
      expect(mockReader.releaseLock).toHaveBeenCalledOnce();
    });

    it('throws on non-OK response', async () => {
      fetchSpy.mockResolvedValueOnce({
        ok: false,
        status: 429,
        text: async () => 'Rate limited',
        body: null,
      } as unknown as Response);

      const tts = new ElevenLabsTTS('el-key');
      const gen = tts.synthesizeStream('Hello');

      await expect(gen.next()).rejects.toThrow('ElevenLabs TTS error 429');
    });

    it('throws when response has no body', async () => {
      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: null,
      } as unknown as Response);

      const tts = new ElevenLabsTTS('el-key');
      const gen = tts.synthesizeStream('Hello');

      await expect(gen.next()).rejects.toThrow('no response body');
    });

    it('skips empty chunks', async () => {
      let readCount = 0;
      const mockReader = {
        read: vi.fn().mockImplementation(async () => {
          readCount++;
          if (readCount === 1) return { done: false, value: new Uint8Array(0) };
          if (readCount === 2) return { done: false, value: new Uint8Array([1, 2]) };
          return { done: true, value: undefined };
        }),
        releaseLock: vi.fn(),
      };

      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: { getReader: () => mockReader },
      } as unknown as Response);

      const tts = new ElevenLabsTTS('el-key');
      const chunks: Buffer[] = [];
      for await (const chunk of tts.synthesizeStream('Hello')) {
        chunks.push(chunk);
      }

      expect(chunks).toHaveLength(1);
    });
  });

  describe('synthesize()', () => {
    it('returns concatenated audio buffer from all chunks', async () => {
      const chunk1 = new Uint8Array([1, 2, 3]);
      const chunk2 = new Uint8Array([4, 5, 6]);

      let readCount = 0;
      const mockReader = {
        read: vi.fn().mockImplementation(async () => {
          readCount++;
          if (readCount === 1) return { done: false, value: chunk1 };
          if (readCount === 2) return { done: false, value: chunk2 };
          return { done: true, value: undefined };
        }),
        releaseLock: vi.fn(),
      };

      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: { getReader: () => mockReader },
      } as unknown as Response);

      const tts = new ElevenLabsTTS('el-key');
      const result = await tts.synthesize('Hello');

      expect(result).toEqual(Buffer.from([1, 2, 3, 4, 5, 6]));
    });
  });
});

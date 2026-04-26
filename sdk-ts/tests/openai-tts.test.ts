import { describe, it, expect, vi, beforeEach } from 'vitest';
import { OpenAITTS } from '../src/providers/openai-tts';

describe('OpenAITTS', () => {
  it('initializes with default model and voice', () => {
    const tts = new OpenAITTS('sk-test');
    expect(tts).toBeDefined();
  });

  it('accepts custom voice and model', () => {
    const tts = new OpenAITTS('sk-test', 'nova', 'tts-1-hd');
    expect(tts).toBeDefined();
  });

  describe('resample24kTo16k', () => {
    it('returns empty buffer when input is a single odd byte (flushed later)', () => {
      // Legacy wrapper: in the new streaming resampler a single trailing
      // byte is a ``carry`` that would be consumed by the next chunk; the
      // non-streaming helper flushes only complete samples and therefore
      // returns an empty buffer for a 1-byte input.
      const input = Buffer.from([0x01]);
      const result = OpenAITTS.resample24kTo16k(input);
      expect(result.length).toBe(0);
    });

    it('returns empty buffer for empty input', () => {
      const input = Buffer.alloc(0);
      const result = OpenAITTS.resample24kTo16k(input);
      expect(result).toEqual(input);
    });

    it('resamples 3 samples to 2 samples', () => {
      // 3 PCM16-LE samples at 24kHz -> 2 samples at 16kHz
      const input = Buffer.alloc(6);
      input.writeInt16LE(100, 0);   // sample 0
      input.writeInt16LE(200, 2);   // sample 1
      input.writeInt16LE(300, 4);   // sample 2

      const result = OpenAITTS.resample24kTo16k(input);

      // Output should be 2 samples (4 bytes)
      expect(result.length).toBe(4);
      // First sample kept as-is
      expect(result.readInt16LE(0)).toBe(100);
      // Second sample = average of samples 1 and 2 = (200 + 300) / 2 = 250
      expect(result.readInt16LE(2)).toBe(250);
    });

    it('resamples 6 samples to 4 samples', () => {
      const input = Buffer.alloc(12);
      input.writeInt16LE(100, 0);
      input.writeInt16LE(200, 2);
      input.writeInt16LE(300, 4);
      input.writeInt16LE(400, 6);
      input.writeInt16LE(500, 8);
      input.writeInt16LE(600, 10);

      const result = OpenAITTS.resample24kTo16k(input);

      expect(result.length).toBe(8);
      // Group 1: keep sample 0, avg(sample 1, sample 2)
      expect(result.readInt16LE(0)).toBe(100);
      expect(result.readInt16LE(2)).toBe(250);
      // Group 2: keep sample 3, avg(sample 4, sample 5)
      expect(result.readInt16LE(4)).toBe(400);
      expect(result.readInt16LE(6)).toBe(550);
    });

    it('handles negative samples correctly', () => {
      const input = Buffer.alloc(6);
      input.writeInt16LE(-1000, 0);
      input.writeInt16LE(-2000, 2);
      input.writeInt16LE(-3000, 4);

      const result = OpenAITTS.resample24kTo16k(input);

      expect(result.length).toBe(4);
      expect(result.readInt16LE(0)).toBe(-1000);
      expect(result.readInt16LE(2)).toBe(-2500);
    });

    it('handles non-multiple-of-3 sample counts', () => {
      // 4 samples: group of 3 + 1 leftover
      const input = Buffer.alloc(8);
      input.writeInt16LE(100, 0);
      input.writeInt16LE(200, 2);
      input.writeInt16LE(300, 4);
      input.writeInt16LE(400, 6);

      const result = OpenAITTS.resample24kTo16k(input);

      // Group 1 (samples 0,1,2) -> 2 output samples
      // Leftover (sample 3) -> 1 output sample
      expect(result.length).toBe(6);
      expect(result.readInt16LE(0)).toBe(100);
      expect(result.readInt16LE(2)).toBe(250);
      expect(result.readInt16LE(4)).toBe(400);
    });

    it('handles 2 samples (no third for interpolation)', () => {
      const input = Buffer.alloc(4);
      input.writeInt16LE(100, 0);
      input.writeInt16LE(200, 2);

      const result = OpenAITTS.resample24kTo16k(input);

      // sample 0 kept, sample 1 kept (no sample 2 to interpolate with)
      expect(result.length).toBe(4);
      expect(result.readInt16LE(0)).toBe(100);
      expect(result.readInt16LE(2)).toBe(200);
    });

    it('rounds to nearest integer on interpolation', () => {
      const input = Buffer.alloc(6);
      input.writeInt16LE(0, 0);
      input.writeInt16LE(1, 2);
      input.writeInt16LE(2, 4);

      const result = OpenAITTS.resample24kTo16k(input);

      expect(result.readInt16LE(0)).toBe(0);
      // (1 + 2) / 2 = 1.5 -> Math.round -> 2 (banker's rounding rule
      // does not apply; JS Math.round always rounds .5 up toward +Inf).
      // Switched from Math.trunc to avoid ~0.5 LSB DC bias on long streams.
      expect(result.readInt16LE(2)).toBe(2);
    });
  });

  describe('synthesizeStream', () => {
    beforeEach(() => {
      vi.restoreAllMocks();
    });

    it('throws on non-ok response', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        text: async () => 'Unauthorized',
      }));

      const tts = new OpenAITTS('bad-key');
      const gen = tts.synthesizeStream('hello');

      await expect(gen.next()).rejects.toThrow('OpenAI TTS error 401: Unauthorized');

      vi.unstubAllGlobals();
    });

    it('throws when response body is null', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: null,
      }));

      const tts = new OpenAITTS('sk-test');
      const gen = tts.synthesizeStream('hello');

      await expect(gen.next()).rejects.toThrow('OpenAI TTS: no response body');

      vi.unstubAllGlobals();
    });

    it('yields resampled audio chunks from stream', async () => {
      // Create a fake 24kHz PCM chunk (6 bytes = 3 samples)
      const pcm24k = Buffer.alloc(6);
      pcm24k.writeInt16LE(100, 0);
      pcm24k.writeInt16LE(200, 2);
      pcm24k.writeInt16LE(300, 4);

      let readCount = 0;
      const mockReader = {
        read: vi.fn().mockImplementation(async () => {
          if (readCount === 0) {
            readCount++;
            return { done: false, value: new Uint8Array(pcm24k) };
          }
          return { done: true, value: undefined };
        }),
        releaseLock: vi.fn(),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: { getReader: () => mockReader },
      }));

      // antiAlias: false preserves bit-exact downsample-only behaviour
      // so the fixture values below remain valid.
      const tts = new OpenAITTS('sk-test', 'alloy', 'gpt-4o-mini-tts', null, null, false);
      const chunks: Buffer[] = [];
      for await (const chunk of tts.synthesizeStream('hello')) {
        chunks.push(chunk);
      }

      expect(chunks.length).toBe(1);
      // Resampled: 3 samples -> 2 samples (4 bytes)
      expect(chunks[0].length).toBe(4);
      expect(chunks[0].readInt16LE(0)).toBe(100);
      expect(chunks[0].readInt16LE(2)).toBe(250);
      expect(mockReader.releaseLock).toHaveBeenCalled();

      vi.unstubAllGlobals();
    });
  });

  describe('synthesize', () => {
    beforeEach(() => {
      vi.restoreAllMocks();
    });

    it('returns concatenated buffer from all chunks', async () => {
      const chunk1 = Buffer.alloc(6);
      chunk1.writeInt16LE(100, 0);
      chunk1.writeInt16LE(200, 2);
      chunk1.writeInt16LE(300, 4);

      const chunk2 = Buffer.alloc(6);
      chunk2.writeInt16LE(400, 0);
      chunk2.writeInt16LE(500, 2);
      chunk2.writeInt16LE(600, 4);

      let readCount = 0;
      const mockReader = {
        read: vi.fn().mockImplementation(async () => {
          if (readCount === 0) {
            readCount++;
            return { done: false, value: new Uint8Array(chunk1) };
          }
          if (readCount === 1) {
            readCount++;
            return { done: false, value: new Uint8Array(chunk2) };
          }
          return { done: true, value: undefined };
        }),
        releaseLock: vi.fn(),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: { getReader: () => mockReader },
      }));

      const tts = new OpenAITTS('sk-test');
      const result = await tts.synthesize('hello');

      // 2 chunks each 3 samples -> 2 resampled chunks of 2 samples each = 8 bytes total
      expect(result.length).toBe(8);

      vi.unstubAllGlobals();
    });
  });
});

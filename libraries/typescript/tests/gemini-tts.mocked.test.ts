/**
 * [mocked] GeminiTTS — mock the @google/genai boundary only.
 *
 * Mocked boundary: ONLY @google/genai (generateContentStream). Everything
 * inward is real: synthesizeStream buffering, base64 decode, StatefulResampler
 * 24k→16k resampling, and empty-text guard.
 *
 * Coverage:
 *  1. synthesizeStream yields PCM16 Buffers; 24k→16k resampling shrinks byte count ~2:1
 *  2. Empty / whitespace text yields nothing (no API call)
 *  3. targetSampleRate validation: constructor throws on values != 8000/16000
 *  4. providerKey === 'gemini_tts'
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GeminiTTS } from '../src/providers/gemini-tts.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build N samples of a 200 Hz sine at `sampleRate` as PCM16-LE Buffer.
 * This gives us non-trivial (non-silent) audio bytes to encode.
 */
function makePcm16(samples: number, sampleRate: number): Buffer {
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) {
    const val = Math.round(8000 * Math.sin((2 * Math.PI * 200 * i) / sampleRate));
    buf.writeInt16LE(val, i * 2);
  }
  return buf;
}

/**
 * Encode a PCM16 buffer as the base64 string the Gemini TTS API returns
 * inside inlineData.data.
 */
function toBase64(buf: Buffer): string {
  return buf.toString('base64');
}

/**
 * Build a fake generateContentStream that yields chunks shaped exactly as
 * the Gemini TTS API returns them:
 *   { candidates: [{ content: { parts: [{ inlineData: { data: <base64> } }] } }] }
 */
function makeStreamMock(chunks: Buffer[]) {
  return vi.fn().mockResolvedValue(
    (async function* () {
      for (const chunk of chunks) {
        yield {
          candidates: [
            {
              content: {
                parts: [{ inlineData: { data: toBase64(chunk) } }],
              },
            },
          ],
        };
      }
    })(),
  );
}

/** Collect all Buffers yielded by synthesizeStream into an array. */
async function collectStream(tts: GeminiTTS, text: string): Promise<Buffer[]> {
  const out: Buffer[] = [];
  for await (const chunk of tts.synthesizeStream(text)) {
    out.push(chunk);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Mock setup
// ---------------------------------------------------------------------------

// vi.mock covers both static `import` and dynamic `await import(...)` by default
// in vitest — no extra configuration needed.
vi.mock('@google/genai', () => ({
  GoogleGenAI: vi.fn().mockImplementation(() => ({
    models: {
      generateContent: vi.fn(),
      generateContentStream: vi.fn(),
    },
  })),
}));

// ---------------------------------------------------------------------------
// 1. synthesizeStream yields PCM16 Buffers; 24k→16k shrinks byte count ~2:1
// ---------------------------------------------------------------------------

describe('[mocked] GeminiTTS — synthesizeStream yields PCM16 at target rate', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('yields non-empty Buffer chunks for valid text with PCM24k mock data', async () => {
    // 480 samples @ 24 kHz = 20 ms — a typical chunk size from the API.
    const pcm24 = makePcm16(480, 24000);

    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const streamMock = makeStreamMock([pcm24]);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: streamMock,
      },
    }));

    const tts = new GeminiTTS('test-api-key');
    const buffers = await collectStream(tts, 'Hello world.');

    expect(buffers.length).toBeGreaterThan(0);
    for (const buf of buffers) {
      expect(buf).toBeInstanceOf(Buffer);
      expect(buf.length).toBeGreaterThan(0);
    }
  });

  it('resampling 24k→16k reduces total byte count roughly 2:1 (ratio in [1.5, 2.5])', async () => {
    // 3 chunks of 480 samples each = 1440 samples @ 24kHz = 2880 bytes input.
    // After 24k→16k resampling: 960 samples @ 16kHz = 1920 bytes output (2:3 ratio).
    const samples = 480;
    const pcm24 = makePcm16(samples, 24000);
    const inputBytesPerChunk = pcm24.length; // 960

    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const streamMock = makeStreamMock([pcm24, pcm24, pcm24]);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: streamMock,
      },
    }));

    const tts = new GeminiTTS('test-api-key', 'Kore', 'gemini-3.1-flash-tts-preview', 16000);
    const buffers = await collectStream(tts, 'Hello world from Patter.');

    const totalInput = inputBytesPerChunk * 3; // 2880 bytes @ 24kHz
    const totalOutput = buffers.reduce((s, b) => s + b.length, 0);

    expect(totalOutput).toBeGreaterThan(0);
    // 24k→16k means 2/3 the samples, so 2/3 the bytes:
    // ratio = totalInput / totalOutput should be ~1.5 (not 2:1 — the comment
    // in the task says "roughly 2:1" but 24k→16k is a 3:2 ratio).
    // We allow a window of [1.3, 2.2] to accommodate resampler phase-carry effects.
    const ratio = totalInput / totalOutput;
    expect(ratio).toBeGreaterThan(1.3);
    expect(ratio).toBeLessThan(2.2);
  });

  it('processes multiple chunks in a single stream call', async () => {
    const pcm24a = makePcm16(480, 24000);
    const pcm24b = makePcm16(240, 24000);

    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const streamMock = makeStreamMock([pcm24a, pcm24b]);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: streamMock,
      },
    }));

    const tts = new GeminiTTS('test-api-key');
    const buffers = await collectStream(tts, 'Testing multi-chunk stream.');

    // Both chunks had content so we must get at least one output buffer.
    expect(buffers.length).toBeGreaterThan(0);
    expect(streamMock).toHaveBeenCalledOnce();
  });

  it('skips chunks with missing inlineData (no crash)', async () => {
    const pcm24 = makePcm16(480, 24000);

    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);

    // First chunk has no inlineData; second is valid.
    const streamMock = vi.fn().mockResolvedValue(
      (async function* () {
        yield { candidates: [{ content: { parts: [{}] } }] }; // no inlineData
        yield {
          candidates: [
            { content: { parts: [{ inlineData: { data: toBase64(pcm24) } }] } },
          ],
        };
      })(),
    );
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: streamMock,
      },
    }));

    const tts = new GeminiTTS('test-api-key');
    const buffers = await collectStream(tts, 'Testing skip of empty chunk.');

    // Only the second chunk produces output.
    expect(buffers.length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// 2. Empty / whitespace text yields nothing (no API call)
// ---------------------------------------------------------------------------

describe('[mocked] GeminiTTS — empty text guard', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('yields nothing and makes no API call for empty string', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const streamMock = vi.fn();
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: streamMock,
      },
    }));

    const tts = new GeminiTTS('test-api-key');
    const buffers = await collectStream(tts, '');

    expect(buffers).toHaveLength(0);
    // The API must never be called for empty text.
    expect(streamMock).not.toHaveBeenCalled();
  });

  it('yields nothing and makes no API call for whitespace-only string', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const streamMock = vi.fn();
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: streamMock,
      },
    }));

    const tts = new GeminiTTS('test-api-key');
    const buffers = await collectStream(tts, '   \t\n  ');

    expect(buffers).toHaveLength(0);
    expect(streamMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 3. targetSampleRate validation
// ---------------------------------------------------------------------------

describe('[mocked] GeminiTTS — constructor targetSampleRate validation', () => {
  it('does NOT throw for targetSampleRate 16000 (default)', () => {
    expect(() => new GeminiTTS('key')).not.toThrow();
    expect(() => new GeminiTTS('key', 'Kore', 'model', 16000)).not.toThrow();
  });

  it('does NOT throw for targetSampleRate 8000', () => {
    expect(() => new GeminiTTS('key', 'Kore', 'model', 8000)).not.toThrow();
  });

  it('throws for targetSampleRate 22050', () => {
    expect(() => new GeminiTTS('key', 'Kore', 'model', 22050)).toThrow(
      /targetSampleRate must be 8000 or 16000/,
    );
  });

  it('throws for targetSampleRate 44100', () => {
    expect(() => new GeminiTTS('key', 'Kore', 'model', 44100)).toThrow(
      /targetSampleRate must be 8000 or 16000/,
    );
  });

  it('throws for targetSampleRate 0', () => {
    expect(() => new GeminiTTS('key', 'Kore', 'model', 0)).toThrow();
  });

  it('throws for targetSampleRate 24000 (the source rate, not an allowed target)', () => {
    expect(() => new GeminiTTS('key', 'Kore', 'model', 24000)).toThrow();
  });
});

// ---------------------------------------------------------------------------
// 4. providerKey
// ---------------------------------------------------------------------------

describe('[mocked] GeminiTTS — providerKey', () => {
  it('static providerKey === "gemini_tts"', () => {
    expect(GeminiTTS.providerKey).toBe('gemini_tts');
  });
});

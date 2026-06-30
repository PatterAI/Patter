/**
 * [mocked] GeminiSTT — mock the @google/genai boundary only.
 *
 * Mocked boundary: ONLY @google/genai (generateContent). Everything inward
 * is real: PCM buffering, WAV header wrapping, transcript emission, finalize,
 * close, and clone.
 *
 * Coverage:
 *  1. connect + sendAudio + finalize fires onTranscript with the model text,
 *     isFinal === true, and the model receives inlineData { mimeType: 'audio/wav' }
 *  2. finalize() with no buffered audio does NOT call the model
 *  3. close() flushes remaining buffered audio (transcribes once) then resolves
 *  4. clone() returns a fresh instance (different object), same args
 *  5. providerKey === 'gemini_stt'
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GeminiSTT, type Transcript } from '../src/providers/gemini-stt.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** 160 samples of PCM16 @ 16 kHz (10 ms). */
function makePcm16(samples = 160): Buffer {
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) {
    buf.writeInt16LE(Math.round(4000 * Math.sin((2 * Math.PI * 440 * i) / 16000)), i * 2);
  }
  return buf;
}

/**
 * Build a fake generateContent mock that returns `{ text: responseText }`.
 * The provider's extractText() checks `.text` first (direct field) before
 * falling through to candidates[].content.parts[].text — we use the direct
 * form which is simpler and avoids a fragile deeply-nested shape.
 */
function makeGenerateContentMock(responseText: string) {
  return vi.fn().mockResolvedValue({ text: responseText });
}

// ---------------------------------------------------------------------------
// Mock setup
// ---------------------------------------------------------------------------

// vi.mock covers both static imports and dynamic `await import(...)`.
vi.mock('@google/genai', () => ({
  GoogleGenAI: vi.fn().mockImplementation(() => ({
    models: {
      generateContent: vi.fn(),
      generateContentStream: vi.fn(),
    },
  })),
}));

// ---------------------------------------------------------------------------
// 1. connect + sendAudio + finalize fires onTranscript
// ---------------------------------------------------------------------------

describe('[mocked] GeminiSTT — connect + sendAudio + finalize', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('fires onTranscript with the model text verbatim after finalize()', async () => {
    const responseText = '[tone: weary] hello there';
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const gcMock = makeGenerateContentMock(responseText);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    const received: Transcript[] = [];
    stt.onTranscript((t) => received.push(t));

    await stt.connect();
    stt.sendAudio(makePcm16(480));
    stt.finalize();

    // Wait for the async transcribe() promise to settle.
    await new Promise<void>((r) => setTimeout(r, 50));

    expect(received).toHaveLength(1);
    expect(received[0].text).toBe(responseText);
    expect(received[0].isFinal).toBe(true);
    expect(received[0].confidence).toBe(1.0);

    await stt.close();
  });

  it('fires onTranscript with isFinal === true', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: makeGenerateContentMock('[tone: calm] yes please'),
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    const transcripts: Transcript[] = [];
    stt.onTranscript((t) => transcripts.push(t));

    await stt.connect();
    stt.sendAudio(makePcm16(320));
    stt.finalize();

    await new Promise<void>((r) => setTimeout(r, 50));

    expect(transcripts.length).toBeGreaterThan(0);
    expect(transcripts[0].isFinal).toBe(true);

    await stt.close();
  });

  it('passes inlineData audio part with mimeType === "audio/wav" to generateContent', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const gcMock = makeGenerateContentMock('[tone: neutral] okay');
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    stt.onTranscript(() => {});

    await stt.connect();
    stt.sendAudio(makePcm16(480));
    stt.finalize();

    await new Promise<void>((r) => setTimeout(r, 50));

    expect(gcMock).toHaveBeenCalledOnce();

    // Inspect the call argument: contents[0].parts must contain an inlineData part.
    const callArg = gcMock.mock.calls[0][0] as {
      contents: Array<{
        role: string;
        parts: Array<{ text?: string; inlineData?: { mimeType: string; data: string } }>;
      }>;
    };

    const parts = callArg.contents[0].parts;
    const inlineDataPart = parts.find((p) => p.inlineData !== undefined);

    expect(inlineDataPart).toBeDefined();
    expect(inlineDataPart!.inlineData!.mimeType).toBe('audio/wav');
    // data must be a non-empty base64 string (the WAV container + PCM).
    expect(typeof inlineDataPart!.inlineData!.data).toBe('string');
    expect(inlineDataPart!.inlineData!.data.length).toBeGreaterThan(0);

    await stt.close();
  });

  it('text passes through verbatim (no transformation by the adapter)', async () => {
    const verbatim = '[tone: excited] I need help right now!';
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: makeGenerateContentMock(verbatim),
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    const received: Transcript[] = [];
    stt.onTranscript((t) => received.push(t));

    await stt.connect();
    stt.sendAudio(makePcm16(160));
    stt.finalize();

    await new Promise<void>((r) => setTimeout(r, 50));

    expect(received[0].text).toBe(verbatim);

    await stt.close();
  });
});

// ---------------------------------------------------------------------------
// 2. finalize() with no buffered audio does NOT call the model
// ---------------------------------------------------------------------------

describe('[mocked] GeminiSTT — finalize with no audio', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('does NOT call generateContent when finalize() is called with no buffered audio', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const gcMock = vi.fn();
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    await stt.connect();
    stt.finalize(); // nothing buffered

    await new Promise<void>((r) => setTimeout(r, 50));

    expect(gcMock).not.toHaveBeenCalled();

    await stt.close();
  });

  it('does NOT emit a transcript when finalize() is called with no buffered audio', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    const transcripts: Transcript[] = [];
    stt.onTranscript((t) => transcripts.push(t));
    await stt.connect();
    stt.finalize();

    await new Promise<void>((r) => setTimeout(r, 50));

    expect(transcripts).toHaveLength(0);

    await stt.close();
  });

  it('sendAudio before connect() is ignored (running guard)', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const gcMock = vi.fn();
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    // send audio before connect() — should be silently ignored
    stt.sendAudio(makePcm16(320));
    stt.finalize();

    await new Promise<void>((r) => setTimeout(r, 50));

    // Nothing was buffered (running=false), so generateContent must not be called.
    expect(gcMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 3. close() flushes remaining buffered audio (transcribes once)
// ---------------------------------------------------------------------------

describe('[mocked] GeminiSTT — close() flushes buffered audio', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('close() transcribes once when audio is buffered but finalize() was never called', async () => {
    const closeText = '[tone: flat] goodbye';
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const gcMock = makeGenerateContentMock(closeText);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    const received: Transcript[] = [];
    stt.onTranscript((t) => received.push(t));

    await stt.connect();
    stt.sendAudio(makePcm16(640)); // buffered but not finalized

    await stt.close(); // close() must flush the buffer

    expect(gcMock).toHaveBeenCalledOnce();
    expect(received).toHaveLength(1);
    expect(received[0].text).toBe(closeText);
  });

  it('close() does NOT call generateContent when buffer is empty at close time', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const gcMock = vi.fn();
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    await stt.connect();
    // No audio sent.
    await stt.close();

    expect(gcMock).not.toHaveBeenCalled();
  });

  it('close() resolves after all pending transcriptions settle', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    // Simulate a slow transcription (~20 ms delay).
    const gcMock = vi.fn().mockImplementation(
      () => new Promise<{ text: string }>((r) => setTimeout(() => r({ text: '[tone: slow] done' }), 20)),
    );
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    const received: Transcript[] = [];
    stt.onTranscript((t) => received.push(t));

    await stt.connect();
    stt.sendAudio(makePcm16(320));

    // close() must await the pending transcription.
    await stt.close();

    // After close() resolves, the transcript must already have been received.
    expect(received).toHaveLength(1);
    expect(received[0].text).toBe('[tone: slow] done');
  });
});

// ---------------------------------------------------------------------------
// 4. clone() returns a fresh instance
// ---------------------------------------------------------------------------

describe('[mocked] GeminiSTT — clone()', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('clone() returns a different object (not the same reference)', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    const cloned = stt.clone();

    expect(cloned).not.toBe(stt);
  });

  it('clone() returns an instance of GeminiSTT', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: vi.fn(),
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    const cloned = stt.clone();

    expect(cloned).toBeInstanceOf(GeminiSTT);
  });

  it('clone() starts with a clean buffer (independent from the original)', async () => {
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    // Return a safe object so close() does not error when it flushes.
    const gcMock = vi.fn().mockResolvedValue({ text: '' });
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const stt = new GeminiSTT('test-api-key');
    // Clone BEFORE connect — neither the original nor the clone is connected.
    const cloned = stt.clone();

    // Close the clone without connecting — it should not trigger any API call
    // because the clone has no buffered audio and running=false.
    await cloned.close();
    expect(gcMock).not.toHaveBeenCalled();

    // The original is never connected either, so close() is a no-op.
    await stt.close();
  });

  it('clone() constructed with same ctorArgs works end-to-end', async () => {
    const cloneText = '[tone: neutral] cloned transcript';
    const { GoogleGenAI } = await import('@google/genai');
    const mockGenAI = vi.mocked(GoogleGenAI);
    const gcMock = makeGenerateContentMock(cloneText);
    mockGenAI.mockImplementation(() => ({
      models: {
        generateContent: gcMock,
        generateContentStream: vi.fn(),
      },
    }));

    const original = new GeminiSTT('test-api-key', 'gemini-2.5-flash', 16000);
    const cloned = original.clone();

    const received: Transcript[] = [];
    cloned.onTranscript((t) => received.push(t));

    await cloned.connect();
    cloned.sendAudio(makePcm16(480));
    cloned.finalize();

    await new Promise<void>((r) => setTimeout(r, 50));

    expect(received).toHaveLength(1);
    expect(received[0].text).toBe(cloneText);

    await cloned.close();
  });
});

// ---------------------------------------------------------------------------
// 5. providerKey
// ---------------------------------------------------------------------------

describe('[mocked] GeminiSTT — providerKey', () => {
  it('static providerKey === "gemini_stt"', () => {
    expect(GeminiSTT.providerKey).toBe('gemini_stt');
  });
});

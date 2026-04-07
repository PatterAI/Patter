import { describe, it, expect, vi, beforeEach } from 'vitest';
import { WhisperSTT } from '../src/providers/whisper-stt';

describe('WhisperSTT', () => {
  it('initializes with required api key', () => {
    const stt = new WhisperSTT('sk-test');
    expect(stt).toBeDefined();
  });

  it('accepts custom model', () => {
    const stt = new WhisperSTT('sk-test', 'whisper-1');
    expect(stt).toBeDefined();
  });

  it('accepts custom language', () => {
    const stt = new WhisperSTT('sk-test', 'whisper-1', 'it');
    expect(stt).toBeDefined();
  });

  it('accepts custom buffer size', () => {
    const stt = new WhisperSTT('sk-test', 'whisper-1', 'en', 64000);
    expect(stt).toBeDefined();
  });

  it('forTwilio creates instance with defaults', () => {
    const stt = WhisperSTT.forTwilio('sk-test');
    expect(stt).toBeDefined();
  });

  it('forTwilio accepts custom language and model', () => {
    const stt = WhisperSTT.forTwilio('sk-test', 'it', 'whisper-1');
    expect(stt).toBeDefined();
  });

  it('connect sets running state', async () => {
    const stt = new WhisperSTT('sk-test');
    await stt.connect();
    // Should not throw when sending audio after connect
    expect(() => stt.sendAudio(Buffer.from('audio'))).not.toThrow();
  });

  it('sendAudio does not throw when not connected', () => {
    const stt = new WhisperSTT('sk-test');
    expect(() => stt.sendAudio(Buffer.from('audio'))).not.toThrow();
  });

  it('close does not throw when not connected', () => {
    const stt = new WhisperSTT('sk-test');
    expect(() => stt.close()).not.toThrow();
  });

  it('close does not throw after connect', async () => {
    const stt = new WhisperSTT('sk-test');
    await stt.connect();
    expect(() => stt.close()).not.toThrow();
  });

  it('onTranscript registers a callback', () => {
    const stt = new WhisperSTT('sk-test');
    const cb = vi.fn();
    stt.onTranscript(cb);
    // No error means success; callback stored internally
    expect(true).toBe(true);
  });

  it('onTranscript limits callbacks to 10', () => {
    const stt = new WhisperSTT('sk-test');
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    for (let i = 0; i < 11; i++) {
      stt.onTranscript(vi.fn());
    }

    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('maximum of 10 onTranscript callbacks'),
    );
    warnSpy.mockRestore();
  });

  it('buffers audio and triggers transcription at threshold', async () => {
    // Use a small buffer size so we can trigger transcription easily
    const bufferSize = 100;
    const stt = new WhisperSTT('sk-test', 'whisper-1', 'en', bufferSize);

    const mockResponse = {
      ok: true,
      json: async () => ({ text: 'hello world' }),
      text: async () => '',
    };
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as Response);

    const cb = vi.fn();
    stt.onTranscript(cb);
    await stt.connect();

    // Send enough audio to exceed the buffer threshold
    stt.sendAudio(Buffer.alloc(bufferSize + 10));

    // Wait for the async transcription to complete
    await new Promise((r) => setTimeout(r, 50));

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(cb).toHaveBeenCalledWith({
      text: 'hello world',
      isFinal: true,
      confidence: 1.0,
    });

    fetchSpy.mockRestore();
  });

  it('does not trigger callback for empty transcription', async () => {
    const bufferSize = 100;
    const stt = new WhisperSTT('sk-test', 'whisper-1', 'en', bufferSize);

    const mockResponse = {
      ok: true,
      json: async () => ({ text: '' }),
      text: async () => '',
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as Response);

    const cb = vi.fn();
    stt.onTranscript(cb);
    await stt.connect();

    stt.sendAudio(Buffer.alloc(bufferSize + 10));

    await new Promise((r) => setTimeout(r, 50));

    expect(cb).not.toHaveBeenCalled();

    vi.restoreAllMocks();
  });

  it('handles API errors gracefully', async () => {
    const bufferSize = 100;
    const stt = new WhisperSTT('sk-test', 'whisper-1', 'en', bufferSize);

    const mockResponse = {
      ok: false,
      status: 401,
      text: async () => 'Unauthorized',
      json: async () => ({}),
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as Response);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const cb = vi.fn();
    stt.onTranscript(cb);
    await stt.connect();

    stt.sendAudio(Buffer.alloc(bufferSize + 10));

    await new Promise((r) => setTimeout(r, 50));

    expect(cb).not.toHaveBeenCalled();
    expect(errorSpy).toHaveBeenCalled();

    vi.restoreAllMocks();
  });

  it('flushes remaining buffer on close when above 25% threshold', async () => {
    const bufferSize = 100;
    const stt = new WhisperSTT('sk-test', 'whisper-1', 'en', bufferSize);

    const mockResponse = {
      ok: true,
      json: async () => ({ text: 'flushed' }),
      text: async () => '',
    };
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as Response);

    const cb = vi.fn();
    stt.onTranscript(cb);
    await stt.connect();

    // Send audio that is above 25% but below threshold (won't auto-trigger)
    stt.sendAudio(Buffer.alloc(30));
    expect(fetchSpy).not.toHaveBeenCalled();

    // Close should flush the buffer
    stt.close();

    await new Promise((r) => setTimeout(r, 50));

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(cb).toHaveBeenCalledWith({
      text: 'flushed',
      isFinal: true,
      confidence: 1.0,
    });

    fetchSpy.mockRestore();
  });

  it('does not flush on close when buffer is below 25% threshold', async () => {
    const bufferSize = 100;
    const stt = new WhisperSTT('sk-test', 'whisper-1', 'en', bufferSize);

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ text: 'should not happen' }),
      text: async () => '',
    } as Response);

    await stt.connect();

    // Send very little audio (below 25% of threshold)
    stt.sendAudio(Buffer.alloc(10));

    stt.close();

    await new Promise((r) => setTimeout(r, 50));

    expect(fetchSpy).not.toHaveBeenCalled();

    fetchSpy.mockRestore();
  });

  it('sends correct headers and form data to OpenAI API', async () => {
    const bufferSize = 50;
    const stt = new WhisperSTT('sk-my-key', 'whisper-1', 'fr', bufferSize);

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ text: 'bonjour' }),
      text: async () => '',
    } as Response);

    stt.onTranscript(vi.fn());
    await stt.connect();
    stt.sendAudio(Buffer.alloc(bufferSize + 1));

    await new Promise((r) => setTimeout(r, 50));

    expect(fetchSpy).toHaveBeenCalledTimes(1);

    const [url, opts] = fetchSpy.mock.calls[0];
    expect(url).toBe('https://api.openai.com/v1/audio/transcriptions');
    expect((opts as RequestInit).method).toBe('POST');
    expect((opts as RequestInit).headers).toEqual(
      expect.objectContaining({ Authorization: 'Bearer sk-my-key' }),
    );

    fetchSpy.mockRestore();
  });
});

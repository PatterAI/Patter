const OPENAI_TTS_URL = 'https://api.openai.com/v1/audio/speech';

export class OpenAITTS {
  constructor(
    private readonly apiKey: string,
    private readonly voice: string = 'alloy',
    private readonly model: string = 'tts-1',
  ) {}

  /**
   * Synthesise text to speech and return the full audio as a single Buffer.
   *
   * For large chunks (or when latency matters) call `synthesizeStream` instead.
   */
  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /**
   * Synthesise text and yield audio chunks as they arrive (streaming).
   *
   * OpenAI returns 24 kHz PCM16; each chunk is resampled to 16 kHz before
   * yielding so the output is ready for telephony pipelines.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(OPENAI_TTS_URL, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${this.apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: this.model,
        input: text,
        voice: this.voice,
        response_format: 'pcm',
      }),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`OpenAI TTS error ${response.status}: ${body}`);
    }

    if (!response.body) {
      throw new Error('OpenAI TTS: no response body');
    }

    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          yield OpenAITTS.resample24kTo16k(Buffer.from(value));
        }
      }
    } finally {
      if (typeof reader.cancel === 'function') await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  /**
   * Resample 24 kHz PCM16-LE to 16 kHz by taking 2 out of every 3 samples.
   *
   * For each group of 3 input samples the first is kept as-is and the second
   * output sample is the average of input samples 2 and 3.  This matches the
   * Python SDK implementation.
   */
  static resample24kTo16k(audio: Buffer): Buffer {
    if (audio.length < 2) return audio;

    const sampleCount = Math.floor(audio.length / 2);
    const samples = new Int16Array(sampleCount);
    for (let i = 0; i < sampleCount; i++) {
      samples[i] = audio.readInt16LE(i * 2);
    }

    const resampled: number[] = [];
    for (let i = 0; i < samples.length; i += 3) {
      resampled.push(samples[i]);
      if (i + 1 < samples.length) {
        if (i + 2 < samples.length) {
          // Interpolate between sample i+1 and i+2
          resampled.push(Math.trunc((samples[i + 1] + samples[i + 2]) / 2));
        } else {
          resampled.push(samples[i + 1]);
        }
      }
    }

    const out = Buffer.alloc(resampled.length * 2);
    for (let i = 0; i < resampled.length; i++) {
      out.writeInt16LE(resampled[i], i * 2);
    }
    return out;
  }
}

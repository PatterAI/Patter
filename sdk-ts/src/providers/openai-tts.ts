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
   *
   * The resampler carries state (buffered samples + odd trailing byte)
   * between chunks — without that state cross-chunk sample alignment drifts
   * and the caller hears pops / dropped audio (BUG #23, mirror of the
   * Python `audioop.ratecv` fix).
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

    // Stateful resampler: keeps leftover samples + an odd trailing byte so
    // chunk N+1 continues the 3:2 cadence where chunk N stopped.
    const ctx = { carryByte: null as number | null, leftover: [] as number[] };

    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          const out = OpenAITTS.resampleStreaming(Buffer.from(value), ctx);
          if (out.length > 0) yield out;
        }
      }
      // Flush trailing leftover (≤2 samples) at stream end.
      if (ctx.leftover.length > 0) {
        const tail = Buffer.alloc(ctx.leftover.length * 2);
        for (let i = 0; i < ctx.leftover.length; i++) {
          tail.writeInt16LE(ctx.leftover[i], i * 2);
        }
        yield tail;
      }
    } finally {
      if (typeof reader.cancel === 'function') await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  /**
   * Streaming 24 kHz → 16 kHz resampler (PCM16-LE). Maintains cross-chunk
   * state so the 3:2 pattern doesn't reset at every network read.
   */
  static resampleStreaming(
    audio: Buffer,
    ctx: { carryByte: number | null; leftover: number[] },
  ): Buffer {
    // Prepend an odd trailing byte from the previous chunk (PCM16 = 2 B/sample).
    let buf: Buffer;
    if (ctx.carryByte !== null) {
      buf = Buffer.concat([Buffer.from([ctx.carryByte]), audio]);
      ctx.carryByte = null;
    } else {
      buf = audio;
    }
    if (buf.length % 2 === 1) {
      ctx.carryByte = buf[buf.length - 1];
      buf = buf.subarray(0, buf.length - 1);
    }
    if (buf.length === 0 && ctx.leftover.length === 0) {
      return Buffer.alloc(0);
    }

    const sampleCount = buf.length / 2;
    // Combine leftover samples from the previous chunk with the new ones.
    const samples: number[] = ctx.leftover.slice();
    for (let i = 0; i < sampleCount; i++) {
      samples.push(buf.readInt16LE(i * 2));
    }

    const out: number[] = [];
    let i = 0;
    // Process complete groups of 3 input samples → 2 output samples.
    while (i + 2 < samples.length) {
      out.push(samples[i]);
      out.push(Math.trunc((samples[i + 1] + samples[i + 2]) / 2));
      i += 3;
    }
    // Keep any unprocessed trailing samples (0, 1, or 2) for the next call.
    ctx.leftover = samples.slice(i);

    const buffer = Buffer.alloc(out.length * 2);
    for (let j = 0; j < out.length; j++) {
      buffer.writeInt16LE(out[j], j * 2);
    }
    return buffer;
  }

  /** @deprecated use {@link resampleStreaming} with persistent state. */
  static resample24kTo16k(audio: Buffer): Buffer {
    const ctx = { carryByte: null as number | null, leftover: [] as number[] };
    const out = OpenAITTS.resampleStreaming(audio, ctx);
    if (ctx.leftover.length === 0) return out;
    const tail = Buffer.alloc(ctx.leftover.length * 2);
    for (let i = 0; i < ctx.leftover.length; i++) {
      tail.writeInt16LE(ctx.leftover[i], i * 2);
    }
    return Buffer.concat([out, tail]);
  }
}

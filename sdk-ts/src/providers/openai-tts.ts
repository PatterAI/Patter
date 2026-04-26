const OPENAI_TTS_URL = 'https://api.openai.com/v1/audio/speech';
// OpenAI TTS models that accept a voice-direction ``instructions`` field.
// Older models (``tts-1``, ``tts-1-hd``) 400 if we include it.
const INSTRUCTIONS_PREFIX = 'gpt-4o-mini-tts';
// -3dB ~5-6 kHz at 24kHz input; balances anti-alias (Nyquist 8kHz of 16kHz output) vs sibilant preservation
const LPF_ALPHA = 0.78;

export class OpenAITTS {
  constructor(
    private readonly apiKey: string,
    private readonly voice: string = 'alloy',
    private readonly model: string = 'gpt-4o-mini-tts',
    private readonly instructions: string | null = null,
    private readonly speed: number | null = null,
    // Enable the anti-aliasing LPF ahead of the 3:2 decimation. When true
    // (the default), eliminates the high-frequency aliasing on sibilants
    // produced by the prior downsample-only path. Tests that need the
    // bit-exact downsample-only path can opt out with ``antiAlias: false``.
    private readonly antiAlias: boolean = true,
  ) {
    if (speed !== null && speed !== undefined && (speed < 0.25 || speed > 4.0)) {
      throw new Error('OpenAITTS: speed must be in [0.25, 4.0]');
    }
  }

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
   * OpenAI returns 24 kHz PCM16; each chunk is lowpass-filtered then
   * decimated 3:2 to 16 kHz before yielding so the output is ready for
   * telephony pipelines.
   *
   * The resampler carries state (filter memory + buffered samples + odd
   * trailing byte) between chunks so cross-chunk sample alignment and
   * filter phase don't reset on every network read.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const body: Record<string, unknown> = {
      model: this.model,
      input: text,
      voice: this.voice,
      response_format: 'pcm',
    };
    if (this.instructions !== null && this.model.startsWith(INSTRUCTIONS_PREFIX)) {
      body.instructions = this.instructions;
    }
    if (this.speed !== null) {
      body.speed = this.speed;
    }

    // No top-level ``AbortSignal.timeout`` — we rely on response-body stream
    // cancellation and the ``reader.cancel()`` in finally so a slow trickle
    // of TTS audio doesn't get killed 30 s in.
    const response = await fetch(OPENAI_TTS_URL, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${this.apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      const errBody = await response.text();
      throw new Error(`OpenAI TTS error ${response.status}: ${errBody}`);
    }

    if (!response.body) {
      throw new Error('OpenAI TTS: no response body');
    }

    // Stateful resampler: keeps the LPF memory, leftover samples, and an
    // odd trailing byte so chunk N+1 continues the 3:2 cadence where chunk
    // N stopped.
    const ctx: ResampleCtx = {
      carryByte: null,
      leftover: [],
      lpfPrev: 0,
      lpfEnabled: this.antiAlias,
    };

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
   * Streaming 24 kHz → 16 kHz resampler (PCM16-LE). Applies a single-pole
   * lowpass ahead of the 3:2 decimation and carries filter + sample state
   * across chunks so the cadence doesn't reset at every network read.
   *
   * ``ctx.lpfEnabled`` (default true on the streaming path, false for the
   * legacy static helper) controls whether the LPF is engaged — we keep
   * the helper bit-exact for the downsample-only tests while the real
   * streaming path gets anti-alias filtering.
   */
  static resampleStreaming(
    audio: Buffer,
    ctx: ResampleCtx,
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
    const samples: number[] = ctx.leftover.slice();
    // Optional single-pole IIR pre-filter: y[n] = α·x[n] + (1-α)·y[n-1].
    // Carried across chunks via ``ctx.lpfPrev``.
    const lpf = ctx.lpfEnabled !== false;
    let y = ctx.lpfPrev;
    for (let i = 0; i < sampleCount; i++) {
      const x = buf.readInt16LE(i * 2);
      if (lpf) {
        y = LPF_ALPHA * x + (1 - LPF_ALPHA) * y;
        let s = Math.round(y);
        if (s > 32767) s = 32767;
        else if (s < -32768) s = -32768;
        samples.push(s);
      } else {
        samples.push(x);
      }
    }
    if (lpf) ctx.lpfPrev = y;

    const out: number[] = [];
    let i = 0;
    // Process complete groups of 3 input samples → 2 output samples.
    while (i + 2 < samples.length) {
      out.push(samples[i]);
      // Math.round (not trunc) to avoid a ~0.5 LSB DC bias over long
      // voiced segments. trunc rounds toward zero, introducing an
      // asymmetric bias on sign crossings.
      out.push(Math.round((samples[i + 1] + samples[i + 2]) / 2));
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
    // Bit-exact legacy behaviour: downsample only, no LPF. The streaming
    // path uses the anti-aliased version.
    const ctx: ResampleCtx = { carryByte: null, leftover: [], lpfPrev: 0, lpfEnabled: false };
    const out = OpenAITTS.resampleStreaming(audio, ctx);
    if (ctx.leftover.length === 0) return out;
    const tail = Buffer.alloc(ctx.leftover.length * 2);
    for (let i = 0; i < ctx.leftover.length; i++) {
      tail.writeInt16LE(ctx.leftover[i], i * 2);
    }
    return Buffer.concat([out, tail]);
  }
}

export interface ResampleCtx {
  carryByte: number | null;
  leftover: number[];
  lpfPrev: number;
  /** Enable the single-pole lowpass ahead of decimation. Default true. */
  lpfEnabled?: boolean;
}

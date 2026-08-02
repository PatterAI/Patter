/**
 * Fish Audio Text-to-Speech provider — HTTP streaming `/v1/tts` endpoint.
 *
 * **Beta: validated against the Fish Audio API spec (docs.fish.audio); not yet
 * exercised against a live phone call.**
 *
 * Calls `POST https://api.fish.audio/v1/tts`, which streams the synthesised
 * audio back with chunked transfer encoding. The model is selected with a
 * `model:` request HEADER (not a body field), so one key serves `s2.1-pro`,
 * `s2-pro`, `s1` and the free tier without reconnecting.
 *
 * Default output is raw PCM16-LE @ 16 kHz (`format: "pcm"`) so chunks drop
 * straight into the Patter pipeline with no transcoding — the same choice made
 * for LMNT (`raw`) and Inworld (`PCM`).
 *
 * Model families (see {@link FishAudioModel}):
 * - `s2.1-pro`      — recommended production model. 83 languages, multi-speaker,
 *                     natural-language expression control via `[brackets]`.
 * - `s2.1-pro-free` — same model on the free tier. No time-to-first-audio
 *                     guarantee, so it is NOT the default here: a voice agent
 *                     that stalls mid-call is worse than one that costs money.
 * - `s2-pro`        — previous generation, ~100 ms time-to-first-audio. Also the
 *                     only S2 model reachable over the WebSocket transport
 *                     (see {@link FishAudioWebSocketTTS}).
 * - `s1`            — legacy. Emotion control uses `(parentheses)`.
 *
 * Credential: `FISH_AUDIO_API_KEY`. Auth is an `Authorization: Bearer <key>` header.
 *
 * **Telephony** — Fish emits linear PCM only (no native G.711), so the pipeline
 * always runs the μ-law encode. {@link FishAudioTTS.forTwilio} requests PCM
 * directly at 8 kHz so the resample step is skipped;
 * {@link FishAudioTTS.forTelnyx} keeps the 16 kHz pipeline rate.
 */

/** Fish Audio API origin — shared by the TTS, ASR and model-listing endpoints. */
export const FISH_AUDIO_API_ORIGIN = 'https://api.fish.audio';
/** HTTP streaming text-to-speech endpoint. */
export const FISH_AUDIO_TTS_URL = `${FISH_AUDIO_API_ORIGIN}/v1/tts`;
/** Voice-model listing — a free metadata read, used as the warmup target. */
export const FISH_AUDIO_MODELS_URL = `${FISH_AUDIO_API_ORIGIN}/model`;

/** Pipeline-friendly default sample rate (Hz). */
export const FISH_AUDIO_DEFAULT_SAMPLE_RATE = 16000;

/** Fish Audio TTS model ids, passed via the `model:` request header. */
export const FishAudioModel = {
  S2_1_PRO: 's2.1-pro',
  S2_1_PRO_FREE: 's2.1-pro-free',
  S2_PRO: 's2-pro',
  S1: 's1',
} as const;
export type FishAudioModel = (typeof FishAudioModel)[keyof typeof FishAudioModel];

/** Output container/codec. Only `PCM` is pipeline-compatible. */
export const FishAudioFormat = {
  PCM: 'pcm',
  WAV: 'wav',
  MP3: 'mp3',
  OPUS: 'opus',
} as const;
export type FishAudioFormat = (typeof FishAudioFormat)[keyof typeof FishAudioFormat];

/**
 * Latency/quality trade-off. `NORMAL` favours quality (~500 ms), `BALANCED` is
 * the interactive sweet spot (~300 ms) and is Patter's default, `LOW` trades
 * quality for the fastest possible first chunk.
 */
export const FishAudioLatency = {
  LOW: 'low',
  BALANCED: 'balanced',
  NORMAL: 'normal',
} as const;
export type FishAudioLatency = (typeof FishAudioLatency)[keyof typeof FishAudioLatency];

/** Constructor options for {@link FishAudioTTS}. */
export interface FishAudioTTSOptions {
  /** Model id. Defaults to `"s2.1-pro"`. */
  model?: FishAudioModel | string;
  /**
   * Fish `reference_id` — a voice-model id from the Fish voice library or one
   * you cloned. Omit to use the model's built-in voice. Pass an array for
   * multi-speaker synthesis and mark speakers inline with `<|speaker:0|>`.
   */
  voice?: string | readonly string[];
  /** Output format. Defaults to `"pcm"` (the only pipeline-compatible one). */
  format?: FishAudioFormat | string;
  /** Output sample rate in Hz (pcm/wav only). Defaults to 16000. */
  sampleRate?: number;
  /** Latency mode. Defaults to `"balanced"`. */
  latency?: FishAudioLatency | string;
  /** Speaking-rate multiplier in `[0.5, 2.0]`. */
  speed?: number;
  /** Volume adjustment in dB, `[-20, +20]`. */
  volume?: number;
  /** Loudness normalisation (S2-Pro only). */
  normalizeLoudness?: boolean;
  /** Expressiveness sampling temperature in `[0, 1]` (Fish default 0.7). */
  temperature?: number;
  /** Nucleus sampling in `[0, 1]` (Fish default 0.7). */
  topP?: number;
  /** Text segment size in `[100, 300]` (Fish default 300). */
  chunkLength?: number;
  /** Minimum chunk size in `[0, 100]` characters (Fish default 50). */
  minChunkLength?: number;
  /** Normalise English/Chinese text before synthesis (Fish default true). */
  normalize?: boolean;
  /** Max tokens generated per chunk (Fish default 1024). */
  maxNewTokens?: number;
  /** Repetition penalty (Fish default 1.2). */
  repetitionPenalty?: number;
  /** Condition each chunk on the previously generated audio (Fish default true). */
  conditionOnPreviousChunks?: boolean;
  /** Batch early-stop threshold in `[0, 1]` (Fish default 1). */
  earlyStopThreshold?: number;
  /** MP3 bitrate in kbps: 64 | 128 | 192. */
  mp3Bitrate?: number;
  /** Opus bitrate in bps: -1000 (auto) | 24000 | 32000 | 48000 | 64000. */
  opusBitrate?: number;
  /** Override the REST endpoint (e.g. for tests or an on-prem proxy). */
  baseUrl?: string;
}

/** Options for the carrier-specific factories — same minus `format`/`sampleRate`. */
export type FishAudioCarrierOptions = Omit<FishAudioTTSOptions, 'format' | 'sampleRate'>;

/** Fish Audio TTS adapter backed by the HTTP `/v1/tts` streaming endpoint. */
export class FishAudioTTS {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'fish_audio';

  protected readonly apiKey: string;
  readonly model: string;
  readonly voice?: string | readonly string[];
  readonly format: string;
  readonly sampleRate: number;
  readonly latency: string;
  private readonly speed?: number;
  private readonly volume?: number;
  private readonly normalizeLoudness?: boolean;
  private readonly temperature?: number;
  private readonly topP?: number;
  private readonly chunkLength?: number;
  private readonly minChunkLength?: number;
  private readonly normalize?: boolean;
  private readonly maxNewTokens?: number;
  private readonly repetitionPenalty?: number;
  private readonly conditionOnPreviousChunks?: boolean;
  private readonly earlyStopThreshold?: number;
  private readonly mp3Bitrate?: number;
  private readonly opusBitrate?: number;
  protected readonly baseUrl: string;

  constructor(apiKey: string, opts: FishAudioTTSOptions = {}) {
    if (!apiKey) {
      throw new Error('Fish Audio TTS: apiKey is required');
    }
    this.apiKey = apiKey;
    this.model = opts.model ?? FishAudioModel.S2_1_PRO;
    this.voice = opts.voice;
    this.format = opts.format ?? FishAudioFormat.PCM;
    this.sampleRate = opts.sampleRate ?? FISH_AUDIO_DEFAULT_SAMPLE_RATE;
    this.latency = opts.latency ?? FishAudioLatency.BALANCED;
    this.speed = opts.speed;
    this.volume = opts.volume;
    this.normalizeLoudness = opts.normalizeLoudness;
    this.temperature = opts.temperature;
    this.topP = opts.topP;
    this.chunkLength = opts.chunkLength;
    this.minChunkLength = opts.minChunkLength;
    this.normalize = opts.normalize;
    this.maxNewTokens = opts.maxNewTokens;
    this.repetitionPenalty = opts.repetitionPenalty;
    this.conditionOnPreviousChunks = opts.conditionOnPreviousChunks;
    this.earlyStopThreshold = opts.earlyStopThreshold;
    this.mp3Bitrate = opts.mp3Bitrate;
    this.opusBitrate = opts.opusBitrate;
    this.baseUrl = opts.baseUrl ?? FISH_AUDIO_TTS_URL;
  }

  /**
   * Declare the audio format this adapter emits so the pipeline sender derives
   * the correct resample ratio instead of assuming 16 kHz.
   *
   * Only `format: "pcm"` produces pipeline-consumable bytes; `wav` / `mp3` /
   * `opus` are container formats for offline use and are reported here as their
   * underlying PCM rate.
   */
  sourceAudioFormat(): { encoding: 'pcm_s16le' | 'mulaw' | 'alaw'; sampleRate: number } {
    return { encoding: 'pcm_s16le', sampleRate: this.sampleRate };
  }

  /**
   * Construct an instance pre-configured for Twilio Media Streams.
   *
   * Requests PCM directly at 8 kHz — the carrier wire rate — so the pipeline
   * skips the 16 kHz → 8 kHz resample and only runs the μ-law encode. Fish has
   * no native G.711 output, so a fully bit-clean passthrough is not available.
   */
  static forTwilio(apiKey: string, options: FishAudioCarrierOptions = {}): FishAudioTTS {
    return new FishAudioTTS(apiKey, {
      ...options,
      format: FishAudioFormat.PCM,
      sampleRate: 8000,
    });
  }

  /**
   * Construct an instance pre-configured for Telnyx bidirectional media.
   *
   * Emits PCM @ 16 kHz to match the Telnyx PCM16 pipeline; the SDK resamples to
   * the 8 kHz PCMU wire once, downstream.
   */
  static forTelnyx(apiKey: string, options: FishAudioCarrierOptions = {}): FishAudioTTS {
    return new FishAudioTTS(apiKey, {
      ...options,
      format: FishAudioFormat.PCM,
      sampleRate: FISH_AUDIO_DEFAULT_SAMPLE_RATE,
    });
  }

  /** Request headers. Fish selects the model with a header, not a body field. */
  protected buildHeaders(): Record<string, string> {
    return {
      Authorization: `Bearer ${this.apiKey}`,
      'Content-Type': 'application/json',
      model: this.model,
    };
  }

  /**
   * Build the `TTSRequest` body. Every optional knob is omitted when unset so
   * Fish applies its own documented default rather than a value we guessed.
   */
  protected buildPayload(text: string): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      text,
      format: this.format,
      latency: this.latency,
    };
    // `sample_rate` is only meaningful for the raw/uncompressed formats; mp3
    // and opus derive it from the bitrate instead.
    if (this.format === FishAudioFormat.PCM || this.format === FishAudioFormat.WAV) {
      payload.sample_rate = this.sampleRate;
    }
    if (this.voice !== undefined && this.voice.length > 0) {
      // string → single speaker; array → multi-speaker (s2 models).
      payload.reference_id = typeof this.voice === 'string' ? this.voice : [...this.voice];
    }

    const prosody: Record<string, unknown> = {};
    if (this.speed !== undefined) prosody.speed = this.speed;
    if (this.volume !== undefined) prosody.volume = this.volume;
    if (this.normalizeLoudness !== undefined) {
      prosody.normalize_loudness = this.normalizeLoudness;
    }
    if (Object.keys(prosody).length > 0) payload.prosody = prosody;

    const optional: Record<string, unknown> = {
      temperature: this.temperature,
      top_p: this.topP,
      chunk_length: this.chunkLength,
      min_chunk_length: this.minChunkLength,
      normalize: this.normalize,
      max_new_tokens: this.maxNewTokens,
      repetition_penalty: this.repetitionPenalty,
      condition_on_previous_chunks: this.conditionOnPreviousChunks,
      early_stop_threshold: this.earlyStopThreshold,
      mp3_bitrate: this.mp3Bitrate,
      opus_bitrate: this.opusBitrate,
    };
    for (const [key, value] of Object.entries(optional)) {
      if (value !== undefined) payload[key] = value;
    }
    return payload;
  }

  /**
   * Best-effort pre-call HTTP warmup.
   *
   * Issues `GET /model?page_size=1` — the voice-model listing — so DNS, TLS and
   * the HTTP connection are already established when the first synthesis POST
   * lands. Billing safety: listing voice models is a free metadata read;
   * synthesis is billed only by `POST /v1/tts`. A `HEAD` against `/v1/tts`
   * would be cheaper still, but that endpoint is POST-only and answers 405 —
   * the trap already documented on the Inworld adapter.
   */
  async warmup(): Promise<void> {
    try {
      await fetch(`${new URL(this.baseUrl).origin}/model?page_size=1`, {
        method: 'GET',
        headers: { Authorization: `Bearer ${this.apiKey}` },
        signal: AbortSignal.timeout(5_000),
      });
    } catch {
      // Best-effort: a failed warmup must never affect the call.
    }
  }

  /** Synthesize text and return the concatenated audio buffer. */
  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /**
   * Yield audio chunks as they arrive. With the default `format: "pcm"` these
   * are raw PCM16-LE bytes at the configured `sampleRate`.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(this.baseUrl, {
      method: 'POST',
      headers: this.buildHeaders(),
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(60_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Fish Audio TTS error ${response.status}: ${body}`);
    }
    if (!response.body) {
      throw new Error('Fish Audio TTS: no response body');
    }

    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          yield Buffer.from(value);
        }
      }
    } finally {
      if (typeof reader.cancel === 'function') await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  /** No persistent resources to release — present for interface parity. */
  async close(): Promise<void> {
    // fetch() owns its own connection pool; nothing to tear down.
  }
}

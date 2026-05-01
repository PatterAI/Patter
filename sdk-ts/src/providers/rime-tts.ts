/**
 * Rime TTS provider — HTTP chunked endpoint.
 *
 * Supports both Arcana and Mist model families. The Arcana model can take
 * up to ~80% of the output audio's duration to synthesize, so its request
 * timeout is bumped to 4 minutes.
 */

const RIME_BASE_URL = 'https://users.rime.ai/v1/rime-tts';

// Model-specific timeouts in milliseconds.
const ARCANA_MODEL_TIMEOUT_MS = 60 * 4 * 1000;
const MIST_MODEL_TIMEOUT_MS = 30 * 1000;

function isMistModel(model: string): boolean {
  return model.includes('mist');
}

function timeoutForModel(model: string): number {
  if (model === 'arcana') return ARCANA_MODEL_TIMEOUT_MS;
  return MIST_MODEL_TIMEOUT_MS;
}

export interface RimeTTSOptions {
  model?: string;
  speaker?: string;
  lang?: string;
  sampleRate?: number;
  // Arcana-only options
  repetitionPenalty?: number;
  temperature?: number;
  topP?: number;
  maxTokens?: number;
  // Mist-only options
  speedAlpha?: number;
  reduceLatency?: boolean;
  pauseBetweenBrackets?: boolean;
  phonemizeBetweenBrackets?: boolean;
  baseUrl?: string;
}

export class RimeTTS {
  private readonly apiKey: string;
  private readonly model: string;
  private readonly speaker: string;
  private readonly lang: string;
  private readonly sampleRate: number;
  private readonly repetitionPenalty?: number;
  private readonly temperature?: number;
  private readonly topP?: number;
  private readonly maxTokens?: number;
  private readonly speedAlpha?: number;
  private readonly reduceLatency?: boolean;
  private readonly pauseBetweenBrackets?: boolean;
  private readonly phonemizeBetweenBrackets?: boolean;
  private readonly baseUrl: string;
  private readonly totalTimeoutMs: number;

  constructor(apiKey: string, opts: RimeTTSOptions = {}) {
    this.apiKey = apiKey;
    this.model = opts.model ?? 'arcana';

    // Defaults: "cove" for Mist, "astra" for Arcana.
    const defaultSpeaker = isMistModel(this.model) ? 'cove' : 'astra';
    this.speaker = opts.speaker ?? defaultSpeaker;

    this.lang = opts.lang ?? 'eng';
    this.sampleRate = opts.sampleRate ?? 16000;
    this.repetitionPenalty = opts.repetitionPenalty;
    this.temperature = opts.temperature;
    this.topP = opts.topP;
    this.maxTokens = opts.maxTokens;
    this.speedAlpha = opts.speedAlpha;
    this.reduceLatency = opts.reduceLatency;
    this.pauseBetweenBrackets = opts.pauseBetweenBrackets;
    this.phonemizeBetweenBrackets = opts.phonemizeBetweenBrackets;
    this.baseUrl = opts.baseUrl ?? RIME_BASE_URL;
    this.totalTimeoutMs = timeoutForModel(this.model);
  }

  private buildPayload(text: string): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      speaker: this.speaker,
      text,
      modelId: this.model,
    };

    if (this.model === 'arcana') {
      if (this.repetitionPenalty !== undefined)
        payload.repetition_penalty = this.repetitionPenalty;
      if (this.temperature !== undefined) payload.temperature = this.temperature;
      if (this.topP !== undefined) payload.top_p = this.topP;
      if (this.maxTokens !== undefined) payload.max_tokens = this.maxTokens;
      payload.lang = this.lang;
      payload.samplingRate = this.sampleRate;
    } else if (isMistModel(this.model)) {
      payload.lang = this.lang;
      payload.samplingRate = this.sampleRate;
      if (this.speedAlpha !== undefined) payload.speedAlpha = this.speedAlpha;
      if (this.model === 'mistv2' && this.reduceLatency !== undefined) {
        payload.reduceLatency = this.reduceLatency;
      }
      if (this.pauseBetweenBrackets !== undefined) {
        payload.pauseBetweenBrackets = this.pauseBetweenBrackets;
      }
      if (this.phonemizeBetweenBrackets !== undefined) {
        payload.phonemizeBetweenBrackets = this.phonemizeBetweenBrackets;
      }
    }

    return payload;
  }

  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /**
   * Synthesize text and yield raw PCM_S16LE chunks at the configured
   * `sampleRate` as they stream in.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(this.baseUrl, {
      method: 'POST',
      headers: {
        accept: 'audio/pcm',
        Authorization: `Bearer ${this.apiKey}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(this.totalTimeoutMs),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Rime TTS error ${response.status}: ${body}`);
    }

    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.startsWith('audio')) {
      const body = await response.text();
      throw new Error(`Rime returned non-audio response: ${body.slice(0, 500)}`);
    }

    if (!response.body) {
      throw new Error('Rime TTS: no response body');
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
      if (typeof reader.cancel === 'function')
        await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}

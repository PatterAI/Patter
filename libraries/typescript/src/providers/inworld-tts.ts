/**
 * Inworld TTS provider — HTTP NDJSON streaming endpoint.
 *
 * Calls `POST https://api.inworld.ai/tts/v1/voice:stream`. The response is
 * NDJSON: one JSON object per line of the form
 *   `{"result": {"audioContent": "<base64-PCM_S16LE>", "timestampInfo": ...}}`
 *
 * The default config requests `audioEncoding=PCM` at 16 kHz so the output drops
 * straight into the Patter pipeline without transcoding. Inworld TTS-2 is the
 * default model — pass `model: "inworld-tts-1.5-max"` for the prior generation.
 */

const INWORLD_BASE_URL = "https://api.inworld.ai/tts/v1/voice:stream";

/** Inworld TTS model families. */
export const InworldModel = {
  TTS_2: "inworld-tts-2",
  TTS_1_5_MAX: "inworld-tts-1.5-max",
  TTS_1_5_MINI: "inworld-tts-1.5-mini",
  TTS_1_MAX: "inworld-tts-1-max",
  TTS_1: "inworld-tts-1",
} as const;
export type InworldModel = (typeof InworldModel)[keyof typeof InworldModel];

/** Inworld audio encoding values accepted by the REST API. */
export const InworldAudioEncoding = {
  PCM: "PCM",
  LINEAR16: "LINEAR16",
  OGG_OPUS: "OGG_OPUS",
  MP3: "MP3",
} as const;
export type InworldAudioEncoding =
  (typeof InworldAudioEncoding)[keyof typeof InworldAudioEncoding];

/** TTS-2 stability mode (ignored by older models). */
export const InworldDeliveryMode = {
  EXPRESSIVE: "EXPRESSIVE",
  BALANCED: "BALANCED",
  STABLE: "STABLE",
} as const;
export type InworldDeliveryMode =
  (typeof InworldDeliveryMode)[keyof typeof InworldDeliveryMode];

/** Constructor options for {@link InworldTTS}. */
export interface InworldTTSOptions {
  /** Model id. Defaults to `"inworld-tts-2"`. */
  model?: InworldModel | string;
  /** Voice name (e.g. `"Ashley"`, `"Olivia"`, `"Craig"`, `"Remy"`). */
  voice?: string;
  /** BCP-47 language tag, e.g. `"en"`, `"it"`, `"es"`. */
  language?: string;
  /** Output audio encoding. Defaults to `"PCM"` (raw PCM_S16LE). */
  audioEncoding?: InworldAudioEncoding | string;
  /** Output sample rate in Hz. Defaults to 16000. */
  sampleRate?: number;
  /** Bitrate hint (bits/sec) — used for OGG_OPUS / MP3. Default 64000. */
  bitrate?: number;
  /** Sampling temperature 0.0–2.0 (TTS-1.5 only — ignored by TTS-2). */
  temperature?: number;
  /** Speaking rate multiplier 0.5–1.5. Default 1.0. */
  speakingRate?: number;
  /** Stability mode for TTS-2 (`EXPRESSIVE` / `BALANCED` / `STABLE`). */
  deliveryMode?: InworldDeliveryMode | string;
  /** Override the REST endpoint (e.g. for on-prem deployments). */
  baseUrl?: string;
}

/**
 * Inworld TTS over the `/tts/v1/voice:stream` HTTP NDJSON endpoint.
 *
 * The Inworld dashboard provides a Base64 token that is already in the form
 * expected by the `Authorization: Basic <token>` header — pass it as-is. If
 * you only have the raw API key string, base64-encode `${apiKey}:` yourself
 * before calling the constructor.
 */
export class InworldTTS {
  private readonly authToken: string;
  private readonly model: string;
  private readonly voice: string;
  private readonly language?: string;
  private readonly audioEncoding: string;
  private readonly sampleRate: number;
  private readonly bitrate: number;
  private readonly temperature?: number;
  private readonly speakingRate: number;
  private readonly deliveryMode?: string;
  private readonly baseUrl: string;

  constructor(authToken: string, opts: InworldTTSOptions = {}) {
    if (!authToken) {
      throw new Error("Inworld TTS: authToken is required");
    }
    this.authToken = authToken;
    this.model = opts.model ?? InworldModel.TTS_2;
    this.voice = opts.voice ?? "Ashley";
    this.language = opts.language;
    this.audioEncoding = opts.audioEncoding ?? InworldAudioEncoding.PCM;
    this.sampleRate = opts.sampleRate ?? 16000;
    this.bitrate = opts.bitrate ?? 64000;
    this.temperature = opts.temperature;
    this.speakingRate = opts.speakingRate ?? 1.0;
    this.deliveryMode = opts.deliveryMode;
    this.baseUrl = opts.baseUrl ?? INWORLD_BASE_URL;
  }

  private buildPayload(text: string): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      text,
      voiceId: this.voice,
      modelId: this.model,
      audioConfig: {
        audioEncoding: this.audioEncoding,
        bitrate: this.bitrate,
        sampleRateHertz: this.sampleRate,
      },
      speakingRate: this.speakingRate,
    };
    if (this.language !== undefined) payload.language = this.language;
    if (this.temperature !== undefined) payload.temperature = this.temperature;
    if (this.deliveryMode !== undefined) payload.deliveryMode = this.deliveryMode;
    return payload;
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
   * Yield audio chunks as they arrive. With the default `audioEncoding=PCM`
   * these are raw PCM_S16LE bytes at `sampleRate`.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(this.baseUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Basic ${this.authToken}`,
      },
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(60_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Inworld TTS error ${response.status}: ${body}`);
    }
    if (!response.body) {
      throw new Error("Inworld TTS: no response body");
    }

    // NDJSON parser: feed bytes through a UTF-8 decoder, split on \n, yield
    // base64-decoded audioContent for each `result` line.
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffered = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          buffered += decoder.decode();
          break;
        }
        buffered += decoder.decode(value, { stream: true });
        let newlineIdx: number;
        while ((newlineIdx = buffered.indexOf("\n")) >= 0) {
          const line = buffered.slice(0, newlineIdx).trim();
          buffered = buffered.slice(newlineIdx + 1);
          if (!line) continue;
          const audio = decodeNdjsonLine(line);
          if (audio && audio.length > 0) yield audio;
        }
      }
      // Trailing line with no newline.
      const tail = buffered.trim();
      if (tail) {
        const audio = decodeNdjsonLine(tail);
        if (audio && audio.length > 0) yield audio;
      }
    } finally {
      if (typeof reader.cancel === "function")
        await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}

/**
 * Decode one NDJSON line emitted by `/tts/v1/voice:stream`. Returns
 * `null` when the line does not carry audio (e.g. a final timestamp-only
 * frame or a server-side error message we let the HTTP layer surface).
 */
function decodeNdjsonLine(line: string): Buffer | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const result = (parsed as { result?: { audioContent?: string } }).result;
  const audioB64 = result?.audioContent;
  if (typeof audioB64 !== "string" || audioB64.length === 0) return null;
  return Buffer.from(audioB64, "base64");
}

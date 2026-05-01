const ELEVENLABS_BASE_URL = 'https://api.elevenlabs.io/v1';

// Curated map of common ElevenLabs voice display names to their voice IDs.
// The public API only accepts voice IDs — callers that pass a human-readable
// name like "rachel" would otherwise hit 404. Mirrors the Python SDK map.
const ELEVENLABS_VOICE_ID_BY_NAME: Record<string, string> = {
  rachel: '21m00Tcm4TlvDq8ikWAM',
  drew: '29vD33N1CtxCmqQRPOHJ',
  clyde: '2EiwWnXFnvU5JabPnv8n',
  paul: '5Q0t7uMcjvnagumLfvZi',
  domi: 'AZnzlk1XvdvUeBnXmlld',
  dave: 'CYw3kZ02Hs0563khs1Fj',
  fin: 'D38z5RcWu1voky8WS1ja',
  bella: 'EXAVITQu4vr4xnSDxMaL',
  antoni: 'ErXwobaYiN019PkySvjV',
  thomas: 'GBv7mTt0atIp3Br8iCZE',
  charlie: 'IKne3meq5aSn9XLyUdCD',
  george: 'JBFqnCBsd6RMkjVDRZzb',
  emily: 'LcfcDJNUP1GQjkzn1xUU',
  elli: 'MF3mGyEYCl7XYWbV9V6O',
  callum: 'N2lVS1w4EtoT3dr4eOWO',
  patrick: 'ODq5zmih8GrVes37Dizd',
  harry: 'SOYHLrjzK2X1ezoPC6cr',
  liam: 'TX3LPaxmHKxFdv7VOQHJ',
  dorothy: 'ThT5KcBeYPX3keUQqHPh',
  josh: 'TxGEqnHWrfWFTfGW9XjX',
  arnold: 'VR6AewLTigWG4xSOukaG',
  charlotte: 'XB0fDUnXU5powFXDhCwa',
  matilda: 'XrExE9yKIg1WjnnlVkGX',
  matthew: 'Yko7PKHZNXotIFUBG7I9',
  james: 'ZQe5CZNOzWyzPSCn5a3c',
  joseph: 'Zlb1dXrM653N07WRdFW3',
  jeremy: 'bVMeCyTHy58xNoL34h3p',
  michael: 'flq6f7yk4E4fJM5XTYuZ',
  ethan: 'g5CIjZEefAph4nQFvHAz',
  gigi: 'jBpfuIE2acCO8z3wKNLl',
  freya: 'jsCqWAovK2LkecY7zXl4',
  brian: 'nPczCjzI2devNBz1zQrb',
  grace: 'oWAxZDx7w5VEj9dCyTzz',
  daniel: 'onwK4e9ZLuTAKqWW03F9',
  lily: 'pFZP5JQG7iQjIQuC4Bku',
  serena: 'pMsXgVXv3BLzUgSXRplE',
  adam: 'pNInz6obpgDQGcFmaJgB',
  nicole: 'piTKgcLEGmPE4e6mEKli',
  bill: 'pqHfZKP75CvOlQylNhV4',
  jessie: 't0jbNlBVZ17f02VDIeMI',
  ryan: 'wViXBPUzp2ZZixB1xQuM',
  sam: 'yoZ06aMxZJJ28mfd3POQ',
  glinda: 'z9fAnlkpzviPz146aGWa',
  giovanni: 'zcAOhNBS3c14rBihAFp1',
  mimi: 'zrHiDhphv9ZnVXBqCLjz',
  sarah: 'EXAVITQu4vr4xnSDxMaL',
  alloy: 'EXAVITQu4vr4xnSDxMaL',
};

const VOICE_ID_PATTERN = /^[A-Za-z0-9]{20}$/;

/**
 * Return an ElevenLabs voice ID from either a UUID-like ID or a display name.
 *
 * Opaque ElevenLabs voice IDs are 20-char alphanumeric tokens — anything
 * matching that shape is returned verbatim. Known display names (case-
 * insensitive) are resolved via the internal table. Unknown strings are
 * returned as-is so custom voices keep working.
 */
export function resolveVoiceId(voice: string): string {
  if (!voice) return voice;
  if (VOICE_ID_PATTERN.test(voice)) return voice;
  return ELEVENLABS_VOICE_ID_BY_NAME[voice.toLowerCase()] ?? voice;
}

/**
 * Known stable ElevenLabs voice models (from the official ElevenLabs API
 * reference). Provided as a string-literal union for autocomplete + type
 * narrowing; the public ``modelId`` option also accepts ``string`` so
 * users can pass forward-compat IDs we haven't enumerated yet.
 *
 * - ``eleven_v3`` — newest, highest quality (slower TTFT than Flash).
 * - ``eleven_flash_v2_5`` — current default, fastest (~75 ms TTFT).
 * - ``eleven_turbo_v2_5`` — balanced quality/speed.
 * - ``eleven_multilingual_v2`` — best multilingual support.
 * - ``eleven_monolingual_v1`` — legacy English-only.
 */
export type ElevenLabsModel =
  | 'eleven_v3'
  | 'eleven_flash_v2_5'
  | 'eleven_turbo_v2_5'
  | 'eleven_multilingual_v2'
  | 'eleven_monolingual_v1';

// Supported `output_format` values for the TTS stream endpoint.
// `ulaw_8000` is the telephony-ready option for Twilio/Telnyx.
export type ElevenLabsOutputFormat =
  | 'mp3_22050_32'
  | 'mp3_44100_32'
  | 'mp3_44100_64'
  | 'mp3_44100_96'
  | 'mp3_44100_128'
  | 'mp3_44100_192'
  | 'pcm_8000'
  | 'pcm_16000'
  | 'pcm_22050'
  | 'pcm_24000'
  | 'pcm_44100'
  | 'ulaw_8000';

export interface ElevenLabsVoiceSettings {
  stability?: number;
  similarity_boost?: number;
  style?: number;
  use_speaker_boost?: boolean;
}

export interface ElevenLabsTTSOptions {
  voiceId?: string;
  /**
   * ElevenLabs voice model ID. The default ``eleven_flash_v2_5`` has the
   * lowest TTFT (~75 ms). Pass ``eleven_v3`` for highest quality, or any
   * arbitrary string for forward-compat with future models.
   */
  modelId?: ElevenLabsModel | string;
  outputFormat?: ElevenLabsOutputFormat;
  voiceSettings?: ElevenLabsVoiceSettings;
  languageCode?: string;
  chunkSize?: number;
}

/**
 * ElevenLabs streaming TTS adapter.
 *
 * Supported `modelId` values are autocompleted via {@link ElevenLabsModel}.
 * Default is `eleven_flash_v2_5` (lowest TTFT, ~75 ms).
 *
 * **Telephony optimization** — the constructor default
 * `outputFormat='pcm_16000'` is correct for web playback, dashboard
 * previews, and 16 kHz pipelines. For real phone calls, use the
 * carrier-specific factories instead:
 *
 * - {@link ElevenLabsTTS.forTwilio} emits `ulaw_8000` natively. Twilio's
 *   media-stream WebSocket expects μ-law @ 8 kHz, so the SDK normally
 *   resamples 16 kHz → 8 kHz and PCM → μ-law before sending. Asking
 *   ElevenLabs to produce μ-law directly skips that step (saves
 *   ~30–80 ms first-byte plus per-frame CPU and avoids any resampling
 *   aliasing).
 * - {@link ElevenLabsTTS.forTelnyx} emits `pcm_16000`. Telnyx negotiates
 *   L16/16000 on its bidirectional media WebSocket, so 16 kHz PCM is
 *   already the format used end-to-end and no transcoding happens.
 *   ElevenLabs *also* supports `ulaw_8000` if your Telnyx profile is
 *   pinned to PCMU/8000 — pass `outputFormat: 'ulaw_8000'` explicitly
 *   in that case.
 */
export class ElevenLabsTTS {
  private readonly apiKey: string;
  private readonly voiceId: string;
  private readonly modelId: string;
  private readonly outputFormat: ElevenLabsOutputFormat;
  private readonly voiceSettings: ElevenLabsVoiceSettings | undefined;
  private readonly languageCode: string | undefined;
  private readonly chunkSize: number;

  // Overloads: positional form (back-compat, accepts `string` for
  // outputFormat so existing callers passing arbitrary strings keep
  // compiling) and options-object form (strongly typed).
  constructor(
    apiKey: string,
    voiceId?: string,
    modelId?: string,
    outputFormat?: ElevenLabsOutputFormat | string,
  );
  constructor(apiKey: string, options: ElevenLabsTTSOptions);
  constructor(
    apiKey: string,
    voiceIdOrOptions: string | ElevenLabsTTSOptions = '21m00Tcm4TlvDq8ikWAM',
    modelId: string = 'eleven_flash_v2_5',
    outputFormat: ElevenLabsOutputFormat | string = 'pcm_16000',
  ) {
    this.apiKey = apiKey;
    if (typeof voiceIdOrOptions === 'object') {
      const o = voiceIdOrOptions;
      this.voiceId = resolveVoiceId(o.voiceId ?? '21m00Tcm4TlvDq8ikWAM');
      this.modelId = o.modelId ?? 'eleven_flash_v2_5';
      this.outputFormat = o.outputFormat ?? 'pcm_16000';
      this.voiceSettings = o.voiceSettings;
      this.languageCode = o.languageCode;
      this.chunkSize = o.chunkSize ?? 4096;
    } else {
      this.voiceId = resolveVoiceId(voiceIdOrOptions);
      this.modelId = modelId;
      this.outputFormat = outputFormat as ElevenLabsOutputFormat;
      this.voiceSettings = undefined;
      this.languageCode = undefined;
      this.chunkSize = 4096;
    }
  }

  /**
   * Construct an instance pre-configured for Twilio Media Streams.
   *
   * Sets `outputFormat='ulaw_8000'` so ElevenLabs emits μ-law @ 8 kHz
   * directly — the exact wire format Twilio's media stream uses — letting
   * the SDK skip the 16 kHz→8 kHz resample and PCM→μ-law conversion in
   * `TwilioAudioSender`. Saves ~30–80 ms first-byte and per-frame CPU,
   * and removes a potential aliasing source.
   *
   * `voiceSettings` defaults to a low-bandwidth-friendly profile
   * (speaker boost off, modest stability) which sounds cleaner at 8 kHz
   * μ-law than the studio default. Pass an explicit object to override.
   */
  static forTwilio(
    apiKey: string,
    options: Omit<ElevenLabsTTSOptions, 'outputFormat'> = {},
  ): ElevenLabsTTS {
    const voiceSettings: ElevenLabsVoiceSettings = options.voiceSettings ?? {
      // Speaker boost adds high-frequency emphasis that aliases ugly over an
      // 8 kHz μ-law line. Slightly higher stability tames the excursions
      // that compander quantization noise can amplify.
      stability: 0.6,
      similarity_boost: 0.75,
      use_speaker_boost: false,
    };
    return new ElevenLabsTTS(apiKey, {
      ...options,
      voiceSettings,
      outputFormat: 'ulaw_8000',
    });
  }

  /**
   * Construct an instance pre-configured for Telnyx bidirectional media.
   *
   * Telnyx's default media-streaming codec is L16 PCM @ 16 kHz, which
   * matches our default Telnyx handler. We pick `pcm_16000` so the audio
   * flows end-to-end with zero resampling or transcoding.
   *
   * Trade-off: if your Telnyx profile is pinned to PCMU/8000 (μ-law),
   * construct `ElevenLabsTTS` directly with `outputFormat: 'ulaw_8000'`
   * — Telnyx supports that natively too.
   */
  static forTelnyx(
    apiKey: string,
    options: Omit<ElevenLabsTTSOptions, 'outputFormat'> = {},
  ): ElevenLabsTTS {
    return new ElevenLabsTTS(apiKey, {
      ...options,
      outputFormat: 'pcm_16000',
    });
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
   * The yielded buffers are raw PCM at 16 kHz (or whatever `outputFormat` is
   * configured to). `chunkSize` controls the maximum yield size — 512 is a
   * good choice for low-latency telephony.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const url = `${ELEVENLABS_BASE_URL}/text-to-speech/${encodeURIComponent(this.voiceId)}/stream?output_format=${encodeURIComponent(this.outputFormat)}`;

    const body: Record<string, unknown> = {
      text,
      model_id: this.modelId,
    };
    if (this.voiceSettings) body['voice_settings'] = this.voiceSettings;
    if (this.languageCode) body['language_code'] = this.languageCode;

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'xi-api-key': this.apiKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const errBody = await response.text();
      throw new Error(`ElevenLabs TTS error ${response.status}: ${errBody}`);
    }

    if (!response.body) {
      throw new Error('ElevenLabs TTS: no response body');
    }

    const reader = response.body.getReader();
    try {
      // `fetch` reader returns whatever-sized chunks the HTTP layer hands us;
      // re-chunk to <= this.chunkSize so consumers get predictable framing.
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value || value.length === 0) continue;
        const buf = Buffer.from(value);
        for (let offset = 0; offset < buf.length; offset += this.chunkSize) {
          yield buf.subarray(offset, Math.min(offset + this.chunkSize, buf.length));
        }
      }
    } finally {
      // Cancel the HTTP stream to stop ElevenLabs from synthesizing further
      // characters (they bill per character, even if we stop consuming).
      if (typeof reader.cancel === 'function') await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}

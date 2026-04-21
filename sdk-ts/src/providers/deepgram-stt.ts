import WebSocket from 'ws';
import { getLogger } from '../logger';

export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

const DEEPGRAM_WS_URL = 'wss://api.deepgram.com/v1/listen';

/**
 * Optional tuning knobs for Deepgram live transcription.
 *
 * Mirrors Python's ``DeepgramSTT`` kwargs so callers can lower turn latency
 * without monkey-patching (BUG #13).
 */
export interface DeepgramSTTOptions {
  /** Model name. Default ``nova-3``. */
  readonly model?: string;
  /** Audio encoding (``linear16`` | ``mulaw`` | etc). Default ``linear16``. */
  readonly encoding?: string;
  /** Sample rate in Hz. Default ``16000``. */
  readonly sampleRate?: number;
  /**
   * Voice-activity endpointing threshold in milliseconds.
   * Lower values reduce turn latency at the cost of more false-start cuts.
   * Default ``150``.
   */
  readonly endpointingMs?: number;
  /**
   * End-of-utterance silence window in milliseconds. Deepgram enforces a
   * hard minimum of 1000 ms. Set to ``null`` to disable. Default ``1000``.
   */
  readonly utteranceEndMs?: number | null;
  /** Enable smart formatting (punctuation + numerals). Default ``true``. */
  readonly smartFormat?: boolean;
  /** Emit interim (non-final) transcripts. Default ``true``. */
  readonly interimResults?: boolean;
  /** Emit VAD events (``SpeechStarted`` / ``UtteranceEnd``). Default ``true``. */
  readonly vadEvents?: boolean;
}

export class DeepgramSTT {
  private ws: WebSocket | null = null;
  private callbacks: TranscriptCallback[] = [];
  /** Request ID from Deepgram — used to query actual cost post-call. */
  requestId: string = '';

  private readonly apiKey: string;
  private readonly language: string;
  private readonly model: string;
  private readonly encoding: string;
  private readonly sampleRate: number;
  private readonly endpointingMs: number;
  private readonly utteranceEndMs: number | null;
  private readonly smartFormat: boolean;
  private readonly interimResults: boolean;
  private readonly vadEvents: boolean;

  /**
   * New ergonomic constructor accepting an options object (mirrors Python kwargs).
   *
   * Also accepts the legacy positional form
   * ``(apiKey, language?, model?, encoding?, sampleRate?)`` for backward
   * compatibility with code that predated BUG #13.
   */
  constructor(
    apiKey: string,
    language?: string,
    model?: string,
    encoding?: string,
    sampleRate?: number,
    options?: DeepgramSTTOptions,
  );
  constructor(apiKey: string, options: DeepgramSTTOptions & { language?: string });
  constructor(
    apiKey: string,
    languageOrOptions?: string | (DeepgramSTTOptions & { language?: string }),
    model?: string,
    encoding?: string,
    sampleRate?: number,
    options?: DeepgramSTTOptions,
  ) {
    this.apiKey = apiKey;
    const opts: DeepgramSTTOptions & { language?: string } =
      typeof languageOrOptions === 'object' && languageOrOptions !== null
        ? languageOrOptions
        : options ?? {};

    this.language = (typeof languageOrOptions === 'string' ? languageOrOptions : opts.language) ?? 'en';
    this.model = model ?? opts.model ?? 'nova-3';
    this.encoding = encoding ?? opts.encoding ?? 'linear16';
    this.sampleRate = sampleRate ?? opts.sampleRate ?? 16000;
    this.endpointingMs = opts.endpointingMs ?? 150;
    this.utteranceEndMs = opts.utteranceEndMs === null ? null : opts.utteranceEndMs ?? 1000;
    this.smartFormat = opts.smartFormat ?? true;
    this.interimResults = opts.interimResults ?? true;
    this.vadEvents = opts.vadEvents ?? true;
  }

  /** Factory for Twilio calls — mulaw 8 kHz. Forwards tuning options through. */
  static forTwilio(
    apiKey: string,
    language: string = 'en',
    model: string = 'nova-3',
    options: DeepgramSTTOptions = {},
  ): DeepgramSTT {
    return new DeepgramSTT(apiKey, language, model, 'mulaw', 8000, options);
  }

  async connect(): Promise<void> {
    const params = new URLSearchParams({
      model: this.model,
      language: this.language,
      encoding: this.encoding,
      sample_rate: String(this.sampleRate),
      channels: '1',
      interim_results: this.interimResults ? 'true' : 'false',
      endpointing: String(this.endpointingMs),
      smart_format: this.smartFormat ? 'true' : 'false',
      vad_events: this.vadEvents ? 'true' : 'false',
      no_delay: 'true',
    });
    if (this.utteranceEndMs !== null) {
      // Deepgram enforces a hard minimum of 1000 ms on this knob.
      params.set('utterance_end_ms', String(Math.max(this.utteranceEndMs, 1000)));
    }

    const url = `${DEEPGRAM_WS_URL}?${params.toString()}`;

    this.ws = new WebSocket(url, {
      headers: { Authorization: `Token ${this.apiKey}` },
    });

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Deepgram connect timeout')), 10000);
      this.ws!.once('open', () => {
        clearTimeout(timer);
        resolve();
      });
      this.ws!.once('error', (err) => {
        clearTimeout(timer);
        reject(err);
      });
    });

    this.ws.on('message', (raw) => {
      let data: {
        type: string;
        is_final?: boolean;
        speech_final?: boolean;
        channel?: { alternatives?: Array<{ transcript?: string; confidence?: number }> };
      };
      try {
        data = JSON.parse(raw.toString()) as typeof data;
      } catch {
        return;
      }

      if (data.type === 'Metadata' && (data as Record<string, unknown>).request_id) {
        this.requestId = (data as Record<string, unknown>).request_id as string;
        return;
      }

      if (data.type !== 'Results') return;

      const alternatives = data.channel?.alternatives ?? [];
      if (!alternatives.length) return;

      const best = alternatives[0];
      const text = (best.transcript ?? '').trim();
      if (!text) return;

      // BUG #13 — ``is_final`` alone marks a stable utterance;
      // ``speech_final`` is a faster end-of-utterance hint from Deepgram's
      // VAD. Accept either so the pipeline doesn't wait up to
      // utterance_end_ms on every turn.
      const transcript: Transcript = {
        text,
        isFinal: Boolean(data.is_final) || Boolean(data.speech_final),
        confidence: best.confidence ?? 0,
      };

      for (const cb of this.callbacks) {
        cb(transcript);
      }
    });
  }

  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(audio);
  }

  onTranscript(callback: TranscriptCallback): void {
    if (this.callbacks.length >= 10) {
      getLogger().warn('DeepgramSTT: maximum of 10 onTranscript callbacks reached; replacing the last callback.');
      this.callbacks[this.callbacks.length - 1] = callback;
      return;
    }
    this.callbacks.push(callback);
  }

  close(): void {
    if (this.ws) {
      try {
        this.ws.send(JSON.stringify({ type: 'CloseStream' }));
      } catch {
        // ignore
      }
      this.ws.close();
      this.ws = null;
    }
  }
}

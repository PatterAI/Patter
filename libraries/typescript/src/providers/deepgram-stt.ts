import WebSocket from 'ws';
import type { IncomingMessage } from 'http';
import { AuthenticationError, PatterConnectionError, RateLimitError } from '../errors';
import { getLogger } from '../logger';

export type TranscriptEventType = 'Results' | 'UtteranceEnd' | 'SpeechStarted';

export interface DeepgramWord {
  readonly word?: string;
  readonly start?: number;
  readonly end?: number;
  readonly confidence?: number;
  readonly punctuated_word?: string;
  readonly speaker?: number;
}

export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
  /** Deepgram VAD hint — faster end-of-utterance than ``isFinal``. */
  readonly speechFinal?: boolean;
  /** True when this Results frame was produced in response to a Finalize. */
  readonly fromFinalize?: boolean;
  /** Deepgram request id, populated from the initial Metadata frame. */
  readonly requestId?: string;
  /** Per-word timings/metadata when Deepgram emits them. */
  readonly words?: ReadonlyArray<DeepgramWord>;
  /** Which provider event this Transcript represents. Default ``Results``. */
  readonly eventType?: TranscriptEventType;
}

type TranscriptCallback = (transcript: Transcript) => void;
type ErrorCallback = (error: Error) => void;

const DEEPGRAM_WS_URL = 'wss://api.deepgram.com/v1/listen';

/** Known Deepgram STT models. */
export const DeepgramModel = {
  NOVA_3: 'nova-3',
  NOVA_2: 'nova-2',
  NOVA_2_PHONECALL: 'nova-2-phonecall',
  NOVA_2_GENERAL: 'nova-2-general',
  NOVA_2_MEETING: 'nova-2-meeting',
  NOVA: 'nova',
  ENHANCED: 'enhanced',
  BASE: 'base',
} as const;
export type DeepgramModel = (typeof DeepgramModel)[keyof typeof DeepgramModel];

/** Audio encodings accepted by Deepgram's streaming endpoint. */
export const DeepgramEncoding = {
  LINEAR16: 'linear16',
  MULAW: 'mulaw',
  ALAW: 'alaw',
  OPUS: 'opus',
  FLAC: 'flac',
  AMR_NB: 'amr-nb',
  AMR_WB: 'amr-wb',
} as const;
export type DeepgramEncoding = (typeof DeepgramEncoding)[keyof typeof DeepgramEncoding];

/** Common PCM sample rates for Deepgram streaming input. */
export const DeepgramSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_24000: 24000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type DeepgramSampleRate =
  (typeof DeepgramSampleRate)[keyof typeof DeepgramSampleRate];

// Deepgram closes idle sockets after ~10 s of silence. Send a KeepAlive
// text frame every 4 s — well inside the 3–5 s window recommended by
// Deepgram's docs.
const KEEPALIVE_INTERVAL_MS = 4000;

// Close-path tuning: after sending Finalize we give the server a short
// window to flush any trailing partial as a Results frame before we send
// CloseStream. Kept well below the 500 ms close-latency budget.
const FINALIZE_DRAIN_MS = 100;
const CLOSE_LATENCY_BUDGET_MS = 500;

// ws close codes that indicate an unexpected server-side drop which we
// should try to recover from once.
const RECONNECT_CLOSE_CODES = new Set<number>([1006, 1011]);

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
  /**
   * Enable smart formatting (punctuation + numerals). Default ``false`` —
   * smart formatting adds roughly 50–150 ms to TTFT on each final transcript
   * and is rarely useful for telephony pipelines that pass the text straight
   * to an LLM. Set to ``true`` for use cases (dashboards, raw transcripts)
   * where the formatted text is surfaced directly to humans.
   */
  readonly smartFormat?: boolean;
  /** Emit interim (non-final) transcripts. Default ``true``. */
  readonly interimResults?: boolean;
  /** Emit VAD events (``SpeechStarted`` / ``UtteranceEnd``). Default ``true``. */
  readonly vadEvents?: boolean;
}

interface DeepgramResultsMessage {
  type: string;
  is_final?: boolean;
  speech_final?: boolean;
  from_finalize?: boolean;
  request_id?: string;
  channel?: {
    alternatives?: Array<{
      transcript?: string;
      confidence?: number;
      words?: ReadonlyArray<DeepgramWord>;
    }>;
  };
}

export class DeepgramSTT {
  private ws: WebSocket | null = null;
  private readonly transcriptCallbacks = new Set<TranscriptCallback>();
  private readonly errorCallbacks = new Set<ErrorCallback>();
  private keepaliveTimer: ReturnType<typeof setInterval> | null = null;
  private running = false;
  private reconnectAttempted = false;
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
    this.model = model ?? opts.model ?? DeepgramModel.NOVA_3;
    this.encoding = encoding ?? opts.encoding ?? DeepgramEncoding.LINEAR16;
    this.sampleRate = sampleRate ?? opts.sampleRate ?? DeepgramSampleRate.HZ_16000;
    this.endpointingMs = opts.endpointingMs ?? 150;
    this.utteranceEndMs = opts.utteranceEndMs === null ? null : opts.utteranceEndMs ?? 1000;
    this.smartFormat = opts.smartFormat ?? false;
    this.interimResults = opts.interimResults ?? true;
    this.vadEvents = opts.vadEvents ?? true;
  }

  /** Factory for Twilio calls — mulaw 8 kHz. Forwards tuning options through. */
  static forTwilio(
    apiKey: string,
    language: string = 'en',
    model: string = DeepgramModel.NOVA_3,
    options: DeepgramSTTOptions = {},
  ): DeepgramSTT {
    return new DeepgramSTT(
      apiKey,
      language,
      model,
      DeepgramEncoding.MULAW,
      DeepgramSampleRate.HZ_8000,
      options,
    );
  }

  private buildUrl(): string {
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
    return `${DEEPGRAM_WS_URL}?${params.toString()}`;
  }

  async connect(): Promise<void> {
    await this.openSocket();
    this.running = true;
    this.reconnectAttempted = false;
  }

  private async openSocket(): Promise<void> {
    const url = this.buildUrl();

    const ws = new WebSocket(url, {
      headers: { Authorization: `Token ${this.apiKey}` },
    });
    this.ws = ws;

    await new Promise<void>((resolve, reject) => {
      let settled = false;
      const settle = (fn: () => void): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn();
      };
      const timer = setTimeout(
        () => settle(() => reject(new PatterConnectionError('Deepgram connect timeout'))),
        10000,
      );
      ws.once('open', () => settle(resolve));
      ws.once('error', (err: Error) => settle(() => reject(err)));
      ws.once('unexpected-response', (_req: unknown, res: IncomingMessage) => {
        const status = res?.statusCode ?? 0;
        settle(() => {
          if (status === 401 || status === 403) {
            reject(new AuthenticationError(`Deepgram rejected the API key (HTTP ${status}).`));
            return;
          }
          if (status === 429) {
            reject(new RateLimitError('Deepgram rate limit exceeded (HTTP 429).'));
            return;
          }
          reject(new PatterConnectionError(`Deepgram WebSocket upgrade failed (HTTP ${status}).`));
        });
      });
    });

    ws.on('message', (raw) => this.handleMessage(raw.toString()));
    ws.on('close', (code: number, reason: Buffer) => this.handleClose(code, reason.toString()));
    ws.on('error', (err: Error) => this.handleError(err));

    // KeepAlive pump — Deepgram closes after ~10 s of silence.
    this.keepaliveTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(JSON.stringify({ type: 'KeepAlive' }));
        } catch {
          // Socket may have raced to close; the close handler will surface it.
        }
      }
    }, KEEPALIVE_INTERVAL_MS);
  }

  private clearKeepalive(): void {
    if (this.keepaliveTimer) {
      clearInterval(this.keepaliveTimer);
      this.keepaliveTimer = null;
    }
  }

  private handleMessage(raw: string): void {
    let data: DeepgramResultsMessage;
    try {
      data = JSON.parse(raw) as DeepgramResultsMessage;
    } catch {
      return;
    }

    if (data.type === 'Metadata' && data.request_id) {
      this.requestId = data.request_id;
      return;
    }

    if (data.type === 'SpeechStarted') {
      this.emitTranscript({
        text: '',
        isFinal: false,
        confidence: 0,
        eventType: 'SpeechStarted',
        requestId: this.requestId || undefined,
      });
      return;
    }

    if (data.type === 'UtteranceEnd') {
      this.emitTranscript({
        text: '',
        isFinal: true,
        confidence: 0,
        eventType: 'UtteranceEnd',
        requestId: this.requestId || undefined,
      });
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
    const speechFinal = Boolean(data.speech_final);
    const transcript: Transcript = {
      text,
      isFinal: Boolean(data.is_final) || speechFinal,
      confidence: best.confidence ?? 0,
      speechFinal,
      fromFinalize: Boolean(data.from_finalize),
      requestId: this.requestId || undefined,
      words: best.words,
      eventType: 'Results',
    };

    this.emitTranscript(transcript);
  }

  private emitTranscript(transcript: Transcript): void {
    for (const cb of this.transcriptCallbacks) {
      try {
        cb(transcript);
      } catch (err) {
        getLogger().error(`DeepgramSTT transcript callback threw: ${String(err)}`);
      }
    }
  }

  private emitError(err: Error): void {
    for (const cb of this.errorCallbacks) {
      try {
        cb(err);
      } catch (cbErr) {
        getLogger().error(`DeepgramSTT error callback threw: ${String(cbErr)}`);
      }
    }
  }

  private handleError(err: Error): void {
    getLogger().error(`DeepgramSTT WebSocket error: ${err.message}`);
    this.emitError(err);
  }

  private handleClose(code: number, reason: string): void {
    this.clearKeepalive();

    if (!this.running) {
      // User-initiated close — nothing to do.
      return;
    }

    const closeError = new PatterConnectionError(
      `Deepgram WebSocket closed (code=${code}${reason ? `, reason=${reason}` : ''}).`,
    );
    this.emitError(closeError);

    // Attempt a single reconnect for transient server-side drops.
    if (RECONNECT_CLOSE_CODES.has(code) && !this.reconnectAttempted) {
      this.reconnectAttempted = true;
      this.openSocket().catch((err) => {
        this.running = false;
        this.emitError(err instanceof Error ? err : new Error(String(err)));
      });
    } else {
      this.running = false;
    }
  }

  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    // Deepgram treats a zero-length binary frame as CloseStream — drop
    // empty buffers so a silent VAD gate cannot accidentally tear down
    // the session.
    if (audio.length === 0) return;
    this.ws.send(audio);
  }

  onTranscript(callback: TranscriptCallback): void {
    this.transcriptCallbacks.add(callback);
  }

  offTranscript(callback: TranscriptCallback): void {
    this.transcriptCallbacks.delete(callback);
  }

  onError(callback: ErrorCallback): void {
    this.errorCallbacks.add(callback);
  }

  offError(callback: ErrorCallback): void {
    this.errorCallbacks.delete(callback);
  }

  close(): void {
    this.running = false;
    this.clearKeepalive();
    const ws = this.ws;
    if (!ws) return;
    this.ws = null;

    // Send Finalize first to flush any trailing partial, wait briefly for
    // the server to emit its Results frame, then CloseStream. Total close
    // latency is bounded well under CLOSE_LATENCY_BUDGET_MS.
    const sendSafe = (payload: string): void => {
      if (ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(payload);
        } catch {
          // ignore
        }
      }
    };

    const finishClose = (): void => {
      sendSafe(JSON.stringify({ type: 'CloseStream' }));
      try {
        ws.close();
      } catch {
        // ignore
      }
    };

    if (ws.readyState !== WebSocket.OPEN) {
      // Socket is already closing or closed — short-circuit the flush.
      finishClose();
      return;
    }

    // Flush any trailing partial with Finalize, then wait briefly for the
    // server's Results frame before sending CloseStream. FINALIZE_DRAIN_MS
    // plus the close handshake stays well under CLOSE_LATENCY_BUDGET_MS.
    sendSafe(JSON.stringify({ type: 'Finalize' }));
    setTimeout(finishClose, Math.min(FINALIZE_DRAIN_MS, CLOSE_LATENCY_BUDGET_MS));
  }
}

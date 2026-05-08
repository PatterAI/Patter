/**
 * Speechmatics Speech-to-Text adapter for the Patter SDK pipeline mode.
 *
 * Streams PCM audio to the Speechmatics real-time WebSocket API
 * (`wss://eu.rt.speechmatics.com/v2`) and emits Patter-normalised
 * {@link Transcript} events. Mirrors `SpeechmaticsSTT` in the Python SDK.
 *
 * Divergence from Python: the Python adapter wraps the official
 * `speechmatics-voice` Python SDK (Voice Agent presets, smart turn
 * detection, etc.). No equivalent Node SDK is published, so this TypeScript
 * adapter speaks the underlying RT v2 wire protocol directly via `ws`.
 * The user-facing options (`turnDetectionMode`, `endOfUtteranceSilenceTrigger`,
 * `maxDelay`, `enablePartials`, `enableDiarization`, `additionalVocab`,
 * `operatingPoint`, `domain`, `outputLocale`, `language`, `sampleRate`) all
 * map 1:1 onto the Python adapter so callers can switch SDKs without
 * changing their config.
 */

import WebSocket from 'ws';
import type { IncomingMessage } from 'http';
import { AuthenticationError, PatterConnectionError, RateLimitError } from '../errors';
import { getLogger } from '../logger';

/** Patter-normalised transcript event emitted by {@link SpeechmaticsSTT}. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;
type ErrorCallback = (error: Error) => void;

const SPEECHMATICS_RT_URL = 'wss://eu.rt.speechmatics.com/v2';
const CONNECT_TIMEOUT_MS = 10000;

/**
 * Endpoint / turn-detection handling mode. Mirrors the values accepted by
 * Python's `TurnDetectionMode`. Maps onto Speechmatics's
 * `conversation_config` knobs on the wire.
 */
export const TurnDetectionMode = {
  EXTERNAL: 'external',
  FIXED: 'fixed',
  ADAPTIVE: 'adaptive',
  SMART_TURN: 'smart_turn',
} as const;
export type TurnDetectionMode =
  (typeof TurnDetectionMode)[keyof typeof TurnDetectionMode];

/** Common PCM sample rates for Speechmatics streaming input. */
export const SpeechmaticsSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_44100: 44100,
} as const;
export type SpeechmaticsSampleRate =
  (typeof SpeechmaticsSampleRate)[keyof typeof SpeechmaticsSampleRate];

/** Audio encodings accepted by Speechmatics's real-time API. */
export const SpeechmaticsAudioEncoding = {
  PCM_S16LE: 'pcm_s16le',
} as const;
export type SpeechmaticsAudioEncoding =
  (typeof SpeechmaticsAudioEncoding)[keyof typeof SpeechmaticsAudioEncoding];

/** Speechmatics operating points (accuracy vs latency trade-off). */
export const SpeechmaticsOperatingPoint = {
  ENHANCED: 'enhanced',
  STANDARD: 'standard',
} as const;
export type SpeechmaticsOperatingPoint =
  (typeof SpeechmaticsOperatingPoint)[keyof typeof SpeechmaticsOperatingPoint];

/** Speechmatics RT server-message type names emitted to the client. */
export const SpeechmaticsServerMessage = {
  RECOGNITION_STARTED: 'RecognitionStarted',
  ADD_PARTIAL_TRANSCRIPT: 'AddPartialTranscript',
  ADD_TRANSCRIPT: 'AddTranscript',
  END_OF_UTTERANCE: 'EndOfUtterance',
  END_OF_TRANSCRIPT: 'EndOfTranscript',
  AUDIO_ADDED: 'AudioAdded',
  INFO: 'Info',
  WARNING: 'Warning',
  ERROR: 'Error',
} as const;
export type SpeechmaticsServerMessage =
  (typeof SpeechmaticsServerMessage)[keyof typeof SpeechmaticsServerMessage];

/** Constructor options for {@link SpeechmaticsSTT}. */
export interface SpeechmaticsSTTOptions {
  /** Override the realtime endpoint (default `wss://eu.rt.speechmatics.com/v2`). */
  readonly baseUrl?: string;
  /** BCP-47 language code. Default `"en"`. */
  readonly language?: string;
  /** Endpoint / turn-detection mode. Default `"adaptive"`. */
  readonly turnDetectionMode?: TurnDetectionMode;
  /** PCM sample rate (Hz). Default 16000. */
  readonly sampleRate?: SpeechmaticsSampleRate | number;
  /** Attach speaker IDs to transcripts. Default `false`. */
  readonly enableDiarization?: boolean;
  /** Max latency in seconds before the engine emits finals. Range 0.7..4.0. */
  readonly maxDelay?: number;
  /** Silence (s) that triggers EOU. Range (0, 2). */
  readonly endOfUtteranceSilenceTrigger?: number;
  /** Max EOU delay (s); must exceed `endOfUtteranceSilenceTrigger`. */
  readonly endOfUtteranceMaxDelay?: number;
  /** Include partial transcripts in interim output. Default `true`. */
  readonly includePartials?: boolean;
  /** Additional vocabulary entries (`{content, sounds_like?}`). */
  readonly additionalVocab?: ReadonlyArray<Record<string, unknown>>;
  /** Operating point (`enhanced` | `standard`). */
  readonly operatingPoint?: SpeechmaticsOperatingPoint;
  /** Optional Speechmatics domain (e.g. `"finance"`). */
  readonly domain?: string;
  /** Optional output locale (e.g. `"en-GB"`). */
  readonly outputLocale?: string;
}

interface SpeechmaticsAlternative {
  content?: string;
  confidence?: number;
}

interface SpeechmaticsResult {
  alternatives?: SpeechmaticsAlternative[];
  type?: string;
}

interface SpeechmaticsTranscriptMessage {
  message?: string;
  metadata?: { transcript?: string };
  results?: SpeechmaticsResult[];
  reason?: string;
  type?: string;
}

/**
 * Streaming STT adapter for Speechmatics's RT v2 WebSocket API.
 *
 * @example
 * ```ts
 * const stt = new SpeechmaticsSTT('sm_api_key', { language: 'en' });
 * stt.onTranscript((t) => console.log(t.text, t.isFinal));
 * await stt.connect();
 * stt.sendAudio(pcm16Chunk);
 * stt.close();
 * ```
 */
export class SpeechmaticsSTT {
  private ws: WebSocket | null = null;
  private readonly transcriptCallbacks = new Set<TranscriptCallback>();
  private readonly errorCallbacks = new Set<ErrorCallback>();
  private running = false;
  /** Sequence number of the last audio chunk acknowledged via `AudioAdded`. */
  private lastSeqNo = 0;

  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly language: string;
  private readonly turnDetectionMode: TurnDetectionMode;
  private readonly sampleRate: number;
  private readonly enableDiarization: boolean;
  private readonly maxDelay: number | undefined;
  private readonly endOfUtteranceSilenceTrigger: number | undefined;
  private readonly endOfUtteranceMaxDelay: number | undefined;
  private readonly includePartials: boolean;
  private readonly additionalVocab: ReadonlyArray<Record<string, unknown>>;
  private readonly operatingPoint: SpeechmaticsOperatingPoint | undefined;
  private readonly domain: string | undefined;
  private readonly outputLocale: string | undefined;

  constructor(apiKey: string, options: SpeechmaticsSTTOptions = {}) {
    if (!apiKey) {
      throw new Error('Speechmatics apiKey is required');
    }

    // Validate ranges per the Speechmatics Voice SDK contract — kept in
    // lockstep with the Python adapter so misconfigurations surface the
    // same way regardless of which SDK the user picks.
    const eouSilence = options.endOfUtteranceSilenceTrigger;
    const eouMax = options.endOfUtteranceMaxDelay;
    const maxDelay = options.maxDelay;
    if (eouSilence !== undefined && !(eouSilence > 0 && eouSilence < 2)) {
      throw new Error('endOfUtteranceSilenceTrigger must be between 0 and 2');
    }
    if (
      eouMax !== undefined &&
      eouSilence !== undefined &&
      eouMax <= eouSilence
    ) {
      throw new Error(
        'endOfUtteranceMaxDelay must be greater than endOfUtteranceSilenceTrigger',
      );
    }
    if (maxDelay !== undefined && !(maxDelay >= 0.7 && maxDelay <= 4.0)) {
      throw new Error('maxDelay must be between 0.7 and 4.0');
    }

    this.apiKey = apiKey;
    this.baseUrl = options.baseUrl ?? SPEECHMATICS_RT_URL;
    this.language = options.language ?? 'en';
    this.turnDetectionMode = options.turnDetectionMode ?? TurnDetectionMode.ADAPTIVE;
    this.sampleRate = options.sampleRate ?? SpeechmaticsSampleRate.HZ_16000;
    this.enableDiarization = options.enableDiarization ?? false;
    this.maxDelay = maxDelay;
    this.endOfUtteranceSilenceTrigger = eouSilence;
    this.endOfUtteranceMaxDelay = eouMax;
    this.includePartials = options.includePartials ?? true;
    this.additionalVocab = options.additionalVocab ?? [];
    this.operatingPoint = options.operatingPoint;
    this.domain = options.domain;
    this.outputLocale = options.outputLocale;
  }

  /** Build the JSON `StartRecognition` payload sent on connect. */
  private buildStartRecognition(): Record<string, unknown> {
    const transcriptionConfig: Record<string, unknown> = {
      language: this.language,
      enable_partials: this.includePartials,
      diarization: this.enableDiarization ? 'speaker' : 'none',
    };
    if (this.maxDelay !== undefined) transcriptionConfig.max_delay = this.maxDelay;
    if (this.operatingPoint !== undefined) {
      transcriptionConfig.operating_point = this.operatingPoint;
    }
    if (this.domain !== undefined) transcriptionConfig.domain = this.domain;
    if (this.outputLocale !== undefined) {
      transcriptionConfig.output_locale = this.outputLocale;
    }
    if (this.additionalVocab.length > 0) {
      transcriptionConfig.additional_vocab = [...this.additionalVocab];
    }

    // `conversation_config` carries the turn-detection knobs used by the
    // Python Voice SDK. Speechmatics RT v2 accepts these fields on the
    // root `transcription_config` object.
    const conversationConfig: Record<string, unknown> = {
      end_of_utterance_mode: this.turnDetectionMode,
    };
    if (this.endOfUtteranceSilenceTrigger !== undefined) {
      conversationConfig.end_of_utterance_silence_trigger =
        this.endOfUtteranceSilenceTrigger;
    }
    if (this.endOfUtteranceMaxDelay !== undefined) {
      conversationConfig.end_of_utterance_max_delay = this.endOfUtteranceMaxDelay;
    }
    transcriptionConfig.conversation_config = conversationConfig;

    return {
      message: 'StartRecognition',
      audio_format: {
        type: 'raw',
        encoding: SpeechmaticsAudioEncoding.PCM_S16LE,
        sample_rate: this.sampleRate,
      },
      transcription_config: transcriptionConfig,
    };
  }

  /** Open the streaming WebSocket and send the `StartRecognition` frame. */
  async connect(): Promise<void> {
    if (this.ws !== null) return;

    const ws = new WebSocket(this.baseUrl, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
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
        () =>
          settle(() =>
            reject(new PatterConnectionError('Speechmatics connect timeout')),
          ),
        CONNECT_TIMEOUT_MS,
      );
      ws.once('open', () => settle(resolve));
      ws.once('error', (err: Error) => settle(() => reject(err)));
      ws.once('unexpected-response', (_req: unknown, res: IncomingMessage) => {
        const status = res?.statusCode ?? 0;
        settle(() => {
          if (status === 401 || status === 403) {
            reject(
              new AuthenticationError(
                `Speechmatics rejected the API key (HTTP ${status}).`,
              ),
            );
            return;
          }
          if (status === 429) {
            reject(
              new RateLimitError('Speechmatics rate limit exceeded (HTTP 429).'),
            );
            return;
          }
          reject(
            new PatterConnectionError(
              `Speechmatics WebSocket upgrade failed (HTTP ${status}).`,
            ),
          );
        });
      });
    });

    ws.on('message', (raw) => this.handleMessage(raw.toString()));
    ws.on('close', () => this.handleClose());
    ws.on('error', (err: Error) => this.handleError(err));

    try {
      ws.send(JSON.stringify(this.buildStartRecognition()));
    } catch (err) {
      throw new PatterConnectionError(
        `Speechmatics StartRecognition send failed: ${String(err)}`,
      );
    }
    this.running = true;
  }

  /** Send a binary PCM16-LE audio chunk to Speechmatics for transcription. */
  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }
    if (audio.length === 0) {
      // Speechmatics treats empty frames as protocol errors — drop silently.
      return;
    }
    this.lastSeqNo += 1;
    try {
      this.ws.send(audio);
    } catch (err) {
      getLogger().error(`SpeechmaticsSTT sendAudio failed: ${String(err)}`);
    }
  }

  /** Register a transcript listener. */
  onTranscript(callback: TranscriptCallback): void {
    this.transcriptCallbacks.add(callback);
  }

  /** Remove a previously registered transcript listener. */
  offTranscript(callback: TranscriptCallback): void {
    this.transcriptCallbacks.delete(callback);
  }

  /** Register an error listener for socket / API failures. */
  onError(callback: ErrorCallback): void {
    this.errorCallbacks.add(callback);
  }

  /** Remove a previously registered error listener. */
  offError(callback: ErrorCallback): void {
    this.errorCallbacks.delete(callback);
  }

  private handleMessage(raw: string): void {
    let data: SpeechmaticsTranscriptMessage;
    try {
      data = JSON.parse(raw) as SpeechmaticsTranscriptMessage;
    } catch {
      return;
    }
    const event = data.message;
    if (!event) return;

    switch (event) {
      case SpeechmaticsServerMessage.RECOGNITION_STARTED:
      case SpeechmaticsServerMessage.AUDIO_ADDED:
      case SpeechmaticsServerMessage.END_OF_UTTERANCE:
      case SpeechmaticsServerMessage.END_OF_TRANSCRIPT:
      case SpeechmaticsServerMessage.INFO:
        // No transcript text — log only the lifecycle messages we care to
        // surface for debugging.
        return;
      case SpeechmaticsServerMessage.WARNING:
        getLogger().warn(`SpeechmaticsSTT warning: ${JSON.stringify(data)}`);
        return;
      case SpeechmaticsServerMessage.ERROR: {
        const message =
          data.reason ?? data.type ?? 'Speechmatics returned an Error frame';
        getLogger().error(`SpeechmaticsSTT error: ${message}`);
        this.emitError(new PatterConnectionError(`Speechmatics: ${message}`));
        return;
      }
      case SpeechmaticsServerMessage.ADD_PARTIAL_TRANSCRIPT:
      case SpeechmaticsServerMessage.ADD_TRANSCRIPT: {
        const isFinal = event === SpeechmaticsServerMessage.ADD_TRANSCRIPT;
        const transcript = this.toTranscript(data, isFinal);
        if (transcript !== null) this.emitTranscript(transcript);
        return;
      }
      default:
        return;
    }
  }

  /** Translate a Speechmatics transcript message into a Patter `Transcript`. */
  private toTranscript(
    message: SpeechmaticsTranscriptMessage,
    isFinal: boolean,
  ): Transcript | null {
    // Speechmatics RT supplies a ready-rendered `metadata.transcript` string
    // alongside the per-token `results[]`. Prefer the rendered transcript so
    // we don't have to re-implement word delimiter / punctuation rules.
    const rendered = (message.metadata?.transcript ?? '').trim();
    const results = message.results ?? [];

    let text = rendered;
    const confidences: number[] = [];
    for (const result of results) {
      const best = result.alternatives?.[0];
      if (!best) continue;
      const content = best.content;
      const confidence = best.confidence;
      if (!rendered && typeof content === 'string' && content.length > 0) {
        text = text ? `${text} ${content}` : content;
      }
      if (typeof confidence === 'number') {
        confidences.push(confidence);
      }
    }

    text = text.trim();
    if (!text) return null;

    const confidence =
      confidences.length > 0
        ? confidences.reduce((sum, c) => sum + c, 0) / confidences.length
        : 1.0;
    return { text, isFinal, confidence };
  }

  private emitTranscript(transcript: Transcript): void {
    for (const cb of this.transcriptCallbacks) {
      try {
        cb(transcript);
      } catch (err) {
        getLogger().error(`SpeechmaticsSTT transcript callback threw: ${String(err)}`);
      }
    }
  }

  private emitError(err: Error): void {
    for (const cb of this.errorCallbacks) {
      try {
        cb(err);
      } catch (cbErr) {
        getLogger().error(`SpeechmaticsSTT error callback threw: ${String(cbErr)}`);
      }
    }
  }

  private handleError(err: Error): void {
    getLogger().error(`SpeechmaticsSTT WebSocket error: ${err.message}`);
    this.emitError(err);
  }

  private handleClose(): void {
    if (!this.running) return;
    this.running = false;
  }

  /** Send `EndOfStream` and close the WebSocket. Idempotent. */
  close(): void {
    this.running = false;
    const ws = this.ws;
    if (!ws) return;
    this.ws = null;

    const sendSafe = (payload: string): void => {
      if (ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(payload);
        } catch {
          // ignore — the close path below tears down the socket anyway.
        }
      }
    };
    sendSafe(
      JSON.stringify({ message: 'EndOfStream', last_seq_no: this.lastSeqNo }),
    );

    try {
      ws.close();
    } catch {
      // ignore — best-effort close.
    }
  }
}

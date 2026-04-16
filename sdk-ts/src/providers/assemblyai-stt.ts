/**
 * AssemblyAI Universal Streaming STT adapter for the Patter SDK pipeline mode.
 *
 * Implements a `DeepgramSTT`-shaped provider using AssemblyAI's v3 streaming
 * WebSocket API. Pure `ws` transport — does NOT depend on the vendor SDK.
 *
 * Algorithm adapted from LiveKit Agents (Apache 2.0):
 * https://github.com/livekit/agents
 * Source: livekit-plugins/livekit-plugins-assemblyai/livekit/plugins/assemblyai/stt.py
 * Upstream ref SHA: 78a66bcf79c5cea82989401c408f1dff4b961a5b
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

export type AssemblyAIEncoding = 'pcm_s16le' | 'pcm_mulaw';

export type AssemblyAIModel =
  | 'universal-streaming-english'
  | 'universal-streaming-multilingual'
  | 'u3-rt-pro';

export interface AssemblyAISTTOptions {
  /** One of the AssemblyAI speech models. */
  readonly model?: AssemblyAIModel;
  /** PCM encoding: 16-bit little-endian (default) or G.711 mu-law for telephony. */
  readonly encoding?: AssemblyAIEncoding;
  /** Sample rate in Hz — 16000 for wideband audio, 8000 for telephony. */
  readonly sampleRate?: number;
  /** Override the streaming base URL (e.g. EU: `wss://streaming.eu.assemblyai.com`). */
  readonly baseUrl?: string;
  /** Enable automatic language detection (defaults: true for multilingual/u3-rt-pro). */
  readonly languageDetection?: boolean;
  /** 0..1 confidence required before end-of-turn is finalized. */
  readonly endOfTurnConfidenceThreshold?: number;
  /** Minimum ms of silence required before end-of-turn finalizes. */
  readonly minTurnSilence?: number;
  /** Maximum ms of silence before the turn is force-finalized. */
  readonly maxTurnSilence?: number;
  /** When true, wait for the formatted transcript before emitting final. */
  readonly formatTurns?: boolean;
  /** Bias keywords/phrases. */
  readonly keytermsPrompt?: readonly string[];
  /** Text prompt (u3-rt-pro only). */
  readonly prompt?: string;
  /** VAD threshold (0..1). */
  readonly vadThreshold?: number;
  /** Enable diarization / speaker labels. */
  readonly speakerLabels?: boolean;
  /** Max speakers for diarization. */
  readonly maxSpeakers?: number;
  /** Domain hint (e.g. "medical"). */
  readonly domain?: string;
}

const DEFAULT_BASE_URL = 'wss://streaming.assemblyai.com';
const DEFAULT_MIN_TURN_SILENCE_MS = 100;
const CONNECT_TIMEOUT_MS = 10000;
const MAX_CALLBACKS = 10;

interface AssemblyAIWord {
  readonly text?: string;
  readonly start?: number;
  readonly end?: number;
  readonly confidence?: number;
}

interface AssemblyAIEvent {
  readonly type?: string;
  readonly id?: string;
  readonly expires_at?: number;
  readonly transcript?: string;
  readonly end_of_turn?: boolean;
  readonly turn_is_formatted?: boolean;
  readonly words?: readonly AssemblyAIWord[];
}

export class AssemblyAISTT {
  private ws: WebSocket | null = null;
  private callbacks: TranscriptCallback[] = [];
  /** AssemblyAI session id — set when the `Begin` message arrives. */
  public sessionId: string = '';
  /** Unix timestamp when the AssemblyAI session expires. */
  public expiresAt: number = 0;

  constructor(
    private readonly apiKey: string,
    private readonly options: AssemblyAISTTOptions = {},
  ) {
    if (!apiKey) {
      throw new Error('AssemblyAISTT requires a non-empty apiKey');
    }
  }

  /** Factory for Twilio calls — mulaw 8 kHz. */
  static forTwilio(apiKey: string, model: AssemblyAIModel = 'universal-streaming-english'): AssemblyAISTT {
    return new AssemblyAISTT(apiKey, {
      model,
      encoding: 'pcm_mulaw',
      sampleRate: 8000,
    });
  }

  private buildUrl(): string {
    const opts = this.options;
    const model: AssemblyAIModel = opts.model ?? 'universal-streaming-english';
    const encoding: AssemblyAIEncoding = opts.encoding ?? 'pcm_s16le';
    const sampleRate: number = opts.sampleRate ?? 16000;

    let minSilence: number | undefined;
    let maxSilence: number | undefined;
    if (model === 'u3-rt-pro') {
      minSilence = opts.minTurnSilence ?? 100;
      maxSilence = opts.maxTurnSilence ?? minSilence;
    } else {
      minSilence = opts.minTurnSilence ?? DEFAULT_MIN_TURN_SILENCE_MS;
      maxSilence = opts.maxTurnSilence;
    }

    const languageDetection =
      opts.languageDetection ?? (model.includes('multilingual') || model === 'u3-rt-pro');

    const raw: Record<string, unknown> = {
      sample_rate: sampleRate,
      encoding,
      speech_model: model,
      format_turns: opts.formatTurns,
      end_of_turn_confidence_threshold: opts.endOfTurnConfidenceThreshold,
      min_turn_silence: minSilence,
      max_turn_silence: maxSilence,
      keyterms_prompt: opts.keytermsPrompt ? JSON.stringify(opts.keytermsPrompt) : undefined,
      language_detection: languageDetection,
      prompt: opts.prompt,
      vad_threshold: opts.vadThreshold,
      speaker_labels: opts.speakerLabels,
      max_speakers: opts.maxSpeakers,
      domain: opts.domain,
    };

    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(raw)) {
      if (value === undefined || value === null) continue;
      if (typeof value === 'boolean') {
        params.set(key, value ? 'true' : 'false');
      } else {
        params.set(key, String(value));
      }
    }

    const base = opts.baseUrl ?? DEFAULT_BASE_URL;
    return `${base}/v3/ws?${params.toString()}`;
  }

  async connect(): Promise<void> {
    const url = this.buildUrl();
    this.ws = new WebSocket(url, {
      headers: {
        Authorization: this.apiKey,
        'Content-Type': 'application/json',
        'User-Agent': 'Patter/1.0 (integration=LiveKit-port)',
      },
    });

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error('AssemblyAI connect timeout')),
        CONNECT_TIMEOUT_MS,
      );
      this.ws!.once('open', () => {
        clearTimeout(timer);
        resolve();
      });
      this.ws!.once('error', (err: Error) => {
        clearTimeout(timer);
        reject(err);
      });
    });

    this.ws.on('message', (raw: WebSocket.RawData) => {
      let event: AssemblyAIEvent;
      try {
        event = JSON.parse(raw.toString()) as AssemblyAIEvent;
      } catch {
        return;
      }
      this.handleEvent(event);
    });
  }

  private handleEvent(event: AssemblyAIEvent): void {
    const type = event.type;

    if (type === 'Begin') {
      this.sessionId = event.id ?? '';
      this.expiresAt = event.expires_at ?? 0;
      return;
    }

    if (type !== 'Turn') {
      // Ignore "SpeechStarted" / "Termination" — callers don't consume them here.
      return;
    }

    const endOfTurn = Boolean(event.end_of_turn);
    const turnIsFormatted = Boolean(event.turn_is_formatted);
    const words = event.words ?? [];
    const transcriptText = (event.transcript ?? '').trim();

    if (endOfTurn) {
      // If format_turns requested, only surface the formatted version.
      if (this.options.formatTurns && !turnIsFormatted) return;
      if (!transcriptText) return;
      this.emit({
        text: transcriptText,
        isFinal: true,
        confidence: averageConfidence(words),
      });
      return;
    }

    // Interim: concatenate cumulative word list.
    if (!words.length) return;
    const interim = words
      .map((w) => (w.text ?? '').trim())
      .filter(Boolean)
      .join(' ');
    if (!interim) return;
    this.emit({
      text: interim,
      isFinal: false,
      confidence: averageConfidence(words),
    });
  }

  private emit(transcript: Transcript): void {
    for (const cb of this.callbacks) {
      cb(transcript);
    }
  }

  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(audio);
  }

  onTranscript(callback: TranscriptCallback): void {
    if (this.callbacks.length >= MAX_CALLBACKS) {
      getLogger().warn(
        'AssemblyAISTT: maximum of 10 onTranscript callbacks reached; replacing the last callback.',
      );
      this.callbacks[this.callbacks.length - 1] = callback;
      return;
    }
    this.callbacks.push(callback);
  }

  close(): void {
    if (this.ws) {
      try {
        this.ws.send(JSON.stringify({ type: 'Terminate' }));
      } catch {
        // ignore
      }
      this.ws.close();
      this.ws = null;
    }
  }
}

function averageConfidence(words: readonly AssemblyAIWord[]): number {
  if (!words.length) return 0;
  let total = 0;
  for (const w of words) {
    total += Number(w.confidence ?? 0);
  }
  return total / words.length;
}

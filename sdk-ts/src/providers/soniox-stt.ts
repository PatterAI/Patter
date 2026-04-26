/**
 * Soniox Speech-to-Text adapter for Patter (TypeScript).
 *
 * Pure WebSocket client for the Soniox real-time STT API. Accumulates
 * `is_final` tokens and flushes them on `<end>`/`<fin>` endpoint tokens,
 * mirroring the Python `SonioxSTT` adapter.
 *
 * Adapted from LiveKit Agents (Apache 2.0):
 * https://github.com/livekit/agents
 * (source: livekit-plugins/livekit-plugins-soniox/livekit/plugins/soniox/stt.py
 *  at commit 78a66bcf79c5cea82989401c408f1dff4b961a5b)
 *
 * Speechmatics TypeScript adapter is **intentionally not ported**: the
 * official Speechmatics Voice SDK (`speechmatics.voice`) is Python-only at
 * the time of writing. Python users should install the optional
 * `speechmatics` extra; TypeScript users need to wait for an official
 * upstream SDK before this adapter can land without a WS-handshake reimpl.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

const SONIOX_WS_URL = 'wss://stt-rt.soniox.com/transcribe-websocket';
const KEEPALIVE_MESSAGE = '{"type": "keepalive"}';
const END_TOKEN = '<end>';
const FINALIZED_TOKEN = '<fin>';
const KEEPALIVE_INTERVAL_MS = 5000;

export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

interface SonioxToken {
  text?: string;
  is_final?: boolean;
  confidence?: number;
  [key: string]: unknown;
}

interface SonioxMessage {
  tokens?: SonioxToken[];
  finished?: boolean;
  error_code?: unknown;
  error_message?: string;
}

function isEndToken(token: SonioxToken): boolean {
  return token.text === END_TOKEN || token.text === FINALIZED_TOKEN;
}

/** Accumulates Soniox token text + rolling confidence. */
class TokenAccumulator {
  text = '';
  private confSum = 0;
  private confCount = 0;

  update(token: SonioxToken): void {
    if (token.text) {
      this.text += token.text;
    }
    if (typeof token.confidence === 'number') {
      this.confSum += token.confidence;
      this.confCount += 1;
    }
  }

  get confidence(): number {
    return this.confCount === 0 ? 0 : this.confSum / this.confCount;
  }

  reset(): void {
    this.text = '';
    this.confSum = 0;
    this.confCount = 0;
  }

  get raw(): { sum: number; count: number } {
    return { sum: this.confSum, count: this.confCount };
  }
}

export interface SonioxSTTOptions {
  model?: string;
  languageHints?: string[];
  languageHintsStrict?: boolean;
  sampleRate?: number;
  numChannels?: number;
  enableSpeakerDiarization?: boolean;
  enableLanguageIdentification?: boolean;
  maxEndpointDelayMs?: number;
  clientReferenceId?: string;
  baseUrl?: string;
}

export class SonioxSTT {
  private ws: WebSocket | null = null;
  private callbacks: TranscriptCallback[] = [];
  private final = new TokenAccumulator();
  private keepaliveTimer: ReturnType<typeof setInterval> | null = null;

  private readonly apiKey: string;
  private readonly model: string;
  private readonly languageHints?: string[];
  private readonly languageHintsStrict: boolean;
  private readonly sampleRate: number;
  private readonly numChannels: number;
  private readonly enableSpeakerDiarization: boolean;
  private readonly enableLanguageIdentification: boolean;
  private readonly maxEndpointDelayMs: number;
  private readonly clientReferenceId?: string;
  private readonly baseUrl: string;

  constructor(apiKey: string, options: SonioxSTTOptions = {}) {
    if (!apiKey) {
      throw new Error('Soniox apiKey is required');
    }
    const maxEndpointDelayMs = options.maxEndpointDelayMs ?? 500;
    if (maxEndpointDelayMs < 500 || maxEndpointDelayMs > 3000) {
      throw new Error('maxEndpointDelayMs must be between 500 and 3000');
    }

    this.apiKey = apiKey;
    this.model = options.model ?? 'stt-rt-v4';
    this.languageHints = options.languageHints;
    this.languageHintsStrict = options.languageHintsStrict ?? false;
    this.sampleRate = options.sampleRate ?? 16000;
    this.numChannels = options.numChannels ?? 1;
    this.enableSpeakerDiarization = options.enableSpeakerDiarization ?? false;
    this.enableLanguageIdentification = options.enableLanguageIdentification ?? true;
    this.maxEndpointDelayMs = maxEndpointDelayMs;
    this.clientReferenceId = options.clientReferenceId;
    this.baseUrl = options.baseUrl ?? SONIOX_WS_URL;
  }

  /** Factory for Twilio-style 8 kHz linear PCM. */
  static forTwilio(apiKey: string, languageHints?: string[]): SonioxSTT {
    return new SonioxSTT(apiKey, { sampleRate: 8000, languageHints });
  }

  private buildConfig(): Record<string, unknown> {
    const config: Record<string, unknown> = {
      api_key: this.apiKey,
      model: this.model,
      audio_format: 'pcm_s16le',
      num_channels: this.numChannels,
      sample_rate: this.sampleRate,
      enable_endpoint_detection: true,
      enable_speaker_diarization: this.enableSpeakerDiarization,
      enable_language_identification: this.enableLanguageIdentification,
      max_endpoint_delay_ms: this.maxEndpointDelayMs,
    };
    if (this.languageHints) {
      config.language_hints = this.languageHints;
      config.language_hints_strict = this.languageHintsStrict;
    }
    if (this.clientReferenceId) {
      config.client_reference_id = this.clientReferenceId;
    }
    return config;
  }

  async connect(): Promise<void> {
    // Reset the accumulator so reconnection after close() does not carry
    // stale final.text across streams.
    this.final.reset();
    this.ws = new WebSocket(this.baseUrl);

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Soniox connect timeout')), 10000);
      this.ws!.once('open', () => {
        clearTimeout(timer);
        resolve();
      });
      this.ws!.once('error', (err) => {
        clearTimeout(timer);
        reject(err);
      });
    });

    // Send the initial configuration payload as a JSON text frame.
    this.ws.send(JSON.stringify(this.buildConfig()));

    this.ws.on('message', (raw) => this.handleMessage(raw.toString()));
    this.ws.on('close', () => this.clearKeepalive());
    this.ws.on('error', (err) => {
      getLogger().error(`SonioxSTT WebSocket error: ${String(err)}`);
    });

    this.keepaliveTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(KEEPALIVE_MESSAGE);
        } catch {
          // ignore
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
    let content: SonioxMessage;
    try {
      content = JSON.parse(raw) as SonioxMessage;
    } catch {
      return;
    }

    if (content.error_code || content.error_message) {
      getLogger().error(
        `SonioxSTT error ${String(content.error_code)}: ${String(content.error_message)}`,
      );
    }

    const tokens = content.tokens ?? [];
    const nonFinal = new TokenAccumulator();
    let emittedFinalThisMsg = false;

    for (const token of tokens) {
      if (token.is_final) {
        if (isEndToken(token)) {
          if (this.final.text) {
            this.emit({
              text: this.final.text.trim(),
              isFinal: true,
              confidence: this.final.confidence,
            });
            this.final.reset();
            emittedFinalThisMsg = true;
          }
        } else {
          this.final.update(token);
        }
      } else {
        nonFinal.update(token);
      }
    }

    if (!emittedFinalThisMsg) {
      const text = (this.final.text + nonFinal.text).trim();
      if (text) {
        const { sum: fSum, count: fCount } = this.final.raw;
        const { sum: nSum, count: nCount } = nonFinal.raw;
        const total = fCount + nCount;
        const confidence = total > 0 ? (fSum + nSum) / total : 0;
        this.emit({ text, isFinal: false, confidence });
      }
    }

    if (content.finished && this.final.text) {
      this.emit({
        text: this.final.text.trim(),
        isFinal: true,
        confidence: this.final.confidence,
      });
      this.final.reset();
    }
  }

  private emit(transcript: Transcript): void {
    for (const cb of this.callbacks) {
      cb(transcript);
    }
  }

  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (audio.length === 0) return;
    this.ws.send(audio);
  }

  onTranscript(callback: TranscriptCallback): void {
    if (this.callbacks.length >= 10) {
      getLogger().warn(
        'SonioxSTT: maximum of 10 onTranscript callbacks reached; replacing the last callback.',
      );
      this.callbacks[this.callbacks.length - 1] = callback;
      return;
    }
    this.callbacks.push(callback);
  }

  close(): void {
    this.clearKeepalive();
    if (this.ws) {
      try {
        // Soniox terminates the stream on an empty binary frame.
        this.ws.send(Buffer.alloc(0));
      } catch {
        // ignore
      }
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
  }
}

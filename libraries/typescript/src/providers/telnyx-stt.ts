/**
 * Telnyx Speech-to-Text adapter (WebSocket streaming).
 *
 * Bridges the Telnyx `/v2/speech-to-text/transcription` WebSocket API to the
 * Patter SDK pipeline-mode STT interface. Implemented in TypeScript
 * (`ws` + `Buffer`) with a callback-based interface matching the other
 * Patter STT providers (Deepgram, Whisper).
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

/** Patter-normalised transcript event emitted by {@link TelnyxSTT}. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

/** Backing transcription engine accepted by Telnyx STT. */
export type TelnyxTranscriptionEngine = 'telnyx' | 'google' | 'deepgram' | 'azure';

const TELNYX_STT_WS_URL = 'wss://api.telnyx.com/v2/speech-to-text/transcription';
const DEFAULT_SAMPLE_RATE = 16000;
const NUM_CHANNELS = 1;

/** Build a streaming WAV header with maximum possible data size. */
function createStreamingWavHeader(sampleRate: number, numChannels: number): Buffer {
  const bytesPerSample = 2;
  const byteRate = sampleRate * numChannels * bytesPerSample;
  const blockAlign = numChannels * bytesPerSample;
  const dataSize = 0x7fffffff;
  const fileSize = 36 + dataSize;

  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(fileSize, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(numChannels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(byteRate, 28);
  header.writeUInt16LE(blockAlign, 32);
  header.writeUInt16LE(16, 34); // bits per sample
  header.write('data', 36);
  header.writeUInt32LE(dataSize, 40);
  return header;
}

/** Streaming STT adapter for Telnyx's `/v2/speech-to-text` WebSocket. */
export class TelnyxSTT {
  private ws: WebSocket | null = null;
  private callbacks: TranscriptCallback[] = [];
  private headerSent = false;

  constructor(
    private readonly apiKey: string,
    private readonly language: string = 'en',
    private readonly transcriptionEngine: TelnyxTranscriptionEngine = 'telnyx',
    private readonly sampleRate: number = DEFAULT_SAMPLE_RATE,
    private readonly baseUrl: string = TELNYX_STT_WS_URL,
  ) {}

  /** Open the streaming WebSocket and arm message handlers. */
  async connect(): Promise<void> {
    const params = new URLSearchParams({
      transcription_engine: this.transcriptionEngine,
      language: this.language,
      input_format: 'wav',
    });
    const url = `${this.baseUrl}?${params.toString()}`;

    this.ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Telnyx STT connect timeout')), 10_000);
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
      let data: { transcript?: string; is_final?: boolean; confidence?: number };
      try {
        data = JSON.parse(raw.toString()) as typeof data;
      } catch {
        return;
      }

      const text = (data.transcript ?? '').trim();
      if (!text) return;

      const transcript: Transcript = {
        text,
        isFinal: Boolean(data.is_final),
        confidence: data.confidence ?? 0,
      };

      for (const cb of this.callbacks) {
        cb(transcript);
      }
    });

    this.ws.on('error', (err) => {
      getLogger().warn(`TelnyxSTT WebSocket error: ${String(err)}`);
    });
  }

  /** Send a binary PCM16 audio chunk; emits the WAV header on the first call. */
  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    if (!this.headerSent) {
      const header = createStreamingWavHeader(this.sampleRate, NUM_CHANNELS);
      this.ws.send(header);
      this.headerSent = true;
    }

    this.ws.send(audio);
  }

  /** Register a transcript listener (max 10 concurrent listeners). */
  onTranscript(callback: TranscriptCallback): void {
    if (this.callbacks.length >= 10) {
      getLogger().warn('TelnyxSTT: maximum of 10 onTranscript callbacks reached; replacing the last callback.');
      this.callbacks[this.callbacks.length - 1] = callback;
      return;
    }
    this.callbacks.push(callback);
  }

  /** Close the streaming WebSocket. */
  close(): void {
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
  }
}

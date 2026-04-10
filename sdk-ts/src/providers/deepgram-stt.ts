import WebSocket from 'ws';
import { getLogger } from '../logger';

export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

const DEEPGRAM_WS_URL = 'wss://api.deepgram.com/v1/listen';

export class DeepgramSTT {
  private ws: WebSocket | null = null;
  private callbacks: TranscriptCallback[] = [];
  /** Request ID from Deepgram — used to query actual cost post-call. */
  requestId: string = '';

  constructor(
    private readonly apiKey: string,
    private readonly language: string = 'en',
    private readonly model: string = 'nova-3',
    private readonly encoding: string = 'linear16',
    private readonly sampleRate: number = 16000,
  ) {}

  /** Factory for Twilio calls — mulaw 8 kHz. */
  static forTwilio(apiKey: string, language: string = 'en', model: string = 'nova-3'): DeepgramSTT {
    return new DeepgramSTT(apiKey, language, model, 'mulaw', 8000);
  }

  async connect(): Promise<void> {
    const params = new URLSearchParams({
      model: this.model,
      language: this.language,
      encoding: this.encoding,
      sample_rate: String(this.sampleRate),
      channels: '1',
      interim_results: 'true',
      endpointing: '300',
      smart_format: 'true',
      vad_events: 'true',
      no_delay: 'true',
    });

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

      const transcript: Transcript = {
        text,
        isFinal: Boolean(data.is_final) && Boolean(data.speech_final),
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

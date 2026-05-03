/**
 * Telnyx Text-to-Speech adapter (WebSocket streaming).
 *
 * Bridges the Telnyx `/v2/text-to-speech/speech` WebSocket API to the
 * Patter SDK pipeline-mode TTS interface. Implemented in TypeScript
 * (`ws` + `Buffer`) with the same `synthesize` / `synthesizeStream`
 * method shape used by the other Patter TTS providers (ElevenLabs,
 * OpenAI). The stream yields raw MP3 bytes.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

const TELNYX_TTS_WS_URL = 'wss://api.telnyx.com/v2/text-to-speech/speech';
const DEFAULT_VOICE = 'Telnyx.NaturalHD.astra';

/** Streaming TTS adapter for Telnyx's `/v2/text-to-speech/speech` WebSocket. */
export class TelnyxTTS {
  constructor(
    private readonly apiKey: string,
    private readonly voice: string = DEFAULT_VOICE,
    private readonly baseUrl: string = TELNYX_TTS_WS_URL,
  ) {}

  /** Collect every audio chunk into a single Buffer. */
  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /**
   * Stream MP3-encoded audio chunks as they arrive from Telnyx.
   *
   * The server sends JSON frames of the shape `{"audio": "<base64-mp3>"}`.
   * Callers that need PCM must decode the MP3 bytes (e.g. via `ffmpeg`).
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const url = `${this.baseUrl}?voice=${encodeURIComponent(this.voice)}`;
    const ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Telnyx TTS connect timeout')), 10_000);
      ws.once('open', () => {
        clearTimeout(timer);
        resolve();
      });
      ws.once('error', (err) => {
        clearTimeout(timer);
        reject(err);
      });
    });

    // Queue for chunks. Null signals end-of-stream.
    type QueueItem = Buffer | null | { error: Error };
    const queue: QueueItem[] = [];
    const waiters: Array<(item: QueueItem) => void> = [];

    function push(item: QueueItem): void {
      const w = waiters.shift();
      if (w) {
        w(item);
      } else {
        queue.push(item);
      }
    }

    ws.on('message', (raw) => {
      let data: { audio?: string };
      try {
        data = JSON.parse(raw.toString()) as typeof data;
      } catch {
        getLogger().warn('TelnyxTTS: received invalid JSON');
        return;
      }

      const audioB64 = data.audio;
      if (!audioB64) return;

      try {
        const audioBytes = Buffer.from(audioB64, 'base64');
        if (audioBytes.length > 0) {
          push(audioBytes);
        }
      } catch {
        // Ignore malformed base64 frames
      }
    });

    ws.on('close', () => {
      push(null);
    });

    ws.on('error', (err) => {
      push({ error: err instanceof Error ? err : new Error(String(err)) });
    });

    // Protocol: send empty warm-up frame, then the text, then terminator.
    ws.send(JSON.stringify({ text: ' ' }));
    ws.send(JSON.stringify({ text }));
    ws.send(JSON.stringify({ text: '' }));

    try {
      while (true) {
        const item = queue.length > 0
          ? queue.shift()!
          : await new Promise<QueueItem>((resolve) => waiters.push(resolve));

        if (item === null) return;
        if (typeof item === 'object' && 'error' in item) throw item.error;
        yield item;
      }
    } finally {
      try {
        ws.close();
      } catch {
        // ignore
      }
    }
  }
}

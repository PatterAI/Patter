/**
 * OpenAI Whisper STT adapter for the Patter SDK pipeline mode.
 *
 * Buffers incoming PCM16 audio and periodically sends it to the
 * OpenAI Whisper transcription API as a WAV file.
 */

export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

const OPENAI_TRANSCRIPTION_URL = 'https://api.openai.com/v1/audio/transcriptions';

/** ~1 second of 16 kHz 16-bit mono audio. */
const DEFAULT_BUFFER_SIZE = 16000 * 2;

/**
 * Wrap raw PCM16 data in a minimal WAV container.
 *
 * Returns a Buffer containing a valid WAV file (RIFF header + data).
 */
function wrapPcmInWav(pcm: Buffer, sampleRate: number = 16000, channels: number = 1, bitsPerSample: number = 16): Buffer {
  const dataSize = pcm.length;
  const header = Buffer.alloc(44);

  // RIFF header
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + dataSize, 4);
  header.write('WAVE', 8);

  // fmt sub-chunk
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16); // sub-chunk size
  header.writeUInt16LE(1, 20);  // PCM format
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * channels * (bitsPerSample / 8), 28); // byte rate
  header.writeUInt16LE(channels * (bitsPerSample / 8), 32); // block align
  header.writeUInt16LE(bitsPerSample, 34);

  // data sub-chunk
  header.write('data', 36);
  header.writeUInt32LE(dataSize, 40);

  return Buffer.concat([header, pcm]);
}

export class WhisperSTT {
  private readonly apiKey: string;
  private readonly model: string;
  private readonly language: string | undefined;
  private readonly bufferSize: number;
  private buffer: Buffer = Buffer.alloc(0);
  private callbacks: TranscriptCallback[] = [];
  private running = false;

  constructor(
    apiKey: string,
    model: string = 'whisper-1',
    language?: string,
    bufferSize: number = DEFAULT_BUFFER_SIZE,
  ) {
    this.apiKey = apiKey;
    this.model = model;
    this.language = language;
    this.bufferSize = bufferSize;
  }

  /** Factory for Twilio calls — mulaw 8 kHz is transcoded upstream, so we still receive PCM 16-bit. */
  static forTwilio(apiKey: string, language: string = 'en', model: string = 'whisper-1'): WhisperSTT {
    return new WhisperSTT(apiKey, model, language);
  }

  async connect(): Promise<void> {
    this.running = true;
    this.buffer = Buffer.alloc(0);
  }

  sendAudio(audio: Buffer): void {
    if (!this.running) return;

    this.buffer = Buffer.concat([this.buffer, audio]);

    if (this.buffer.length >= this.bufferSize) {
      const pcm = this.buffer;
      this.buffer = Buffer.alloc(0);
      // Fire-and-forget — transcription runs in the background.
      void this.transcribeBuffer(pcm);
    }
  }

  onTranscript(callback: TranscriptCallback): void {
    if (this.callbacks.length >= 10) {
      console.warn('[PATTER] WhisperSTT: maximum of 10 onTranscript callbacks reached; replacing the last callback.');
      this.callbacks[this.callbacks.length - 1] = callback;
      return;
    }
    this.callbacks.push(callback);
  }

  close(): void {
    this.running = false;

    // Flush remaining buffer if it has enough audio (~25% of threshold).
    if (this.buffer.length >= this.bufferSize / 4) {
      const pcm = this.buffer;
      this.buffer = Buffer.alloc(0);
      void this.transcribeBuffer(pcm);
    } else {
      this.buffer = Buffer.alloc(0);
    }
  }

  // ------------------------------------------------------------------
  // Private
  // ------------------------------------------------------------------

  private async transcribeBuffer(pcm: Buffer): Promise<void> {
    const wav = wrapPcmInWav(pcm);

    const formData = new FormData();
    formData.append('file', new Blob([wav.buffer.slice(wav.byteOffset, wav.byteOffset + wav.byteLength) as BlobPart], { type: 'audio/wav' }), 'audio.wav');
    formData.append('model', this.model);
    if (this.language) {
      formData.append('language', this.language);
    }

    try {
      const resp = await fetch(OPENAI_TRANSCRIPTION_URL, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.apiKey}` },
        body: formData,
      });

      if (!resp.ok) {
        const body = await resp.text();
        console.error(`[PATTER] WhisperSTT transcription error: ${resp.status} ${body}`);
        return;
      }

      const json = (await resp.json()) as { text?: string };
      const text = (json.text ?? '').trim();
      if (!text) return;

      const transcript: Transcript = {
        text,
        isFinal: true,
        confidence: 1.0,
      };

      for (const cb of this.callbacks) {
        cb(transcript);
      }
    } catch (err) {
      console.error('[PATTER] WhisperSTT transcription error:', err);
    }
  }
}

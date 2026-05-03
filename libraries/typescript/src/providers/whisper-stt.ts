/**
 * OpenAI Whisper STT adapter for the Patter SDK pipeline mode.
 *
 * Buffers incoming PCM16 audio and periodically sends it to the
 * OpenAI Whisper transcription API as a WAV file.
 */

import { getLogger } from '../logger';

/** Patter-normalised transcript event emitted by {@link WhisperSTT}. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

const OPENAI_TRANSCRIPTION_URL = 'https://api.openai.com/v1/audio/transcriptions';

/** ~1 second of 16 kHz 16-bit mono audio. */
const DEFAULT_BUFFER_SIZE = 16000 * 2;

/** Models accepted by ``POST /v1/audio/transcriptions``. */
const ALLOWED_MODELS = new Set(['whisper-1', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe']);

/** Response format requested from `POST /v1/audio/transcriptions`. */
export type WhisperResponseFormat = 'json' | 'verbose_json';

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

/** Buffered STT adapter for OpenAI's Whisper transcription HTTP API. */
export class WhisperSTT {
  private readonly apiKey: string;
  private readonly model: string;
  private readonly language: string | undefined;
  private readonly bufferSize: number;
  private readonly responseFormat: WhisperResponseFormat;
  // Accumulate chunks in an array and concat once on flush — avoids the
  // per-``sendAudio`` O(n) ``Buffer.concat([buffer, chunk])`` that quickly
  // dominates CPU when the phone leg delivers 20 ms frames.
  private chunks: Buffer[] = [];
  private bufferedBytes = 0;
  private callbacks: Set<TranscriptCallback> = new Set();
  private running = false;
  private pendingTranscriptions: Promise<void>[] = [];

  /**
   * @param apiKey OpenAI API key.
   * @param language ISO-639-1 language code (e.g. ``"en"``, ``"it"``). Optional.
   * @param model One of ``whisper-1``, ``gpt-4o-transcribe``, ``gpt-4o-mini-transcribe``.
   * @param bufferSize Bytes of PCM16 to buffer before each transcription request.
   * @param responseFormat ``"json"`` (default) or ``"verbose_json"``.
   *
   * Argument order matches the Python SDK's ``WhisperSTT(api_key, language, model, response_format)``
   * for cross-language parity. Pre-0.5.3 the TS positional order was
   * ``(apiKey, model, language, bufferSize, responseFormat)`` — callers using
   * the old order will need to swap ``language`` and ``model``.
   */
  constructor(
    apiKey: string,
    language?: string,
    model: string = 'whisper-1',
    bufferSize: number = DEFAULT_BUFFER_SIZE,
    responseFormat: WhisperResponseFormat = 'json',
  ) {
    if (!ALLOWED_MODELS.has(model)) {
      throw new Error(
        `WhisperSTT: unsupported model "${model}". Expected one of ${[...ALLOWED_MODELS].join(', ')}.`,
      );
    }
    this.apiKey = apiKey;
    this.model = model;
    this.language = language;
    this.bufferSize = bufferSize;
    this.responseFormat = responseFormat;
  }

  /** Factory for Twilio calls — mulaw 8 kHz is transcoded upstream, so we still receive PCM 16-bit. */
  static forTwilio(apiKey: string, language: string = 'en', model: string = 'whisper-1'): WhisperSTT {
    return new WhisperSTT(apiKey, language, model);
  }

  /** Reset the audio buffer and arm the adapter for incoming chunks. */
  async connect(): Promise<void> {
    this.running = true;
    this.chunks = [];
    this.bufferedBytes = 0;
  }

  /** Buffer a PCM16 chunk; flushes to Whisper once `bufferSize` bytes are reached. */
  sendAudio(audio: Buffer): void {
    if (!this.running) return;

    this.chunks.push(audio);
    this.bufferedBytes += audio.length;

    if (this.bufferedBytes >= this.bufferSize) {
      const pcm = this.flushChunks();
      this.trackTranscription(this.transcribeBuffer(pcm));
    }
  }

  private flushChunks(): Buffer {
    const pcm = this.chunks.length === 1 ? this.chunks[0] : Buffer.concat(this.chunks, this.bufferedBytes);
    this.chunks = [];
    this.bufferedBytes = 0;
    return pcm;
  }

  private trackTranscription(promise: Promise<void>): void {
    const wrapped = promise.finally(() => {
      const idx = this.pendingTranscriptions.indexOf(wrapped);
      if (idx !== -1) this.pendingTranscriptions.splice(idx, 1);
    });
    this.pendingTranscriptions.push(wrapped);
  }

  /**
   * Register a transcript listener. Unlike the previous implementation
   * which capped at 10 and silently replaced the last one, we now keep all
   * registered callbacks in a Set; use {@link offTranscript} to remove one.
   */
  onTranscript(callback: TranscriptCallback): void {
    this.callbacks.add(callback);
  }

  /** Remove a previously registered transcript listener. */
  offTranscript(callback: TranscriptCallback): void {
    this.callbacks.delete(callback);
  }

  /** Flush any buffered audio, await pending transcriptions, and clear listeners. */
  async close(): Promise<void> {
    this.running = false;

    // Always flush whatever audio remains in the buffer so the trailing
    // 0–250 ms before end-of-utterance are not silently dropped. Previously
    // the buffer was only transcribed above ~25% of the threshold, which
    // discarded short tail-end utterances entirely.
    if (this.bufferedBytes > 0) {
      const pcm = this.flushChunks();
      this.trackTranscription(this.transcribeBuffer(pcm));
    }

    await Promise.allSettled(this.pendingTranscriptions);
    this.callbacks.clear();
  }

  // ------------------------------------------------------------------
  // Private
  // ------------------------------------------------------------------

  private async transcribeBuffer(pcm: Buffer): Promise<void> {
    const wav = wrapPcmInWav(pcm);

    const formData = new FormData();
    formData.append('file', new Blob([wav.buffer.slice(wav.byteOffset, wav.byteOffset + wav.byteLength) as BlobPart], { type: 'audio/wav' }), 'audio.wav');
    formData.append('model', this.model);
    formData.append('response_format', this.responseFormat);
    if (this.language) {
      formData.append('language', this.language);
    }

    try {
      const resp = await fetch(OPENAI_TRANSCRIPTION_URL, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.apiKey}` },
        body: formData,
        signal: AbortSignal.timeout(15_000),
      });

      if (!resp.ok) {
        const body = await resp.text();
        getLogger().error(`WhisperSTT transcription error: ${resp.status} ${body}`);
        return;
      }

      const json = (await resp.json()) as {
        text?: string;
        segments?: Array<{ avg_logprob?: number }>;
      };
      const text = (json.text ?? '').trim();
      if (!text) return;

      const transcript: Transcript = {
        text,
        isFinal: true,
        confidence: extractConfidence(json),
      };

      for (const cb of this.callbacks) {
        cb(transcript);
      }
    } catch (err) {
      getLogger().error(`WhisperSTT transcription error: ${String(err)}`);
    }
  }
}

function extractConfidence(payload: { segments?: Array<{ avg_logprob?: number }> }): number {
  // OpenAI's verbose_json returns per-segment ``avg_logprob``. We convert
  // to a probability via exp() and clamp. When the field is absent (plain
  // json) we return 1.0 to preserve prior behaviour.
  const segments = payload.segments;
  if (!segments || segments.length === 0) return 1.0;
  const scores: number[] = [];
  for (const seg of segments) {
    const logp = seg.avg_logprob;
    if (typeof logp === 'number') {
      scores.push(Math.max(0, Math.min(1, Math.exp(logp))));
    }
  }
  if (scores.length === 0) return 1.0;
  return scores.reduce((a, b) => a + b, 0) / scores.length;
}

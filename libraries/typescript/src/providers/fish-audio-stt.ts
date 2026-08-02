/**
 * Fish Audio Speech-to-Text adapter for the Patter SDK pipeline mode.
 *
 * **Beta: validated against the Fish Audio API spec (docs.fish.audio); not yet
 * exercised against a live phone call.**
 *
 * Fish exposes transcription as a **batch** endpoint only
 * (`POST https://api.fish.audio/v1/asr`) — there is no streaming socket. This
 * adapter therefore follows the same buffer-and-POST shape as
 * {@link WhisperSTT}: incoming PCM is accumulated, uploaded as a WAV once the
 * window fills, and the resulting text is emitted as a final transcript.
 *
 * Latency consequence: transcripts arrive in ~2 s windows rather than as
 * interim partials. For latency-sensitive agents prefer a genuinely streaming
 * STT (Deepgram, Soniox, AssemblyAI, Speechmatics). Use this adapter when you
 * want a single vendor for both directions of the call, or for Fish's language
 * coverage.
 *
 * Credential: `FISH_AUDIO_API_KEY`. Auth is an `Authorization: Bearer <key>` header.
 */

import { getLogger } from '../logger';
import { FISH_AUDIO_API_ORIGIN } from './fish-audio-tts';

/** Fish Audio batch transcription endpoint. */
export const FISH_AUDIO_ASR_URL = `${FISH_AUDIO_API_ORIGIN}/v1/asr`;

/** The adapter always uploads 16 kHz / 16-bit / mono WAV. */
const STT_SAMPLE_RATE = 16000;
const BYTES_PER_SECOND = STT_SAMPLE_RATE * 2;

/**
 * ~2 s of audio per upload. Fish rejects clips under 1 second, so a 1 s window
 * (what {@link WhisperSTT} uses) would sit exactly on the boundary; 2 s keeps
 * every steady-state upload comfortably inside the accepted range and halves
 * the request count.
 */
export const DEFAULT_BUFFER_SIZE = BYTES_PER_SECOND * 2;

/** Fish's documented minimum clip length — shorter tails are silence-padded. */
const MIN_AUDIO_BYTES = BYTES_PER_SECOND;

/** Fish's documented per-request ceiling (20 MB). */
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

/** One timed segment from a Fish transcription (only when timestamps are requested). */
export interface FishAudioSegment {
  readonly text: string;
  readonly start: number;
  readonly end: number;
}

/** Patter-normalised transcript event emitted by {@link FishAudioSTT}. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
  /** Per-segment timings, populated only when `ignoreTimestamps` is false. */
  readonly segments?: readonly FishAudioSegment[];
}

type TranscriptCallback = (transcript: Transcript) => void;

/** Constructor options for {@link FishAudioSTT}. */
export interface FishAudioSTTOptions {
  /**
   * `true` (default) skips segment timing, which Fish documents as the
   * lower-latency path for clips under 30 s. Set `false` to receive
   * per-segment `start` / `end` on the transcript.
   */
  ignoreTimestamps?: boolean;
  /** Bytes of 16 kHz PCM16 to accumulate before each upload. Default ~2 s. */
  bufferSize?: number;
  /** Override the REST endpoint (e.g. for tests). */
  baseUrl?: string;
}

/**
 * Wrap raw PCM16 data in a minimal WAV container (RIFF header + data).
 *
 * Kept local to the provider, matching `whisper-stt.ts` / `telnyx-stt.ts` /
 * `gemini-stt.ts`, which each carry their own copy.
 */
function wrapPcmInWav(pcm: Buffer, sampleRate: number = STT_SAMPLE_RATE): Buffer {
  const channels = 1;
  const bitsPerSample = 16;
  const dataSize = pcm.length;
  const header = Buffer.alloc(44);

  header.write('RIFF', 0);
  header.writeUInt32LE(36 + dataSize, 4);
  header.write('WAVE', 8);

  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * channels * (bitsPerSample / 8), 28);
  header.writeUInt16LE(channels * (bitsPerSample / 8), 32);
  header.writeUInt16LE(bitsPerSample, 34);

  header.write('data', 36);
  header.writeUInt32LE(dataSize, 40);

  return Buffer.concat([header, pcm]);
}

/** Buffered STT adapter for Fish Audio's batch transcription HTTP API. */
export class FishAudioSTT {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey: string = 'fish_audio_stt';

  private readonly apiKey: string;
  private readonly language: string | undefined;
  private readonly ignoreTimestamps: boolean;
  private readonly bufferSize: number;
  private readonly baseUrl: string;
  // Accumulate chunks in an array and concat once on flush — avoids the
  // per-`sendAudio` O(n) concat that dominates CPU on 20 ms carrier frames.
  private chunks: Buffer[] = [];
  private bufferedBytes = 0;
  private callbacks: Set<TranscriptCallback> = new Set();
  private running = false;
  private pendingTranscriptions: Promise<void>[] = [];
  /** Construction args replayed by clone(). */
  private readonly patterCtorArgs: unknown[];

  /**
   * @param apiKey Fish Audio API key.
   * @param language ISO language code (e.g. `"en"`). Omit to let Fish auto-detect.
   * @param opts Optional tuning — see {@link FishAudioSTTOptions}.
   *
   * Argument order matches the Python SDK's
   * `FishAudioSTT(api_key, language, *, ignore_timestamps=...)`.
   */
  constructor(apiKey: string, language?: string, opts: FishAudioSTTOptions = {}) {
    if (!apiKey) {
      throw new Error('Fish Audio STT: apiKey is required');
    }
    this.patterCtorArgs = [apiKey, language, opts];
    this.apiKey = apiKey;
    this.language = language;
    this.ignoreTimestamps = opts.ignoreTimestamps ?? true;
    this.bufferSize = opts.bufferSize ?? DEFAULT_BUFFER_SIZE;
    this.baseUrl = opts.baseUrl ?? FISH_AUDIO_ASR_URL;
  }

  /** Factory for Twilio calls — μ-law 8 kHz is transcoded upstream to PCM16 16 kHz. */
  static forTwilio(
    apiKey: string,
    language: string = 'en',
    opts: FishAudioSTTOptions = {},
  ): FishAudioSTT {
    return new FishAudioSTT(apiKey, language, opts);
  }

  /**
   * Fresh adapter built with this instance's construction arguments — called
   * per call by the stream handler so concurrent calls never share buffer state.
   */
  clone(): this {
    const ctor = this.constructor as new (...args: unknown[]) => this;
    return new ctor(...this.patterCtorArgs);
  }

  /** Reset the audio buffer and arm the adapter for incoming chunks. */
  async connect(): Promise<void> {
    this.running = true;
    this.chunks = [];
    this.bufferedBytes = 0;
  }

  /** Buffer a PCM16 chunk; uploads to Fish once `bufferSize` bytes are reached. */
  sendAudio(audio: Buffer): void {
    if (!this.running) return;

    this.chunks.push(audio);
    this.bufferedBytes += audio.length;

    if (this.bufferedBytes >= this.bufferSize) {
      const pcm = this.flushChunks();
      this.trackTranscription(this.transcribeBuffer(pcm));
    }
  }

  /** Register a transcript listener. */
  onTranscript(callback: TranscriptCallback): void {
    this.callbacks.add(callback);
  }

  /** Remove a previously registered transcript listener. */
  offTranscript(callback: TranscriptCallback): void {
    this.callbacks.delete(callback);
  }

  /**
   * Flush buffered audio, await pending uploads, and clear listeners.
   *
   * Fish rejects clips shorter than 1 second, so a short tail is padded with
   * digital silence up to the minimum rather than dropped — the alternative
   * would silently lose the last words of an utterance, the exact bug that was
   * fixed on the Whisper adapter.
   */
  async close(): Promise<void> {
    this.running = false;

    if (this.bufferedBytes > 0) {
      let pcm = this.flushChunks();
      if (pcm.length < MIN_AUDIO_BYTES) {
        pcm = Buffer.concat([pcm, Buffer.alloc(MIN_AUDIO_BYTES - pcm.length)]);
      }
      this.trackTranscription(this.transcribeBuffer(pcm));
    }

    await Promise.allSettled(this.pendingTranscriptions);
    this.callbacks.clear();
  }

  // ------------------------------------------------------------------
  // Private
  // ------------------------------------------------------------------

  private flushChunks(): Buffer {
    const pcm =
      this.chunks.length === 1
        ? this.chunks[0]
        : Buffer.concat(this.chunks, this.bufferedBytes);
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

  private async transcribeBuffer(pcmInput: Buffer): Promise<void> {
    let pcm = pcmInput;
    if (pcm.length > MAX_UPLOAD_BYTES) {
      getLogger().warn(
        `Fish Audio STT buffer is ${pcm.length} bytes, over the ` +
          `${MAX_UPLOAD_BYTES}-byte per-request limit; truncating to the most recent audio.`,
      );
      pcm = pcm.subarray(pcm.length - MAX_UPLOAD_BYTES);
    }

    const wav = wrapPcmInWav(pcm);
    const bytes = wav.buffer.slice(
      wav.byteOffset,
      wav.byteOffset + wav.byteLength,
    ) as BlobPart;

    const formData = new FormData();
    formData.append('audio', new Blob([bytes], { type: 'audio/wav' }), 'audio.wav');
    if (this.language) formData.append('language', this.language);
    formData.append('ignore_timestamps', this.ignoreTimestamps ? 'true' : 'false');

    try {
      const resp = await fetch(this.baseUrl, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.apiKey}` },
        body: formData,
        signal: AbortSignal.timeout(30_000),
      });

      if (!resp.ok) {
        const body = await resp.text();
        getLogger().error(`Fish Audio STT error: ${resp.status} ${body}`);
        return;
      }

      const json = (await resp.json()) as {
        text?: string;
        duration?: number;
        segments?: FishAudioSegment[];
      };
      const text = (json.text ?? '').trim();
      if (!text) return;

      // Fish reports no confidence score, so it is fixed at 1.0.
      const transcript: Transcript = {
        text,
        isFinal: true,
        confidence: 1.0,
        ...(Array.isArray(json.segments) ? { segments: json.segments } : {}),
      };

      for (const cb of this.callbacks) {
        cb(transcript);
      }
    } catch (err) {
      getLogger().error(`Fish Audio STT transcription error: ${String(err)}`);
    }
  }
}

/**
 * Gemini multimodal STT adapter for Patter pipeline mode.
 *
 * Buffers inbound PCM16 (16 kHz) and, when the turn ends (`finalize()` fired
 * by the pipeline at VAD speech-end), sends the whole utterance to Google's
 * `gemini-2.5-flash` as audio. The model both transcribes AND reads the
 * caller's emotional tone from how they sound, emitting:
 *
 *     [tone: <1-2 words>] <verbatim transcript>
 *
 * The tone prefix flows downstream to the LLM so the agent can mirror mood
 * (a flat, tired caller → a gentler reply), which a words-only transcriber
 * cannot do. Turn-based (not chunked) so tone is judged over the full
 * utterance.
 *
 * Install peer dep:  npm install @google/genai
 */

import { getLogger } from '../logger';

/** Patter-normalised transcript event. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

const TONE_PROMPT =
  'You are the transcription stage of a live phone call. Transcribe the caller ' +
  'verbatim, then judge their emotional tone from HOW they sound — energy, pace, ' +
  'pitch — not just the words. Output exactly one line and nothing else: ' +
  '[tone: <1-2 words>] <transcript>. If there is no intelligible speech, output an empty line.';

/** Minimal PCM16-mono WAV container (Gemini accepts audio/wav inline). */
function wrapPcmInWav(pcm: Buffer, sampleRate: number): Buffer {
  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + pcm.length, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(1, 22); // mono
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write('data', 36);
  header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}

/** Buffered multimodal STT adapter using Gemini for transcription + tone. */
export class GeminiSTT {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey: string = 'gemini_stt';
  private client: unknown = null;
  private chunks: Buffer[] = [];
  private bufferedBytes = 0;
  private callbacks = new Set<TranscriptCallback>();
  private running = false;
  private pending: Promise<void>[] = [];
  private readonly ctorArgs: unknown[];

  /**
   * @param apiKey Google Generative Language API key.
   * @param model Multimodal model (default `gemini-2.5-flash`).
   * @param sampleRate Inbound PCM rate from the pipeline (default 16000).
   */
  constructor(
    private readonly apiKey: string,
    private readonly model: string = 'gemini-2.5-flash',
    private readonly sampleRate: number = 16000,
  ) {
    this.ctorArgs = [apiKey, model, sampleRate];
  }

  /** Per-call fresh adapter so concurrent calls never share buffers. */
  clone(): this {
    const Ctor = this.constructor as new (...args: unknown[]) => this;
    return new Ctor(...this.ctorArgs);
  }

  async connect(): Promise<void> {
    await this.ensureClient();
    this.running = true;
    this.chunks = [];
    this.bufferedBytes = 0;
  }

  /** Buffer a PCM16 chunk (transcription happens at turn-end via finalize). */
  sendAudio(pcm: Buffer): void {
    if (!this.running || pcm.length === 0) return;
    this.chunks.push(pcm);
    this.bufferedBytes += pcm.length;
  }

  onTranscript(callback: TranscriptCallback): void {
    this.callbacks.add(callback);
  }

  offTranscript(callback: TranscriptCallback): void {
    this.callbacks.delete(callback);
  }

  /** Turn ended (VAD speech-end): transcribe the buffered utterance. */
  finalize(): void {
    if (this.bufferedBytes === 0) return;
    this.track(this.transcribe(this.flush()));
  }

  async close(): Promise<void> {
    this.running = false;
    if (this.bufferedBytes > 0) this.track(this.transcribe(this.flush()));
    await Promise.allSettled(this.pending);
    this.callbacks.clear();
  }

  // ------------------------------------------------------------------ private

  private flush(): Buffer {
    const pcm = this.chunks.length === 1 ? this.chunks[0] : Buffer.concat(this.chunks, this.bufferedBytes);
    this.chunks = [];
    this.bufferedBytes = 0;
    return pcm;
  }

  private track(promise: Promise<void>): void {
    const wrapped = promise.finally(() => {
      const idx = this.pending.indexOf(wrapped);
      if (idx !== -1) this.pending.splice(idx, 1);
    });
    this.pending.push(wrapped);
  }

  private async transcribe(pcm: Buffer): Promise<void> {
    try {
      await this.ensureClient();
      const ai = this.client as {
        models?: { generateContent?: (a: unknown) => Promise<unknown> };
      };
      if (!ai?.models?.generateContent) return;
      const wav = wrapPcmInWav(pcm, this.sampleRate);
      const res = await ai.models.generateContent({
        model: this.model,
        contents: [{ role: 'user', parts: [
          { text: TONE_PROMPT },
          { inlineData: { mimeType: 'audio/wav', data: wav.toString('base64') } },
        ] }],
        config: { temperature: 0.2, maxOutputTokens: 300 },
      });
      const text = extractText(res).trim();
      if (!text) return;
      const transcript: Transcript = { text, isFinal: true, confidence: 1.0 };
      for (const cb of this.callbacks) cb(transcript);
    } catch (err) {
      getLogger().error(`GeminiSTT transcribe error: ${String(err)}`);
    }
  }

  private async ensureClient(): Promise<void> {
    if (this.client) return;
    let genai: { GoogleGenAI: new (a: { apiKey: string }) => unknown };
    try {
      genai = (await import('@google/genai')) as typeof genai;
    } catch {
      throw new Error('GeminiSTT requires "@google/genai". Install: npm install @google/genai');
    }
    this.client = new genai.GoogleGenAI({ apiKey: this.apiKey });
  }
}

function extractText(res: unknown): string {
  const direct = (res as { text?: string }).text;
  if (typeof direct === 'string') return direct;
  const parts = (res as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> })
    .candidates?.[0]?.content?.parts;
  return (parts ?? []).map((p) => p.text ?? '').join('');
}

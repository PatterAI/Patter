/**
 * Gemini TTS adapter for Patter pipeline mode.
 *
 * Wraps Google's `gemini-3.1-flash-tts-preview` speech model via
 * `@google/genai` `generateContentStream`. Streams PCM L16 24 kHz and
 * resamples to the pipeline's expected 16 kHz (the stream handler does the
 * final 16k→8k mulaw step for the carrier).
 *
 * Inline square-bracket delivery tags in the text — `[warm]`, `[short pause]`,
 * `[sigh]` — are honoured by the model and shape prosody rather than being
 * spoken, which is why the demo persona annotates its replies.
 *
 * Install peer dep:  npm install @google/genai
 */

import { getLogger } from '../logger';
import { StatefulResampler } from '../audio/transcoding';

/** Native Gemini TTS output rate. */
const TTS_SOURCE_SR = 24000;
/** Default pipeline-facing rate (stream handler resamples 16k→8k for Twilio). */
const DEFAULT_TARGET_SR = 16000;

/** Streaming Gemini TTS adapter (Kore voice by default). */
export class GeminiTTS {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey: string = 'gemini_tts';
  private client: unknown = null;

  /**
   * @param apiKey Google Generative Language API key.
   * @param voice Prebuilt TTS voice name (default `Kore`).
   * @param model TTS model (default `gemini-3.1-flash-tts-preview`).
   * @param targetSampleRate Output PCM rate: 8000 or 16000 (default 16000).
   */
  constructor(
    private readonly apiKey: string,
    private readonly voice: string = 'Kore',
    private readonly model: string = 'gemini-3.1-flash-tts-preview',
    private readonly targetSampleRate: number = DEFAULT_TARGET_SR,
  ) {
    if (targetSampleRate !== 8000 && targetSampleRate !== 16000) {
      throw new Error('GeminiTTS: targetSampleRate must be 8000 or 16000');
    }
  }

  /** Force the model connection warm so the first real synth is fast. */
  async warmup(): Promise<void> {
    try {
      // Drain a tiny synthesis to establish the HTTP/2 connection + model warm.
      for await (const _ of this.synthesizeStream('Hello.')) { /* discard */ }
    } catch (e) {
      getLogger().warn(`GeminiTTS warmup: ${String(e)}`);
    }
  }

  /** Synthesise `text` and yield PCM16-LE chunks at {@link targetSampleRate}. */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    if (!text.trim()) return;
    await this.ensureClient();
    const ai = this.client as {
      models?: { generateContentStream?: (a: unknown) => Promise<AsyncIterable<unknown>> };
    };
    if (!ai?.models?.generateContentStream) {
      throw new Error('GeminiTTS: @google/genai generateContentStream unavailable');
    }
    // Fresh resampler per call: each synthesis is an independent PCM stream, so
    // carrying phase state across calls would corrupt the first frames.
    const resampler =
      this.targetSampleRate === TTS_SOURCE_SR
        ? null
        : new StatefulResampler({ srcRate: TTS_SOURCE_SR, dstRate: this.targetSampleRate });

    const stream = await ai.models.generateContentStream({
      model: this.model,
      contents: [{ role: 'user', parts: [{ text }] }],
      config: {
        responseModalities: ['AUDIO'],
        speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: this.voice } } },
      },
    });

    for await (const chunk of stream) {
      const c = chunk as {
        candidates?: Array<{ content?: { parts?: Array<{ inlineData?: { data?: string } }> } }>;
      };
      const data = c.candidates?.[0]?.content?.parts?.[0]?.inlineData?.data;
      if (!data) continue;
      const pcm24 = Buffer.from(data, 'base64');
      if (pcm24.length === 0) continue;
      const out = resampler ? resampler.process(pcm24) : pcm24;
      if (out.length > 0) yield out;
    }
  }

  private async ensureClient(): Promise<void> {
    if (this.client) return;
    let genai: { GoogleGenAI: new (a: { apiKey: string }) => unknown };
    try {
      genai = (await import('@google/genai')) as typeof genai;
    } catch {
      throw new Error('GeminiTTS requires "@google/genai". Install: npm install @google/genai');
    }
    this.client = new genai.GoogleGenAI({ apiKey: this.apiKey });
  }
}

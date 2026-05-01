/**
 * OpenAI GPT-4o Transcribe STT adapter for the Patter SDK pipeline mode.
 *
 * First-class wrapper around OpenAI's ``gpt-4o-transcribe`` /
 * ``gpt-4o-mini-transcribe`` models. They share the
 * ``POST /v1/audio/transcriptions`` endpoint with Whisper-1 but offer ~10x
 * lower latency and stronger multilingual quality, making them a drop-in
 * replacement for ``WhisperSTT`` whenever speed matters.
 *
 * Use this class instead of ``WhisperSTT`` when you specifically want the
 * GPT-4o Transcribe family — it restricts the accepted models so
 * misconfigured calls fail fast instead of silently dropping back to
 * ``whisper-1``.
 */

import { WhisperSTT, type WhisperResponseFormat } from './whisper-stt';

const ALLOWED_MODELS = new Set(['gpt-4o-transcribe', 'gpt-4o-mini-transcribe']);

/** ~1 second of 16 kHz 16-bit mono audio — same default as WhisperSTT. */
const DEFAULT_BUFFER_SIZE = 16000 * 2;

export class OpenAITranscribeSTT extends WhisperSTT {
  /**
   * @param apiKey OpenAI API key.
   * @param language ISO-639-1 language code (e.g. ``"en"``, ``"it"``). Optional.
   * @param model One of ``gpt-4o-transcribe`` (default), ``gpt-4o-mini-transcribe``.
   *   ``"whisper-1"`` is intentionally rejected here — use ``WhisperSTT`` for that.
   * @param bufferSize Bytes of PCM16 to buffer before each transcription request.
   * @param responseFormat ``"json"`` (default) or ``"verbose_json"``.
   */
  constructor(
    apiKey: string,
    language?: string,
    model: string = 'gpt-4o-transcribe',
    bufferSize: number = DEFAULT_BUFFER_SIZE,
    responseFormat: WhisperResponseFormat = 'json',
  ) {
    if (!ALLOWED_MODELS.has(model)) {
      throw new Error(
        `OpenAITranscribeSTT: unsupported model "${model}". Expected one of ${[...ALLOWED_MODELS].join(', ')}. ` +
          `For "whisper-1", use WhisperSTT instead.`,
      );
    }
    super(apiKey, language, model, bufferSize, responseFormat);
  }
}

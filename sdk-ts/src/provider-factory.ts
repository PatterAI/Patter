/**
 * Shared STT / TTS adapter dispatch.
 *
 * In v0.5.0+ callers always pass pre-instantiated adapters (``agent.stt`` /
 * ``agent.tts`` are ``STTAdapter`` / ``TTSAdapter`` instances), so these
 * helpers are thin pass-throughs that return the instance or null. Kept as
 * functions so the Twilio/Telnyx bridges have a single dispatch point.
 */
import type { AgentOptions } from './types';

/** Per-word timings / metadata (Deepgram-shaped). Optional on every adapter. */
export interface STTWord {
  readonly word?: string;
  readonly start?: number;
  readonly end?: number;
  readonly confidence?: number;
  readonly punctuated_word?: string;
  readonly speaker?: number;
}

/**
 * Facade transcript shape — widened to surface richer provider fields
 * (Deepgram emits all of them) without forcing adapters that only know
 * ``text``/``isFinal`` to change. All non-text fields are optional.
 */
export interface STTTranscript {
  text: string;
  isFinal?: boolean;
  /** Overall transcript confidence in [0, 1]. */
  confidence?: number;
  /** Provider-side end-of-utterance hint (faster than ``isFinal``). */
  speechFinal?: boolean;
  /** True when the result was produced in response to a Finalize command. */
  fromFinalize?: boolean;
  /** Provider request id (Deepgram populates this from the Metadata frame). */
  requestId?: string;
  /** Per-word timings / metadata when the provider emits them. */
  words?: ReadonlyArray<STTWord>;
  /** Which provider event this transcript represents (e.g. ``Results``). */
  eventType?: string;
}

export type STTTranscriptCallback = (t: STTTranscript) => Promise<void> | void;

/** Shape shared by every STT adapter in the SDK. */
export interface STTAdapter {
  connect(): Promise<void>;
  sendAudio(pcm: Buffer): void | Promise<void>;
  onTranscript(cb: STTTranscriptCallback): void;
  close(): void | Promise<void>;
}

export interface TTSAdapter {
  synthesizeStream(text: string): AsyncIterable<Buffer>;
}

/**
 * Return the STT adapter instance attached to ``agent``, or null when no STT
 * is configured. In v0.5.0+ ``agent.stt`` is always an adapter instance.
 */
export async function createSTT(agent: AgentOptions): Promise<STTAdapter | null> {
  return agent.stt ?? null;
}

/** Return the TTS adapter instance attached to ``agent``, or null. */
export async function createTTS(agent: AgentOptions): Promise<TTSAdapter | null> {
  return agent.tts ?? null;
}

/**
 * Shared STT / TTS adapter dispatch.
 *
 * In v0.5.0+ callers always pass pre-instantiated adapters (``agent.stt`` /
 * ``agent.tts`` are ``STTAdapter`` / ``TTSAdapter`` instances), so these
 * helpers are thin pass-throughs that return the instance or null. Kept as
 * functions so the Twilio/Telnyx bridges have a single dispatch point.
 */
import type { AgentOptions } from './types';

export interface STTTranscript {
  text: string;
  isFinal?: boolean;
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

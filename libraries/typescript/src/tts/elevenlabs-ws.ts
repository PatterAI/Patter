/**
 * Backward-compat alias. As of 0.6.1, `getpatter/tts/elevenlabs` defaults to
 * WebSocket streaming, so this module re-exports the same `TTS` facade. The
 * `getpatter/tts/elevenlabs-ws` import path is preserved so existing user
 * code keeps working without changes.
 */
export { TTS, type ElevenLabsTTSOptions as ElevenLabsWebSocketOptions } from "./elevenlabs";
export type { ElevenLabsModel } from "../providers/elevenlabs-tts";

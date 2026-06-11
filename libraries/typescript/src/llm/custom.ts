/**
 * Custom LLM — point Patter's pipeline at ANY OpenAI-compatible endpoint.
 *
 * The industry-standard "Custom LLM" pattern (the name the voice-AI
 * ecosystem uses for this concept): Patter owns the phone leg
 * — carrier, STT, turn-taking, barge-in, TTS — and POSTs each conversation
 * turn to YOUR ``/chat/completions`` endpoint. That endpoint can be:
 *
 * - an **agent runtime** (Hermes, OpenClaw — prefer the dedicated presets in
 *   ``llm/hermes`` / ``llm/openclaw``, thin subclasses of this same engine
 *   with the right defaults baked in),
 * - a **local inference gateway** (Ollama, vLLM, LM Studio — keyless OK),
 * - or **your own service** that speaks the OpenAI Chat Completions protocol
 *   (SSE streaming, optional tool calls).
 *
 * ``CustomLLM`` is the canonical name for the generic engine
 * ({@link OpenAICompatibleLLMProvider}): same streaming loop, same barge-in
 * cancellation, same opt-in session continuity (per-call ``user`` field,
 * per-call session-id header, and a static or factory-derived memory-scope
 * header).
 *
 * @example
 * ```ts
 * import { CustomLLM } from "getpatter";
 *
 * // Your own agent service (any OpenAI-compatible /chat/completions):
 * const llm = new CustomLLM({
 *   baseUrl: "http://127.0.0.1:9000/v1",
 *   model: "my-agent",
 *   apiKeyEnv: "MY_AGENT_KEY",
 *   timeout: 120,                  // agent runtimes run tools before replying
 * });
 *
 * // Keyless local gateway (Ollama / vLLM / LM Studio):
 * const llm = new CustomLLM({ baseUrl: "http://127.0.0.1:11434/v1", model: "llama3.1" });
 *
 * // Per-call session continuity + per-caller long-term memory, on a runtime
 * // that scopes sessions/memory by header:
 * const llm = new CustomLLM({
 *   baseUrl: "http://127.0.0.1:9000/v1",
 *   model: "my-agent",
 *   sessionIdHeader: "X-My-Session-Id",   // value = `${prefix}${callId}`
 *   sessionIdPrefix: "patter-call-",
 *   sessionKeyHeader: "X-My-Memory-Key",
 *   sessionKeyFrom: "caller_hash",        // patter-caller-<hash>
 * });
 * ```
 */
import {
  OpenAICompatibleLLMProvider,
  type OpenAICompatibleLLMOptions,
} from './openai-compatible';

/** Constructor options for the generic Custom LLM provider. */
export type CustomLLMOptions = OpenAICompatibleLLMOptions;

/**
 * Generic "Custom LLM" provider for any OpenAI-compatible endpoint.
 *
 * All constructor options are inherited from
 * {@link OpenAICompatibleLLMOptions} (``baseUrl`` and ``model`` are
 * required). The Hermes / OpenClaw presets are subclasses of the same engine
 * — use them when they exist; use this for everything else.
 */
export class LLM extends OpenAICompatibleLLMProvider {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey: string = 'custom';
}

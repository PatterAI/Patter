/** ElevenLabs ConvAI engine — marker class for Patter client dispatch. */

export interface ConvAIOptions {
  /** API key. Falls back to ELEVENLABS_API_KEY env var when omitted. */
  apiKey?: string;
  /** ElevenLabs Agent ID. Falls back to ELEVENLABS_AGENT_ID env var when omitted. */
  agentId?: string;
  /** Voice ID to override the agent's default voice. */
  voice?: string;
}

/**
 * ElevenLabs ConvAI engine marker.
 *
 * @example
 * ```ts
 * import * as elevenlabs from "getpatter/engines/elevenlabs";
 * const engine = new elevenlabs.ConvAI();                   // reads env vars
 * const engine = new elevenlabs.ConvAI({ agentId: "agent_..." });
 * ```
 */
export class ConvAI {
  readonly kind = "elevenlabs_convai" as const;
  readonly apiKey: string;
  readonly agentId: string;
  readonly voice: string | undefined;

  constructor(opts: ConvAIOptions = {}) {
    const key = opts.apiKey ?? process.env.ELEVENLABS_API_KEY;
    const agent = opts.agentId ?? process.env.ELEVENLABS_AGENT_ID;
    if (!key) {
      throw new Error(
        "ElevenLabs ConvAI requires an apiKey. Pass { apiKey: '...' } or " +
          "set ELEVENLABS_API_KEY in the environment.",
      );
    }
    if (!agent) {
      throw new Error(
        "ElevenLabs ConvAI requires an agentId. Create one in the ElevenLabs " +
          "dashboard (https://elevenlabs.io/app/conversational-ai) — the " +
          "agent ID is per-deployed-agent and cannot be derived from the " +
          "API key alone. Then either pass { agentId: 'agent_...' } at " +
          "construction or set ELEVENLABS_AGENT_ID in the environment.",
      );
    }
    this.apiKey = key;
    this.agentId = agent;
    this.voice = opts.voice;
  }
}

/**
 * Default provider pricing and merge utilities.
 *
 * Pricing is based on public provider rates (as of early 2025).
 * Developers can override any provider's pricing.
 */

export interface ProviderPricing {
  unit: string;
  price?: number;
  audio_input_per_token?: number;
  audio_output_per_token?: number;
  text_input_per_token?: number;
  text_output_per_token?: number;
}

export const DEFAULT_PRICING: Record<string, ProviderPricing> = {
  // STT — per minute of audio processed
  deepgram: { unit: 'minute', price: 0.0043 },
  whisper: { unit: 'minute', price: 0.006 },
  // TTS — per 1,000 characters synthesized
  elevenlabs: { unit: '1k_chars', price: 0.18 },
  openai_tts: { unit: '1k_chars', price: 0.015 },
  // OpenAI Realtime — per token
  openai_realtime: {
    unit: 'token',
    audio_input_per_token: 0.0001,
    audio_output_per_token: 0.0004,
    text_input_per_token: 0.000005,
    text_output_per_token: 0.00002,
  },
  // Telephony — per minute of call duration
  twilio: { unit: 'minute', price: 0.013 },
  telnyx: { unit: 'minute', price: 0.007 },
};

/**
 * Merge user overrides into a copy of DEFAULT_PRICING.
 * Performs a shallow per-provider merge.
 */
export function mergePricing(
  overrides?: Record<string, Partial<ProviderPricing>> | null,
): Record<string, ProviderPricing> {
  const merged: Record<string, ProviderPricing> = {};
  for (const [k, v] of Object.entries(DEFAULT_PRICING)) {
    merged[k] = { ...v };
  }
  if (!overrides) return merged;
  for (const [provider, values] of Object.entries(overrides)) {
    if (merged[provider]) {
      merged[provider] = { ...merged[provider], ...values };
    } else {
      merged[provider] = { unit: 'minute', ...values } as ProviderPricing;
    }
  }
  return merged;
}

/** Calculate STT cost from audio duration. */
export function calculateSttCost(
  provider: string,
  audioSeconds: number,
  pricing: Record<string, ProviderPricing>,
): number {
  const config = pricing[provider];
  if (!config || config.unit !== 'minute') return 0;
  return (audioSeconds / 60) * (config.price ?? 0);
}

/** Calculate TTS cost from character count. */
export function calculateTtsCost(
  provider: string,
  characterCount: number,
  pricing: Record<string, ProviderPricing>,
): number {
  const config = pricing[provider];
  if (!config || config.unit !== '1k_chars') return 0;
  return (characterCount / 1000) * (config.price ?? 0);
}

/** Calculate OpenAI Realtime cost from token usage. */
export function calculateRealtimeCost(
  usage: {
    input_token_details?: { audio_tokens?: number; text_tokens?: number };
    output_token_details?: { audio_tokens?: number; text_tokens?: number };
  },
  pricing: Record<string, ProviderPricing>,
): number {
  const config = pricing.openai_realtime;
  if (!config || config.unit !== 'token') return 0;

  const input = usage.input_token_details ?? {};
  const output = usage.output_token_details ?? {};

  let cost = 0;
  cost += (input.audio_tokens ?? 0) * (config.audio_input_per_token ?? 0);
  cost += (input.text_tokens ?? 0) * (config.text_input_per_token ?? 0);
  cost += (output.audio_tokens ?? 0) * (config.audio_output_per_token ?? 0);
  cost += (output.text_tokens ?? 0) * (config.text_output_per_token ?? 0);
  return cost;
}

/** Calculate telephony cost from call duration. */
export function calculateTelephonyCost(
  provider: string,
  durationSeconds: number,
  pricing: Record<string, ProviderPricing>,
): number {
  const config = pricing[provider];
  if (!config || config.unit !== 'minute') return 0;
  return (durationSeconds / 60) * (config.price ?? 0);
}

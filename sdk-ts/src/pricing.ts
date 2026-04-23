/**
 * Default provider pricing and merge utilities.
 *
 * Pricing reflects public provider rates as of 2026. These defaults
 * are calibrated for the default models Patter ships with — notably
 * ``gpt-4o-mini-realtime-preview`` for OpenAI Realtime. If you pick
 * a different model (e.g. ``gpt-4o-realtime-preview`` or
 * ``gpt-realtime``), override the ``openai_realtime`` entry via the
 * ``pricing`` option on ``Patter()`` so the dashboard cost display
 * matches what OpenAI actually bills.
 */

/** Pricing table version identifier, updated in lockstep with sdk-py. */
export const PRICING_VERSION = '2026.1';
export const PRICING_LAST_UPDATED = '2026-04-23';

export interface ProviderPricing {
  unit: string;
  price?: number;
  audio_input_per_token?: number;
  audio_output_per_token?: number;
  text_input_per_token?: number;
  text_output_per_token?: number;
  cached_audio_input_per_token?: number;
  cached_text_input_per_token?: number;
}

export const DEFAULT_PRICING: Record<string, ProviderPricing> = {
  // STT — per minute of audio processed
  deepgram: { unit: 'minute', price: 0.0043 },
  whisper: { unit: 'minute', price: 0.006 },
  // TTS — per 1,000 characters synthesized
  elevenlabs: { unit: '1k_chars', price: 0.18 },
  openai_tts: { unit: '1k_chars', price: 0.015 },
  // OpenAI Realtime — per token.
  // Calibrated for gpt-4o-mini-realtime-preview (the Patter default):
  //   audio  input  $10 / M  ->  0.00001 per token
  //   audio  output $20 / M  ->  0.00002 per token
  //   text   input  $0.60/ M ->  0.0000006 per token
  //   text   output $2.40/ M ->  0.0000024 per token
  // For gpt-4o-realtime-preview multiply by ~10, for gpt-realtime by ~3.
  openai_realtime: {
    unit: 'token',
    audio_input_per_token: 0.00001,
    audio_output_per_token: 0.00002,
    text_input_per_token: 0.0000006,
    text_output_per_token: 0.0000024,
    // Prompt caching rates (official): audio cached $0.30/M ~= 3% of full,
    // text cached $0.06/M = 10% of full. OpenAI bills the cached portion of
    // input_token_details.audio_tokens / text_tokens at these reduced rates.
    cached_audio_input_per_token: 0.0000003,
    cached_text_input_per_token: 0.00000006,
  },
  // Telephony — per minute of call duration.
  // twilio default = US inbound local (the 99% case for voice agents receiving
  // calls on a local number). For US toll-free inbound ($0.022/min) or US
  // outbound local ($0.0140/min), override via Patter({ pricing: { twilio: {...} } }).
  twilio: { unit: 'minute', price: 0.0085 },
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
      // Fail-closed: when the user registers a brand-new provider without a
      // ``unit`` field, leave it missing so ``calculate_*_cost`` returns 0
      // instead of silently billing as minutes. Matches sdk-py behaviour.
      merged[provider] = { ...values } as ProviderPricing;
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

/**
 * Calculate OpenAI Realtime cost from token usage.
 *
 * OpenAI bills the cached portion of ``input_token_details.audio_tokens`` and
 * ``.text_tokens`` at the reduced cached rate (typically ~3% of full for audio,
 * ~10% of full for text on the mini model). ``cached_tokens_details`` is a
 * nested breakdown of the same ``input_token_details`` totals — the cached
 * counts are already INCLUDED in the top-level totals, so we subtract them
 * out before applying the full rate and add them back at the cached rate.
 */
export function calculateRealtimeCost(
  usage: {
    input_token_details?: {
      audio_tokens?: number;
      text_tokens?: number;
      cached_tokens_details?: { audio_tokens?: number; text_tokens?: number };
    };
    output_token_details?: { audio_tokens?: number; text_tokens?: number };
  },
  pricing: Record<string, ProviderPricing>,
): number {
  const config = pricing.openai_realtime;
  if (!config || config.unit !== 'token') return 0;

  const input = usage.input_token_details ?? {};
  const output = usage.output_token_details ?? {};
  const cached = input.cached_tokens_details ?? {};

  const cachedAudioRate = config.cached_audio_input_per_token ?? config.audio_input_per_token ?? 0;
  const cachedTextRate = config.cached_text_input_per_token ?? config.text_input_per_token ?? 0;

  const totalAudioIn = input.audio_tokens ?? 0;
  const totalTextIn = input.text_tokens ?? 0;
  const cachedAudioIn = Math.min(cached.audio_tokens ?? 0, totalAudioIn);
  const cachedTextIn = Math.min(cached.text_tokens ?? 0, totalTextIn);

  let cost = 0;
  cost += (totalAudioIn - cachedAudioIn) * (config.audio_input_per_token ?? 0);
  cost += cachedAudioIn * cachedAudioRate;
  cost += (totalTextIn - cachedTextIn) * (config.text_input_per_token ?? 0);
  cost += cachedTextIn * cachedTextRate;
  cost += (output.audio_tokens ?? 0) * (config.audio_output_per_token ?? 0);
  cost += (output.text_tokens ?? 0) * (config.text_output_per_token ?? 0);
  return cost;
}

/**
 * How much would have been paid if the cached portion of the input tokens
 * had been billed at the full rate. Used to expose a "saved from prompt
 * caching" figure on the dashboard.
 */
export function calculateRealtimeCachedSavings(
  usage: {
    input_token_details?: {
      audio_tokens?: number;
      text_tokens?: number;
      cached_tokens_details?: { audio_tokens?: number; text_tokens?: number };
    };
  },
  pricing: Record<string, ProviderPricing>,
): number {
  const config = pricing.openai_realtime;
  if (!config || config.unit !== 'token') return 0;
  const input = usage.input_token_details ?? {};
  const cached = input.cached_tokens_details ?? {};
  const cachedAudioRate = config.cached_audio_input_per_token ?? config.audio_input_per_token ?? 0;
  const cachedTextRate = config.cached_text_input_per_token ?? config.text_input_per_token ?? 0;
  const cachedAudio = Math.min(cached.audio_tokens ?? 0, input.audio_tokens ?? 0);
  const cachedText = Math.min(cached.text_tokens ?? 0, input.text_tokens ?? 0);
  const fullAudio = cachedAudio * (config.audio_input_per_token ?? 0);
  const fullText = cachedText * (config.text_input_per_token ?? 0);
  const discountedAudio = cachedAudio * cachedAudioRate;
  const discountedText = cachedText * cachedTextRate;
  return (fullAudio + fullText) - (discountedAudio + discountedText);
}

/**
 * Calculate telephony cost from call duration.
 *
 * Twilio bills in whole-minute increments (any partial minute is rounded up
 * to the next full minute per twilio.com/help/223132307). Telnyx bills
 * per-second. We detect Twilio by provider name and apply the round-up.
 */
export function calculateTelephonyCost(
  provider: string,
  durationSeconds: number,
  pricing: Record<string, ProviderPricing>,
): number {
  const config = pricing[provider];
  if (!config || config.unit !== 'minute') return 0;
  const minutes = provider === 'twilio'
    ? Math.ceil(durationSeconds / 60)
    : durationSeconds / 60;
  return minutes * (config.price ?? 0);
}

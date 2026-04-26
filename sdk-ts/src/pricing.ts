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
export const PRICING_VERSION = '2026.2';
export const PRICING_LAST_UPDATED = '2026-04-24';

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
  // Deepgram Nova-3 streaming (monolingual) — the default model Patter ships.
  // The previous $0.0043/min was the batch rate; streaming is $0.0077/min per
  // deepgram.com/pricing. For multilingual Nova-3 ($0.0092/min) override.
  deepgram: { unit: 'minute', price: 0.0077 },
  whisper: { unit: 'minute', price: 0.006 },
  // AssemblyAI Universal-Streaming — $0.15/hr = $0.0025/min
  assemblyai: { unit: 'minute', price: 0.0025 },
  // Cartesia ink-whisper streaming STT — ~$0.15/hr on usage plans
  cartesia_stt: { unit: 'minute', price: 0.0025 },
  // Soniox real-time STT — $0.12/hr = $0.002/min
  soniox: { unit: 'minute', price: 0.002 },
  // Speechmatics Pro tier — $0.24/hr = $0.0040/min (new users land here).
  // Previous $0.0173 default reflected a legacy Standard tier that was
  // retired; users were being over-billed ~4.3x.
  speechmatics: { unit: 'minute', price: 0.004 },
  // TTS — per 1,000 characters synthesized.
  // ElevenLabs default model is eleven_flash_v2_5 billed at $0.06/1k via the
  // direct API. The previous $0.18 matched only the Creator plan overage.
  elevenlabs: { unit: '1k_chars', price: 0.06 },
  openai_tts: { unit: '1k_chars', price: 0.015 },
  openai_tts_hd: { unit: '1k_chars', price: 0.030 },
  // Cartesia Sonic TTS — ~1 credit/char, effective $0.030/1k chars on usage plans
  cartesia_tts: { unit: '1k_chars', price: 0.030 },
  // Rime mist v2 — $0.030/1k chars pay-as-you-go
  rime: { unit: '1k_chars', price: 0.030 },
  // LMNT aurora/blizzard — $0.050/1k chars Indie overage
  lmnt: { unit: '1k_chars', price: 0.050 },
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

  const input = (usage.input_token_details ?? {}) as {
    audio_tokens?: number;
    text_tokens?: number;
    cached_tokens?: number;
    cached_tokens_details?: { audio_tokens?: number; text_tokens?: number };
  };
  const output = usage.output_token_details ?? {};

  const cachedAudioRate = config.cached_audio_input_per_token ?? config.audio_input_per_token ?? 0;
  const cachedTextRate = config.cached_text_input_per_token ?? config.text_input_per_token ?? 0;

  const totalAudioIn = input.audio_tokens ?? 0;
  const totalTextIn = input.text_tokens ?? 0;

  // cached_tokens_details is the preferred breakdown. When absent (older
  // Azure OpenAI responses) fall back to the top-level cached_tokens scalar
  // and pro-rate by the audio/text split so the discount still applies.
  let cachedAudioIn: number;
  let cachedTextIn: number;
  const details = input.cached_tokens_details;
  if (details && (details.audio_tokens !== undefined || details.text_tokens !== undefined)) {
    cachedAudioIn = Math.min(details.audio_tokens ?? 0, totalAudioIn);
    cachedTextIn = Math.min(details.text_tokens ?? 0, totalTextIn);
  } else if (input.cached_tokens && input.cached_tokens > 0) {
    const totalIn = totalAudioIn + totalTextIn;
    const ratio = totalIn > 0 ? input.cached_tokens / totalIn : 0;
    cachedAudioIn = Math.min(Math.round(totalAudioIn * ratio), totalAudioIn);
    cachedTextIn = Math.min(Math.round(totalTextIn * ratio), totalTextIn);
  } else {
    cachedAudioIn = 0;
    cachedTextIn = 0;
  }

  let cost = 0;
  cost += (totalAudioIn - cachedAudioIn) * (config.audio_input_per_token ?? 0);
  cost += cachedAudioIn * cachedAudioRate;
  cost += (totalTextIn - cachedTextIn) * (config.text_input_per_token ?? 0);
  cost += cachedTextIn * cachedTextRate;
  cost += (output.audio_tokens ?? 0) * (config.audio_output_per_token ?? 0);
  cost += (output.text_tokens ?? 0) * (config.text_output_per_token ?? 0);
  // Clamp ≥0 so mis-configured cached rates (higher than full) can never
  // produce negative billing on the dashboard.
  return Math.max(0, cost);
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
  // Clamp ≥0. If a user overrides cached_*_input_per_token to a rate
  // HIGHER than full, the diff becomes negative — meaningless as a savings
  // figure, so we render 0 instead of a negative number.
  return Math.max(0, (fullAudio + fullText) - (discountedAudio + discountedText));
}

// ---------------------------------------------------------------------------
// Chat/completion LLM pricing (per 1M tokens)
// ---------------------------------------------------------------------------
//
// Rates reflect publicly listed provider pricing as of PRICING_LAST_UPDATED.
// ``input`` / ``output`` are dollars per 1M tokens. Anthropic adds
// ``cache_read`` (~10% of full input) and ``cache_write`` (~125% of full input)
// for prompt caching. Groq / Cerebras / Google do not publicly expose cache
// rates for these models, so only input/output are populated.

export interface LlmModelPricing {
  input: number;
  output: number;
  cache_read?: number;
  cache_write?: number;
}

export const llmPricing: Record<string, Record<string, LlmModelPricing>> = {
  anthropic: {
    'claude-opus-4-7': {
      input: 15.0,
      output: 75.0,
      cache_read: 1.5,
      cache_write: 18.75,
    },
    'claude-sonnet-4-6': {
      input: 3.0,
      output: 15.0,
      cache_read: 0.3,
      cache_write: 3.75,
    },
    'claude-haiku-4-5': {
      input: 1.0,
      output: 5.0,
      cache_read: 0.1,
      cache_write: 1.25,
    },
  },
  google: {
    'gemini-2.5-pro': { input: 1.25, output: 10.0 },
    'gemini-2.5-flash': { input: 0.30, output: 2.50 },
    'gemini-live-2.5-flash-native-audio': { input: 0.30, output: 2.50 },
  },
  groq: {
    'llama-3.3-70b-versatile': { input: 0.59, output: 0.79 },
    'llama-3.1-8b-instant': { input: 0.05, output: 0.08 },
  },
  cerebras: {
    'llama-3.3-70b': { input: 0.85, output: 1.20 },
    'qwen-3-32b': { input: 0.40, output: 0.80 },
  },
  // OpenAI Chat Completions (non-Realtime) — mirrors sdk-py pricing table.
  // Rates are per 1M tokens (USD), cache_read = cached input rate.
  openai: {
    'gpt-4o': { input: 2.50, output: 10.00, cache_read: 1.25 },
    'gpt-4o-mini': { input: 0.15, output: 0.60, cache_read: 0.075 },
    'gpt-4.1': { input: 3.00, output: 12.00, cache_read: 0.75 },
    'gpt-4.1-mini': { input: 0.80, output: 3.20, cache_read: 0.20 },
    'o3': { input: 2.00, output: 8.00, cache_read: 0.50 },
    'o4-mini': { input: 1.10, output: 4.40, cache_read: 0.275 },
  },
};

/**
 * Calculate LLM cost from token counts using :data:`llmPricing`.
 *
 * Callers should subtract ``cacheReadTokens`` from ``inputTokens`` before
 * passing when they also pass ``cacheReadTokens`` separately so cached
 * tokens aren't double-billed. Returns 0 when the provider/model is not
 * listed so unknown models never produce bogus line items.
 */
export function calculateLlmCost(
  provider: string,
  model: string,
  inputTokens: number,
  outputTokens: number,
  cacheReadTokens: number = 0,
  cacheWriteTokens: number = 0,
): number {
  const providerTable = llmPricing[provider];
  if (!providerTable) return 0;
  // Exact match first; fall back to longest-prefix match so versioned model
  // ids like ``claude-haiku-4-5-20251001`` resolve against the canonical
  // alias ``claude-haiku-4-5`` in the pricing table.
  let rates = providerTable[model];
  if (!rates) {
    let bestKey = '';
    for (const key of Object.keys(providerTable)) {
      if (model.startsWith(key) && key.length > bestKey.length) {
        bestKey = key;
      }
    }
    if (bestKey) rates = providerTable[bestKey];
  }
  if (!rates) return 0;

  let cost = 0;
  cost += (inputTokens / 1_000_000) * (rates.input ?? 0);
  cost += (outputTokens / 1_000_000) * (rates.output ?? 0);
  cost += (cacheReadTokens / 1_000_000) * (rates.cache_read ?? 0);
  cost += (cacheWriteTokens / 1_000_000) * (rates.cache_write ?? 0);
  return Math.max(0, cost);
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

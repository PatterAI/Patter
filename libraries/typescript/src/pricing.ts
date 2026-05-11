/**
 * Default provider pricing and merge utilities.
 *
 * Pricing reflects public provider rates as of 2026. Each provider entry
 * carries provider-level defaults (the model Patter ships with by default)
 * plus an optional ``models`` map keyed by model identifier with per-model
 * overrides. Cost-calc functions take an optional ``model`` arg and
 * auto-resolve the rate via {@link resolveProviderRates} (longest-prefix
 * match for versioned model IDs). When the agent's adapter exposes
 * ``model`` and the metrics layer threads it through, the dashboard bills
 * with model accuracy out of the box — no manual override needed.
 *
 * User overrides via ``new Patter({ pricing: {...} })`` keep working as
 * before. To register a new model rate without touching the SDK source:
 *
 *     new Patter({ pricing: { elevenlabs: { models: { my_custom: { price: 0.075 } } } } })
 */

/** Pricing table version identifier, updated in lockstep with the Python SDK. */
export const PRICING_VERSION = '2026.3';
/** ISO date the pricing table was last refreshed against public provider rates. */
export const PRICING_LAST_UPDATED = '2026-05-08';

/**
 * Billing units used by ``DEFAULT_PRICING`` entries. String values keep the
 * pricing table JSON-serialisable and backwards-compatible with consumers
 * that still compare against the raw strings.
 */
export const PricingUnit = {
  MINUTE: 'minute',
  THOUSAND_CHARS: '1k_chars',
  TOKEN: 'token',
} as const;
/** String value for one of the entries in `PricingUnit`. */
export type PricingUnitValue = (typeof PricingUnit)[keyof typeof PricingUnit];

/** Per-model rate overrides — same shape as `ProviderPricing` minus the unit. */
export type ModelPricing = Omit<ProviderPricing, 'unit' | 'models'> & {
  unit?: PricingUnitValue | string;
};

/** Single provider's pricing entry inside `DEFAULT_PRICING` or a user override map. */
export interface ProviderPricing {
  /**
   * Billing unit. The library ships with values from :data:`PricingUnit`,
   * but the field stays ``string`` so user overrides loaded from JSON /
   * env config (which are unconstrained at the type system) keep flowing
   * through ``mergePricing`` without type assertions.
   */
  unit: PricingUnitValue | string;
  price?: number;
  audio_input_per_token?: number;
  audio_output_per_token?: number;
  text_input_per_token?: number;
  text_output_per_token?: number;
  cached_audio_input_per_token?: number;
  cached_text_input_per_token?: number;
  /**
   * Per-model rate overrides keyed by model identifier. When the cost-calc
   * function receives a ``model`` arg, the matching entry overlays the
   * provider defaults; missing models fall back to the surrounding rates
   * (legacy behaviour). Longest-prefix match handles versioned IDs like
   * ``gpt-realtime-2-2026-05`` against ``gpt-realtime-2``. See
   * :func:`resolveProviderRates`.
   */
  models?: Record<string, ModelPricing>;
}

/**
 * Merge model-specific overrides on top of provider-level defaults. Returns a
 * fresh object — callers can index it like the legacy flat config without
 * worrying about mutating ``DEFAULT_PRICING``. Falls back to provider defaults
 * when the model is unknown or omitted.
 */
export function resolveProviderRates(
  providerConfig: ProviderPricing | undefined,
  model?: string | null,
): ProviderPricing {
  if (!providerConfig) return { unit: '' };
  const { models, ...base } = providerConfig;
  if (!model || !models) return { ...base } as ProviderPricing;
  let override = models[model];
  if (!override) {
    let bestKey = '';
    for (const key of Object.keys(models)) {
      if (model.startsWith(key) && key.length > bestKey.length) {
        bestKey = key;
      }
    }
    if (bestKey) override = models[bestKey];
  }
  if (override) {
    return { ...base, ...override } as ProviderPricing;
  }
  return { ...base } as ProviderPricing;
}

/**
 * Built-in pricing table — overridable via `Patter({ pricing: {...} })`.
 *
 * Each provider entry carries provider-level defaults plus an optional
 * `models` map for per-model overrides. When the cost-calc function gets a
 * model arg it auto-resolves via {@link resolveProviderRates} (longest-prefix
 * fallback for versioned model IDs). Empty/unknown model → provider defaults.
 */
export const DEFAULT_PRICING: Record<string, ProviderPricing> = {
  // STT — per minute of audio processed.
  deepgram: {
    unit: PricingUnit.MINUTE,
    // Default = Nova-3 streaming monolingual ($0.0048/min, current Pay-
    // As-You-Go promotional rate). Source: https://deepgram.com/pricing
    // (verified 2026-05-11). The promo replaces the standard $0.0077/min
    // quoted at Nova-3 launch and is the rate customers actually pay
    // today; revisit when Deepgram removes the "Limited-time promotional
    // rates on streaming" banner.
    price: 0.0048,
    models: {
      // Nova-3 family — current flagship.
      'nova-3': { price: 0.0048 },
      'nova-3-multilingual': { price: 0.0058 },
      // Flux family — new event-driven turn-taking STT (2026 launch).
      flux: { price: 0.0065 },
      'flux-english': { price: 0.0065 },
      'flux-multilingual': { price: 0.0078 },
      // Legacy Nova-2 / Nova-1 — still supported but no longer featured on
      // the public pricing page; rates kept as last verified.
      'nova-2': { price: 0.0058 },
      nova: { price: 0.0043 },
      // Whisper Cloud via Deepgram — separate tier.
      'whisper-large': { price: 0.0048 },
      'whisper-medium': { price: 0.0048 },
    },
  },
  whisper: {
    unit: PricingUnit.MINUTE,
    // Default = whisper-1 REST ($0.006/min).
    price: 0.006,
    models: {
      'whisper-1': { price: 0.006 },
      'gpt-4o-transcribe': { price: 0.006 },
      'gpt-4o-mini-transcribe': { price: 0.003 },
      // Streaming Whisper variant for Realtime sessions.
      'gpt-realtime-whisper': { price: 0.017 },
    },
  },
  // OpenAI standalone transcription endpoint (separate provider_key from
  // ``whisper`` so the dashboard can distinguish them).
  openai_transcribe: {
    unit: PricingUnit.MINUTE,
    price: 0.006,
    models: {
      'gpt-4o-transcribe': { price: 0.006 },
      'gpt-4o-mini-transcribe': { price: 0.003 },
      'whisper-1': { price: 0.006 },
    },
  },
  // AssemblyAI Universal-Streaming — $0.15/hr = $0.0025/min
  assemblyai: { unit: PricingUnit.MINUTE, price: 0.0025 },
  // Cartesia ink-whisper streaming STT — ~$0.15/hr on usage plans
  cartesia_stt: { unit: PricingUnit.MINUTE, price: 0.0025 },
  // Soniox real-time STT — $0.12/hr = $0.002/min
  soniox: { unit: PricingUnit.MINUTE, price: 0.002 },
  // Speechmatics Pro tier — $0.24/hr = $0.0040/min (new users land here).
  // Previous $0.0173 default reflected a legacy Standard tier that was
  // retired; users were being over-billed ~4.3x.
  speechmatics: { unit: PricingUnit.MINUTE, price: 0.004 },
  // TTS — per 1,000 characters synthesized.
  // Source: https://elevenlabs.io/pricing/api (verified 2026-05-11). The
  // per-1K-character API/overage rate is flat across all plan tiers (Free
  // through Business); only the included character bundle varies by plan.
  elevenlabs: {
    unit: PricingUnit.THOUSAND_CHARS,
    // Default = eleven_flash_v2_5 (Patter's default model) at $0.05/1k.
    price: 0.05,
    models: {
      eleven_flash_v2_5: { price: 0.05 },
      eleven_turbo_v2_5: { price: 0.05 },
      eleven_multilingual_v2: { price: 0.10 },
      eleven_monolingual_v1: { price: 0.10 },
      eleven_v3: { price: 0.10 },
    },
  },
  // ElevenLabs WebSocket streaming TTS shares pricing with REST.
  elevenlabs_ws: {
    unit: PricingUnit.THOUSAND_CHARS,
    price: 0.05,
    models: {
      eleven_flash_v2_5: { price: 0.05 },
      eleven_turbo_v2_5: { price: 0.05 },
      eleven_multilingual_v2: { price: 0.10 },
      eleven_v3: { price: 0.10 },
    },
  },
  openai_tts: {
    unit: PricingUnit.THOUSAND_CHARS,
    // Default = tts-1 ($0.015/1k chars).
    price: 0.015,
    models: {
      'tts-1': { price: 0.015 },
      'tts-1-hd': { price: 0.030 },
      // gpt-4o-mini-tts is billed by tokens upstream but published per
      // 1k chars equivalent here for parity with the rest of the table.
      'gpt-4o-mini-tts': { price: 0.012 },
    },
  },
  // Legacy alias preserved for backward compat with users who set
  // provider_key="openai_tts_hd" in their own adapters.
  openai_tts_hd: { unit: PricingUnit.THOUSAND_CHARS, price: 0.030 },
  cartesia_tts: {
    unit: PricingUnit.THOUSAND_CHARS,
    // Default = Sonic-2 (current Cartesia flagship) at ~$0.030/1k chars.
    price: 0.030,
    models: {
      'sonic-2': { price: 0.030 },
      'sonic-1': { price: 0.030 },
      'sonic-english': { price: 0.030 },
      'sonic-multilingual': { price: 0.030 },
    },
  },
  rime: {
    unit: PricingUnit.THOUSAND_CHARS,
    // Default = mistv2 ($0.030/1k chars).
    price: 0.030,
    models: {
      mistv2: { price: 0.030 },
      mist: { price: 0.030 },
      arcana: { price: 0.040 },
    },
  },
  lmnt: {
    unit: PricingUnit.THOUSAND_CHARS,
    // Default = aurora ($0.050/1k chars).
    price: 0.050,
    models: {
      aurora: { price: 0.050 },
      blizzard: { price: 0.050 },
    },
  },
  inworld: {
    unit: PricingUnit.THOUSAND_CHARS,
    // Default = inworld-tts-2 (placeholder rate — verify against tier).
    price: 0.020,
    models: {
      'inworld-tts-2': { price: 0.020 },
      'inworld-tts-1.5-max': { price: 0.025 },
      'inworld-tts-1.5': { price: 0.025 },
    },
  },
  // OpenAI Realtime — per token. Provider defaults match
  // gpt-realtime-mini / gpt-4o-mini-realtime-preview (Patter's default).
  // Per-model overrides under ``models`` are auto-resolved when the
  // realtime adapter's model is threaded through ``calculateRealtimeCost``.
  openai_realtime: {
    unit: PricingUnit.TOKEN,
    // Default rates: gpt-realtime-mini / gpt-4o-mini-realtime-preview
    audio_input_per_token: 0.00001,
    audio_output_per_token: 0.00002,
    text_input_per_token: 0.0000006,
    text_output_per_token: 0.0000024,
    // Prompt caching rates (official): audio cached $0.30/M ~= 3% of full,
    // text cached $0.06/M = 10% of full. OpenAI bills the cached portion of
    // input_token_details.audio_tokens / text_tokens at these reduced rates.
    cached_audio_input_per_token: 0.0000003,
    cached_text_input_per_token: 0.00000006,
    models: {
      // gpt-realtime (GA, August 2025): audio in $32/M, audio out $64/M,
      // text in $4/M, text out $16/M, cached $0.40/M (audio + text).
      'gpt-realtime': {
        audio_input_per_token: 0.000032,
        audio_output_per_token: 0.000064,
        text_input_per_token: 0.000004,
        text_output_per_token: 0.000016,
        cached_audio_input_per_token: 0.0000004,
        cached_text_input_per_token: 0.0000004,
      },
      // gpt-realtime-2 (most-capable): audio in $32/M, audio out $64/M,
      // text in $4/M, text out $24/M, cached $0.40/M (audio + text).
      'gpt-realtime-2': {
        audio_input_per_token: 0.000032,
        audio_output_per_token: 0.000064,
        text_input_per_token: 0.000004,
        text_output_per_token: 0.000024,
        cached_audio_input_per_token: 0.0000004,
        cached_text_input_per_token: 0.0000004,
      },
      // gpt-realtime-mini and gpt-4o-mini-realtime-preview share the
      // provider defaults. Listed explicitly so tooling can introspect.
      'gpt-realtime-mini': {
        audio_input_per_token: 0.00001,
        audio_output_per_token: 0.00002,
        text_input_per_token: 0.0000006,
        text_output_per_token: 0.0000024,
        cached_audio_input_per_token: 0.0000003,
        cached_text_input_per_token: 0.00000006,
      },
      'gpt-4o-mini-realtime-preview': {
        audio_input_per_token: 0.00001,
        audio_output_per_token: 0.00002,
        text_input_per_token: 0.0000006,
        text_output_per_token: 0.0000024,
        cached_audio_input_per_token: 0.0000003,
        cached_text_input_per_token: 0.00000006,
      },
      // gpt-4o-realtime-preview (legacy preview, ~10x mini for audio):
      // audio in $100/M, audio out $200/M, text in $5/M, text out $20/M.
      'gpt-4o-realtime-preview': {
        audio_input_per_token: 0.0001,
        audio_output_per_token: 0.0002,
        text_input_per_token: 0.000005,
        text_output_per_token: 0.000020,
        cached_audio_input_per_token: 0.0000020,
        cached_text_input_per_token: 0.0000025,
      },
    },
  },
  // Telephony — per minute of call duration.
  // twilio default = US inbound local (the 99% case for voice agents receiving
  // calls on a local number). For US toll-free inbound ($0.022/min) or US
  // outbound local ($0.0140/min), override via Patter({ pricing: { twilio: {...} } }).
  twilio: { unit: PricingUnit.MINUTE, price: 0.0085 },
  // Telnyx — direction-aware rates as of 2026-05-11.
  // Sources:
  //   https://telnyx.com/pricing/elastic-sip
  //   https://telnyx.com/pricing/voice-api
  // US inbound (DID / local termination, Pay-As-You-Go): $0.0035/min
  // US outbound (Pay-As-You-Go, mid-range of $0.005-$0.009): $0.007/min
  // Billing granularity is per-MINUTE (Telnyx rounds partial minutes up
  // on the invoice; prior internal docs incorrectly claimed per-second).
  // The legacy ``telnyx`` key is preserved at the outbound rate as a
  // safe fallback for users who override ``pricing: { telnyx: {...} }``
  // without knowing the direction; the metrics layer currently uses
  // this flat key (direction is not threaded through to
  // ``calculateTelephonyCost``). Direction-aware billing can be enabled
  // by override-only: ``new Patter({ pricing: { telnyx: { unit: 'minute',
  // price: 0.0035 } } })`` to bill all inbound at the lower rate.
  telnyx: { unit: PricingUnit.MINUTE, price: 0.007 },
  telnyx_inbound: { unit: PricingUnit.MINUTE, price: 0.0035 },
  telnyx_outbound: { unit: PricingUnit.MINUTE, price: 0.007 },
};

function cloneProviderEntry(entry: ProviderPricing): ProviderPricing {
  const out: ProviderPricing = { ...entry };
  if (entry.models) {
    const models: Record<string, ModelPricing> = {};
    for (const [mk, mv] of Object.entries(entry.models)) {
      models[mk] = { ...mv };
    }
    out.models = models;
  }
  return out;
}

/**
 * Merge user overrides into a copy of DEFAULT_PRICING.
 *
 * Performs a per-provider shallow merge with one exception: the nested
 * ``models`` dict is itself merged shallowly (per-model entries replace
 * the default entry but unmentioned models keep their built-in rates).
 * A user override of ``{ deepgram: { models: { 'nova-2': { price: 0.01 } } } }``
 * keeps every other Deepgram model rate intact.
 */
export function mergePricing(
  overrides?: Record<string, Partial<ProviderPricing>> | null,
): Record<string, ProviderPricing> {
  const merged: Record<string, ProviderPricing> = {};
  for (const [k, v] of Object.entries(DEFAULT_PRICING)) {
    merged[k] = cloneProviderEntry(v);
  }
  if (!overrides) return merged;
  for (const [provider, values] of Object.entries(overrides)) {
    if (!merged[provider]) {
      // Fail-closed: when the user registers a brand-new provider without a
      // ``unit`` field, leave it missing so ``calculate_*_cost`` returns 0
      // instead of silently billing as minutes. Matches the Python SDK behaviour.
      merged[provider] = cloneProviderEntry(values as ProviderPricing);
      continue;
    }
    const target = merged[provider];
    for (const [k, v] of Object.entries(values)) {
      if (
        k === 'models' &&
        v &&
        typeof v === 'object' &&
        target.models &&
        typeof target.models === 'object'
      ) {
        // Per-model overlay — keep models the user did NOT mention.
        const mergedModels: Record<string, ModelPricing> = { ...target.models };
        for (const [mk, mv] of Object.entries(v as Record<string, ModelPricing>)) {
          mergedModels[mk] = { ...mv };
        }
        target.models = mergedModels;
      } else {
        // Direct copy for top-level keys (price, unit, per-token rates, ...).
        (target as unknown as Record<string, unknown>)[k] = v as unknown;
      }
    }
  }
  return merged;
}

/**
 * Calculate STT cost from audio duration.
 *
 * When ``model`` is supplied and the provider entry has a matching
 * ``models`` override, the per-model rate is used; otherwise falls back
 * to the provider-level rate (legacy behaviour, model omitted).
 */
export function calculateSttCost(
  provider: string,
  audioSeconds: number,
  pricing: Record<string, ProviderPricing>,
  model?: string | null,
): number {
  const rates = resolveProviderRates(pricing[provider], model);
  if (rates.unit !== 'minute') return 0;
  return (audioSeconds / 60) * (rates.price ?? 0);
}

/**
 * Calculate TTS cost from character count.
 *
 * When ``model`` is supplied and the provider entry has a matching
 * ``models`` override, the per-model rate is used; otherwise falls back
 * to the provider-level rate (legacy behaviour, model omitted).
 */
export function calculateTtsCost(
  provider: string,
  characterCount: number,
  pricing: Record<string, ProviderPricing>,
  model?: string | null,
): number {
  const rates = resolveProviderRates(pricing[provider], model);
  if (rates.unit !== '1k_chars') return 0;
  return (characterCount / 1000) * (rates.price ?? 0);
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
  model?: string | null,
): number {
  const rates = resolveProviderRates(pricing.openai_realtime, model);
  if (rates.unit !== 'token') return 0;

  const input = (usage.input_token_details ?? {}) as {
    audio_tokens?: number;
    text_tokens?: number;
    cached_tokens?: number;
    cached_tokens_details?: { audio_tokens?: number; text_tokens?: number };
  };
  const output = usage.output_token_details ?? {};

  const cachedAudioRate = rates.cached_audio_input_per_token ?? rates.audio_input_per_token ?? 0;
  const cachedTextRate = rates.cached_text_input_per_token ?? rates.text_input_per_token ?? 0;

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
  cost += (totalAudioIn - cachedAudioIn) * (rates.audio_input_per_token ?? 0);
  cost += cachedAudioIn * cachedAudioRate;
  cost += (totalTextIn - cachedTextIn) * (rates.text_input_per_token ?? 0);
  cost += cachedTextIn * cachedTextRate;
  cost += (output.audio_tokens ?? 0) * (rates.audio_output_per_token ?? 0);
  cost += (output.text_tokens ?? 0) * (rates.text_output_per_token ?? 0);
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
  model?: string | null,
): number {
  const rates = resolveProviderRates(pricing.openai_realtime, model);
  if (rates.unit !== 'token') return 0;
  const input = usage.input_token_details ?? {};
  const cached = input.cached_tokens_details ?? {};
  const cachedAudioRate = rates.cached_audio_input_per_token ?? rates.audio_input_per_token ?? 0;
  const cachedTextRate = rates.cached_text_input_per_token ?? rates.text_input_per_token ?? 0;
  const cachedAudio = Math.min(cached.audio_tokens ?? 0, input.audio_tokens ?? 0);
  const cachedText = Math.min(cached.text_tokens ?? 0, input.text_tokens ?? 0);
  const fullAudio = cachedAudio * (rates.audio_input_per_token ?? 0);
  const fullText = cachedText * (rates.text_input_per_token ?? 0);
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

/** Per-1M-token rates for a single LLM model (input, output, optional cache reads/writes). */
export interface LlmModelPricing {
  input: number;
  output: number;
  cache_read?: number;
  cache_write?: number;
}

/** Per-provider, per-model rate card for chat/completion LLM cost calculation. */
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
    // Rates as of 2026-05-08; verify against groq.com/pricing.
    // ``llama-3.3-70b-versatile`` is the Patter default for Groq. The
    // remaining models are reachable via ``model: "..."`` and were silently
    // billing $0 before this entry was added (silent under-billing).
    'llama-3.3-70b-versatile': { input: 0.59, output: 0.79 },
    'llama-3.1-8b-instant': { input: 0.05, output: 0.08 },
    'llama-3.3-70b-specdec': { input: 0.59, output: 0.99 },
    'llama3-70b-8192': { input: 0.59, output: 0.79 },
    'llama3-8b-8192': { input: 0.05, output: 0.08 },
    'mixtral-8x7b-32768': { input: 0.27, output: 0.27 },
    'gemma2-9b-it': { input: 0.20, output: 0.20 },
  },
  cerebras: {
    // Rates as of 2026-05-11 verified against the canonical per-model docs
    // pages at ``https://inference-docs.cerebras.ai/models/<model>``. The
    // previous 2026-05-08 update overcharged across the board (gpt-oss-120b
    // 2.4x input, qwen-3-235b 1.67x input) because it conflated the launch
    // blog quotes with the "Exploration pricing" banner now shown on each
    // model page. Parity with libraries/python/getpatter/pricing.py.
    'gpt-oss-120b': { input: 0.35, output: 0.75 },
    'llama3.1-8b': { input: 0.10, output: 0.10 },
    'llama-3.3-70b': { input: 0.85, output: 1.20 },
    'qwen-3-32b': { input: 0.40, output: 0.80 },
    'qwen-3-235b-a22b-instruct-2507': { input: 0.60, output: 1.20 },
    'qwen-3-coder-480b': { input: 2.00, output: 2.00 },
    'zai-glm-4.7': { input: 0.85, output: 1.20 },
  },
  // OpenAI Chat Completions (non-Realtime) — mirrors the Python SDK pricing table.
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

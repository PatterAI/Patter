import { describe, it, expect } from 'vitest';
import {
  DEFAULT_PRICING,
  mergePricing,
  calculateSttCost,
  calculateTtsCost,
  calculateRealtimeCost,
  calculateRealtimeCachedSavings,
  calculateTelephonyCost,
  calculateLlmCost,
  llmPricing,
} from '../src/pricing';

describe('DEFAULT_PRICING', () => {
  it('includes all expected providers', () => {
    expect(DEFAULT_PRICING.deepgram).toBeDefined();
    expect(DEFAULT_PRICING.whisper).toBeDefined();
    expect(DEFAULT_PRICING.elevenlabs).toBeDefined();
    expect(DEFAULT_PRICING.openai_tts).toBeDefined();
    expect(DEFAULT_PRICING.openai_realtime).toBeDefined();
    expect(DEFAULT_PRICING.twilio).toBeDefined();
    expect(DEFAULT_PRICING.telnyx).toBeDefined();
  });
});

describe('mergePricing', () => {
  it('returns defaults when no overrides', () => {
    const merged = mergePricing();
    // Deepgram Nova-3 streaming (monolingual) — $0.0048/min (current PAYG
    // promo rate per deepgram.com/pricing, verified 2026-05-11).
    expect(merged.deepgram.price).toBe(0.0048);
  });

  it('overrides individual provider values', () => {
    const merged = mergePricing({ deepgram: { price: 0.01 } });
    expect(merged.deepgram.price).toBe(0.01);
    expect(merged.deepgram.unit).toBe('minute'); // preserved from default
  });

  it('adds new providers', () => {
    const merged = mergePricing({ custom: { unit: 'minute', price: 0.05 } });
    expect(merged.custom.price).toBe(0.05);
  });
});

describe('calculateSttCost', () => {
  it('calculates deepgram cost for 60 seconds', () => {
    const pricing = mergePricing();
    const cost = calculateSttCost('deepgram', 60, pricing);
    // 60s / 60 * $0.0048/min (Nova-3 streaming monolingual, current PAYG
    // promo rate per deepgram.com/pricing, verified 2026-05-11) = $0.0048
    expect(cost).toBeCloseTo(0.0048, 4);
  });

  it('returns 0 for unknown provider', () => {
    const pricing = mergePricing();
    expect(calculateSttCost('unknown', 60, pricing)).toBe(0);
  });
});

describe('calculateTtsCost', () => {
  it('calculates elevenlabs cost for 1000 characters', () => {
    const pricing = mergePricing();
    const cost = calculateTtsCost('elevenlabs', 1000, pricing);
    // eleven_flash_v2_5 (the default model): $0.05/1k chars per the public
    // API/overage rate at https://elevenlabs.io/pricing/api (verified
    // 2026-05-11). Flat across all plan tiers — only the included character
    // bundle changes per plan.
    expect(cost).toBeCloseTo(0.05, 3);
  });

  it('calculates openai_tts cost', () => {
    const pricing = mergePricing();
    const cost = calculateTtsCost('openai_tts', 500, pricing);
    expect(cost).toBeCloseTo(0.0075, 4);
  });
});

describe('calculateRealtimeCost', () => {
  it('calculates from token usage', () => {
    const pricing = mergePricing();
    const cost = calculateRealtimeCost(
      {
        input_token_details: { audio_tokens: 100, text_tokens: 50 },
        output_token_details: { audio_tokens: 200, text_tokens: 30 },
      },
      pricing,
    );
    // gpt-4o-mini-realtime-preview rates (2026):
    //   100*0.00001 + 50*0.0000006 + 200*0.00002 + 30*0.0000024
    //   = 0.001 + 0.00003 + 0.004 + 0.000072 = 0.005102
    expect(cost).toBeCloseTo(0.005102, 6);
  });

  it('returns 0 for empty usage', () => {
    const pricing = mergePricing();
    expect(calculateRealtimeCost({}, pricing)).toBe(0);
  });

  it('applies cached rate to cached portion of input tokens', () => {
    const pricing = mergePricing();
    // 1000 audio in (800 cached), 500 text in (400 cached), 0 out.
    const cost = calculateRealtimeCost(
      {
        input_token_details: {
          audio_tokens: 1000,
          text_tokens: 500,
          cached_tokens_details: { audio_tokens: 800, text_tokens: 400 },
        },
        output_token_details: { audio_tokens: 0, text_tokens: 0 },
      },
      pricing,
    );
    // (1000-800)*1e-5 + 800*3e-7 + (500-400)*6e-7 + 400*6e-8
    // = 0.002 + 0.00024 + 0.00006 + 0.000024 = 0.002324
    expect(cost).toBeCloseTo(0.002324, 8);
  });

  it('clamps cached tokens > total so cost stays non-negative', () => {
    const pricing = mergePricing();
    const cost = calculateRealtimeCost(
      {
        input_token_details: {
          audio_tokens: 100,
          cached_tokens_details: { audio_tokens: 500 }, // malformed: > total
        },
      },
      pricing,
    );
    // Clamped to 100 cached: 0 * 1e-5 + 100 * 3e-7 = 3e-5
    expect(cost).toBeCloseTo(100 * 0.0000003, 10);
    expect(cost).toBeGreaterThanOrEqual(0);
  });

  it('handles null input_token_details safely (no throw)', () => {
    const pricing = mergePricing();
    // OpenAI sometimes emits input_token_details = null on early errors
    const cost = calculateRealtimeCost(
      { input_token_details: undefined, output_token_details: { audio_tokens: 100 } },
      pricing,
    );
    expect(cost).toBeCloseTo(100 * 0.00002, 10);
  });
});

describe('calculateRealtimeCachedSavings', () => {
  it('returns positive savings on normal cached discount', () => {
    const pricing = mergePricing();
    const savings = calculateRealtimeCachedSavings(
      {
        input_token_details: {
          audio_tokens: 1000,
          text_tokens: 500,
          cached_tokens_details: { audio_tokens: 800, text_tokens: 400 },
        },
      },
      pricing,
    );
    // 800 * (1e-5 - 3e-7) + 400 * (6e-7 - 6e-8)
    const expected = 800 * (0.00001 - 0.0000003) + 400 * (0.0000006 - 0.00000006);
    expect(savings).toBeCloseTo(expected, 10);
    expect(savings).toBeGreaterThan(0);
  });

  it('clamps to zero when cached rate misconfigured higher than full', () => {
    // Parity with Python: if a user overrides cached rate above full, savings
    // go negative — must clamp to 0 rather than render negative on dashboard.
    const pricing = mergePricing({
      openai_realtime: {
        cached_audio_input_per_token: 0.0001, // 10x higher than full
        cached_text_input_per_token: 0.00001,
      },
    });
    const savings = calculateRealtimeCachedSavings(
      {
        input_token_details: {
          audio_tokens: 1000,
          text_tokens: 500,
          cached_tokens_details: { audio_tokens: 500, text_tokens: 250 },
        },
      },
      pricing,
    );
    expect(savings).toBe(0);
  });

  it('returns zero when no cached tokens', () => {
    const pricing = mergePricing();
    const savings = calculateRealtimeCachedSavings(
      { input_token_details: { audio_tokens: 1000, text_tokens: 500 } },
      pricing,
    );
    expect(savings).toBe(0);
  });
});

describe('calculateTelephonyCost', () => {
  it('calculates twilio cost for 120 seconds (US inbound local default)', () => {
    const pricing = mergePricing();
    const cost = calculateTelephonyCost('twilio', 120, pricing);
    // 120s / 60 * $0.0085/min = $0.017
    expect(cost).toBeCloseTo(0.017, 4);
  });

  it('calculates telnyx cost', () => {
    const pricing = mergePricing();
    const cost = calculateTelephonyCost('telnyx', 60, pricing);
    expect(cost).toBeCloseTo(0.007, 3);
  });

  it('telnyx_inbound bills at $0.0035/min (US DID local termination)', () => {
    // Verified against https://telnyx.com/pricing/elastic-sip (2026-05-11).
    const pricing = mergePricing();
    const cost = calculateTelephonyCost('telnyx_inbound', 60, pricing);
    expect(cost).toBeCloseTo(0.0035, 4);
  });

  it('telnyx_outbound bills at $0.007/min (Pay-As-You-Go mid-range)', () => {
    const pricing = mergePricing();
    const cost = calculateTelephonyCost('telnyx_outbound', 60, pricing);
    expect(cost).toBeCloseTo(0.007, 3);
  });

  it('legacy telnyx key falls back to outbound rate ($0.007/min)', () => {
    // Users who override ``pricing: { telnyx: {...} }`` without a
    // direction-aware split keep the previous behaviour. Direction-aware
    // billing is opt-in via ``telnyx_inbound`` / ``telnyx_outbound``.
    const pricing = mergePricing();
    const cost = calculateTelephonyCost('telnyx', 60, pricing);
    expect(cost).toBeCloseTo(0.007, 3);
  });
});

describe('per-model rates under openai_realtime.models', () => {
  it('exposes gpt-realtime, gpt-realtime-2, mini, and 4o-preview', () => {
    const models = DEFAULT_PRICING.openai_realtime.models!;
    expect(models['gpt-realtime']).toBeDefined();
    expect(models['gpt-realtime-2']).toBeDefined();
    expect(models['gpt-realtime-mini']).toBeDefined();
    expect(models['gpt-4o-realtime-preview']).toBeDefined();
  });

  it('gpt-realtime-2 matches OpenAI published per-1M-token rates', () => {
    const e = DEFAULT_PRICING.openai_realtime.models!['gpt-realtime-2'];
    expect(e.text_input_per_token).toBeCloseTo(4 / 1_000_000, 12);
    expect(e.text_output_per_token).toBeCloseTo(24 / 1_000_000, 12);
    expect(e.audio_input_per_token).toBeCloseTo(32 / 1_000_000, 12);
    expect(e.audio_output_per_token).toBeCloseTo(64 / 1_000_000, 12);
    expect(e.cached_audio_input_per_token).toBeCloseTo(0.4 / 1_000_000, 12);
    expect(e.cached_text_input_per_token).toBeCloseTo(0.4 / 1_000_000, 12);
  });

  it('calculateRealtimeCost auto-resolves the per-model rate when model is passed', () => {
    const pricing = mergePricing();
    // 1000 audio input tokens at $32/M = $0.032 (gpt-realtime-2)
    const cost = calculateRealtimeCost(
      {
        input_token_details: { audio_tokens: 1000, text_tokens: 0 },
        output_token_details: { audio_tokens: 0, text_tokens: 0 },
      },
      pricing,
      'gpt-realtime-2',
    );
    expect(cost).toBeCloseTo(0.032, 6);
  });

  it('falls back to provider defaults (mini) when model is unknown or omitted', () => {
    const pricing = mergePricing();
    const usage = {
      input_token_details: { audio_tokens: 1000, text_tokens: 0 },
      output_token_details: { audio_tokens: 0, text_tokens: 0 },
    };
    // 1000 * $10/M = $0.01 (mini default rate)
    const costNone = calculateRealtimeCost(usage, pricing);
    const costUnknown = calculateRealtimeCost(usage, pricing, 'some-future-model');
    expect(costNone).toBeCloseTo(0.01, 6);
    expect(costUnknown).toBeCloseTo(0.01, 6);
  });
});

describe('model-aware STT pricing', () => {
  it('deepgram default is nova-3 streaming', () => {
    const pricing = mergePricing();
    // 60s at $0.0048/min = $0.0048 (PAYG promo rate, verified 2026-05-11).
    expect(calculateSttCost('deepgram', 60, pricing)).toBeCloseTo(0.0048, 6);
  });

  it('deepgram multilingual nested rate', () => {
    const pricing = mergePricing();
    // nova-3-multilingual is $0.0058/min on the PAYG promo tier.
    expect(calculateSttCost('deepgram', 60, pricing, 'nova-3-multilingual')).toBeCloseTo(
      0.0058,
      6,
    );
  });

  it('whisper provider exposes per-model rates including gpt-realtime-whisper', () => {
    const pricing = mergePricing();
    expect(calculateSttCost('whisper', 60, pricing, 'gpt-4o-mini-transcribe')).toBeCloseTo(
      0.003,
      6,
    );
    expect(calculateSttCost('whisper', 60, pricing, 'gpt-realtime-whisper')).toBeCloseTo(
      0.017,
      6,
    );
  });

  it('unknown model falls back to provider default', () => {
    const pricing = mergePricing();
    expect(calculateSttCost('deepgram', 60, pricing, 'some-future-model')).toBeCloseTo(
      0.0048,
      6,
    );
  });
});

describe('model-aware TTS pricing', () => {
  it('elevenlabs default is flash_v2_5', () => {
    const pricing = mergePricing();
    // Default == flash_v2_5 at $0.05/1k (public API rate verified 2026-05-11).
    expect(calculateTtsCost('elevenlabs', 1000, pricing)).toBeCloseTo(0.05, 6);
  });

  it('elevenlabs multilingual_v2 nested rate', () => {
    const pricing = mergePricing();
    // Multilingual v2 / v3 share the $0.10/1k tier per the public API page.
    expect(calculateTtsCost('elevenlabs', 1000, pricing, 'eleven_multilingual_v2')).toBeCloseTo(
      0.10,
      6,
    );
  });

  it('openai_tts splits tts-1 vs tts-1-hd via models map', () => {
    const pricing = mergePricing();
    expect(calculateTtsCost('openai_tts', 1000, pricing, 'tts-1')).toBeCloseTo(0.015, 6);
    expect(calculateTtsCost('openai_tts', 1000, pricing, 'tts-1-hd')).toBeCloseTo(0.030, 6);
  });
});

describe('per-model override merge semantics', () => {
  it('overriding one model leaves the others intact', () => {
    const pricing = mergePricing({
      elevenlabs: { models: { eleven_flash_v2_5: { price: 0.04 } } },
    });
    // Overridden
    expect(calculateTtsCost('elevenlabs', 1000, pricing, 'eleven_flash_v2_5')).toBeCloseTo(
      0.04,
      6,
    );
    // Untouched — still original $0.10
    expect(calculateTtsCost('elevenlabs', 1000, pricing, 'eleven_multilingual_v2')).toBeCloseTo(
      0.10,
      6,
    );
  });

  it('user can register a brand-new model under a known provider', () => {
    const pricing = mergePricing({
      deepgram: { models: { 'my-private-model': { price: 0.012 } } },
    });
    expect(calculateSttCost('deepgram', 60, pricing, 'my-private-model')).toBeCloseTo(
      0.012,
      6,
    );
  });

  it('versioned model IDs resolve via longest-prefix fallback', () => {
    const pricing = mergePricing();
    const cost = calculateRealtimeCost(
      {
        input_token_details: { audio_tokens: 1000, text_tokens: 0 },
        output_token_details: { audio_tokens: 0, text_tokens: 0 },
      },
      pricing,
      'gpt-realtime-2-2026-05-08',
    );
    // Should resolve to gpt-realtime-2 rate
    expect(cost).toBeCloseTo(0.032, 6);
  });
});

describe('LLM cost billing — Cerebras + Groq silent under-billing regression', () => {
  // Before these entries were added, every Patter user running the Cerebras
  // default model (``gpt-oss-120b``) or any Groq model outside the two
  // versatile/instant ones billed exactly $0 for LLM tokens. ``calculateLlmCost``
  // falls through to ``return 0`` when the model is missing from the rate
  // table, so the dashboard charged nothing without surfacing a warning.

  it('cerebras default model (gpt-oss-120b) is billed', () => {
    const cost = calculateLlmCost('cerebras', 'gpt-oss-120b', 1000, 1000);
    // Real rate-card math: 1000 in @ $0.35/M + 1000 out @ $0.75/M
    // (verified at inference-docs.cerebras.ai/models/openai-oss).
    expect(cost).toBeCloseTo((1000 / 1_000_000) * 0.35 + (1000 / 1_000_000) * 0.75, 9);
    expect(cost).toBeGreaterThan(0);
  });

  it('cerebras llama3.1-8b is billed (still supported until 2026-05-27 retirement)', () => {
    const cost = calculateLlmCost('cerebras', 'llama3.1-8b', 1000, 1000);
    // 1000 in @ $0.10/M + 1000 out @ $0.10/M
    // (verified at inference-docs.cerebras.ai/models/llama-31-8b).
    expect(cost).toBeCloseTo((1000 / 1_000_000) * 0.10 + (1000 / 1_000_000) * 0.10, 9);
    expect(cost).toBeGreaterThan(0);
  });

  it('every CerebrasModel enum value has a pricing entry', () => {
    // Mirrors libraries/typescript/src/providers/cerebras-llm.ts CerebrasModel.
    const cerebrasModels = [
      'gpt-oss-120b',
      'llama3.1-8b',
      'llama-3.3-70b',
      'qwen-3-235b-a22b-instruct-2507',
      'zai-glm-4.7',
    ];
    for (const model of cerebrasModels) {
      const cost = calculateLlmCost('cerebras', model, 10_000, 10_000);
      expect(cost, `cerebras model ${model} silently billed $0`).toBeGreaterThan(0);
      expect(llmPricing.cerebras[model], `missing rate for ${model}`).toBeDefined();
    }
  });

  it('every GroqModel enum value has a pricing entry (no $0 holes)', () => {
    // Mirrors libraries/typescript/src/providers/groq-llm.ts GroqModel.
    const groqModels = [
      'llama-3.3-70b-versatile',
      'llama-3.1-8b-instant',
      'llama-3.3-70b-specdec',
      'llama3-70b-8192',
      'llama3-8b-8192',
      'mixtral-8x7b-32768',
      'gemma2-9b-it',
    ];
    for (const model of groqModels) {
      const cost = calculateLlmCost('groq', model, 10_000, 10_000);
      expect(cost, `groq model ${model} silently billed $0`).toBeGreaterThan(0);
      expect(llmPricing.groq[model], `missing rate for ${model}`).toBeDefined();
    }
  });

  it('groq specdec carries a higher output rate than versatile', () => {
    // specdec has $0.99/M output (vs $0.79/M versatile) per Groq's published
    // pricing for speculative-decoding endpoints — verify the rate isn't
    // silently aliased to versatile's.
    const specCost = calculateLlmCost('groq', 'llama-3.3-70b-specdec', 0, 1_000_000);
    const verCost = calculateLlmCost('groq', 'llama-3.3-70b-versatile', 0, 1_000_000);
    expect(specCost).toBeCloseTo(0.99, 6);
    expect(verCost).toBeCloseTo(0.79, 6);
    expect(specCost).toBeGreaterThan(verCost);
  });
});

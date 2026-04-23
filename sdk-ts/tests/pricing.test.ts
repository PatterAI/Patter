import { describe, it, expect } from 'vitest';
import {
  DEFAULT_PRICING,
  mergePricing,
  calculateSttCost,
  calculateTtsCost,
  calculateRealtimeCost,
  calculateTelephonyCost,
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
    expect(merged.deepgram.price).toBe(0.0043);
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
    expect(cost).toBeCloseTo(0.0043, 4);
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
    expect(cost).toBeCloseTo(0.18, 2);
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
});

describe('calculateTelephonyCost', () => {
  it('calculates twilio cost for 120 seconds', () => {
    const pricing = mergePricing();
    const cost = calculateTelephonyCost('twilio', 120, pricing);
    expect(cost).toBeCloseTo(0.026, 3);
  });

  it('calculates telnyx cost', () => {
    const pricing = mergePricing();
    const cost = calculateTelephonyCost('telnyx', 60, pricing);
    expect(cost).toBeCloseTo(0.007, 3);
  });
});

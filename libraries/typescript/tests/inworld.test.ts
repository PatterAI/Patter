/**
 * Inworld integration: TTS pricing/models + Router LLM preset.
 *
 * Rates verified against the On-Demand list price on https://inworld.ai/pricing
 * (TTS-2 $25/1M, TTS 1.5 Max $35/1M, TTS 1.5 Mini $15/1M) and the
 * OpenAI-compatible Router endpoint
 * (https://docs.inworld.ai/router/openai-compatibility).
 */
import { describe, it, expect } from 'vitest';
import { DEFAULT_PRICING, PricingUnit } from '../src/pricing';
import { inworld } from '../src/providers';
import { InworldLLM } from '../src';
import { INWORLD_ROUTER_BASE_URL } from '../src/llm/inworld';

describe('Inworld TTS pricing', () => {
  it('prices all three models at On-Demand rates (per 1k chars)', () => {
    const p = DEFAULT_PRICING.inworld;
    expect(p.unit).toBe(PricingUnit.THOUSAND_CHARS);
    expect(p.price).toBe(0.025); // default = TTS-2
    expect(p.models?.['inworld-tts-2']).toEqual({ price: 0.025 }); // $25/1M
    expect(p.models?.['inworld-tts-1.5-max']).toEqual({ price: 0.035 }); // $35/1M
    expect(p.models?.['inworld-tts-1.5-mini']).toEqual({ price: 0.015 }); // $15/1M
  });
});

describe('providers.inworld TTS config helper', () => {
  it('builds an inworld TTS config (parity with Python)', () => {
    const cfg = inworld({ apiKey: 'tok', voice: 'Olivia' });
    expect(cfg.provider).toBe('inworld');
    expect(cfg.voice).toBe('Olivia');
  });

  it('defaults the voice to Ashley', () => {
    expect(inworld({ apiKey: 'tok' }).voice).toBe('Ashley');
  });
});

describe('Inworld Router LLM (OpenAI-compatible)', () => {
  it('constructs with an explicit key and the distinct provider key', () => {
    const llm = new InworldLLM({ apiKey: 'basic-token', model: 'openai/gpt-4o-mini' });
    expect(llm).toBeInstanceOf(InworldLLM);
    // Distinct from the char-priced TTS 'inworld' key (token-based, at-cost).
    expect(InworldLLM.providerKey).toBe('inworld_router');
  });

  it('defaults the base URL to the Router endpoint', () => {
    expect(INWORLD_ROUTER_BASE_URL).toBe('https://api.inworld.ai/v1');
  });

  it('reads INWORLD_API_KEY from the environment', () => {
    const prev = process.env.INWORLD_API_KEY;
    process.env.INWORLD_API_KEY = 'from-env';
    try {
      expect(
        () => new InworldLLM({ model: 'anthropic/claude-haiku-4-5-20251001' }),
      ).not.toThrow();
    } finally {
      if (prev === undefined) delete process.env.INWORLD_API_KEY;
      else process.env.INWORLD_API_KEY = prev;
    }
  });

  it('requires an api key', () => {
    const prev = process.env.INWORLD_API_KEY;
    delete process.env.INWORLD_API_KEY;
    try {
      expect(() => new InworldLLM({ model: 'openai/gpt-4o-mini' })).toThrow(
        /apiKey|INWORLD_API_KEY/,
      );
    } finally {
      if (prev !== undefined) process.env.INWORLD_API_KEY = prev;
    }
  });
});

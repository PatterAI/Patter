/*
 * Unit tests for the bring-your-own-license denoiser registry (TS parity).
 *
 * MOCK: no real inference. Krisp does not publish an official Node.js SDK, so
 * resolving a ``krisp-*`` id throws the same clear guidance the KrispVivaFilter
 * scaffold uses (use the Python SDK or provide a custom AudioFilter). These
 * tests assert the API surface (registry ids + session-type metadata) and the
 * error semantics match the Python SDK.
 */
import { describe, it, expect } from 'vitest';
import {
  DENOISERS,
  DENOISER_IDS,
  resolveDenoiser,
  resolveEffectiveAudioFilter,
} from '../../src/providers/denoiser';
import { KrispModelKind } from '../../src/providers/krisp-filter';
import type { AudioFilter } from '../../src/types';

describe('denoiser registry (TS)', () => {
  it('exposes exactly the two Krisp model ids', () => {
    expect(new Set(DENOISER_IDS)).toEqual(
      new Set(['krisp-viva-tel-v2', 'krisp-bvc-o-pro-v3']),
    );
    expect(new Set(Object.keys(DENOISERS))).toEqual(new Set(DENOISER_IDS));
  });

  it('maps the telephony model to the NC session and BVC model to the BVC session', () => {
    expect(DENOISERS['krisp-viva-tel-v2'].kind).toBe(KrispModelKind.NC);
    expect(DENOISERS['krisp-bvc-o-pro-v3'].kind).toBe(KrispModelKind.BVC);
  });

  it('throws a clear "no Node Krisp SDK" error for the VIVA telephony id', () => {
    expect(() => resolveDenoiser('krisp-viva-tel-v2')).toThrow(
      /not yet available for the Patter TypeScript SDK|Python SDK/,
    );
  });

  it('throws the same clear error for the BVC id (parity of error semantics)', () => {
    expect(() => resolveDenoiser('krisp-bvc-o-pro-v3')).toThrow(
      /not yet available for the Patter TypeScript SDK|Python SDK/,
    );
  });

  it('throws an actionable error for an unknown denoiser id', () => {
    expect(() => resolveDenoiser('krisp-does-not-exist')).toThrow(/Unknown denoiser/);
  });
});

describe('resolveEffectiveAudioFilter (TS)', () => {
  const stubFilter: AudioFilter = {
    process: async (pcm) => pcm,
    close: async () => undefined,
  };

  it('returns undefined when neither audioFilter nor denoiser is set', () => {
    expect(resolveEffectiveAudioFilter({} as never)).toBeUndefined();
  });

  it('prefers the explicit audioFilter instance over the denoiser id', () => {
    // Even with a (throwing) krisp denoiser also set, the explicit filter wins
    // and the denoiser is never resolved.
    const result = resolveEffectiveAudioFilter({
      audioFilter: stubFilter,
      denoiser: 'krisp-bvc-o-pro-v3',
    } as never);
    expect(result).toBe(stubFilter);
  });

  it('resolves the denoiser id when no explicit audioFilter is set (throws for Krisp)', () => {
    expect(() =>
      resolveEffectiveAudioFilter({ denoiser: 'krisp-viva-tel-v2' } as never),
    ).toThrow(/not yet available for the Patter TypeScript SDK|Python SDK/);
  });
});

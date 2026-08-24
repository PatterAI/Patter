import { describe, it, expect } from 'vitest';
import {
  XAI_VOICE_IDS,
  XAI_VOICES,
  XAI_DEFAULT_VOICE,
  isXaiBuiltinVoice,
  normalizeXaiVoice,
  getXaiVoice,
} from '../../src/providers/xai-voices';
import { xai, xaiAsr } from '../../src/providers';

/**
 * [unit] xAI built-in voice catalog — roster shape, the case-insensitive
 * built-in predicate, wire normalization, and the `xai()` / `xaiAsr()` config
 * helpers that consume it. Real code throughout; no external boundary.
 */
describe('[unit] xAI voice catalog', () => {
  it('has exactly 26 voices, with eve as the default and listed first', () => {
    expect(XAI_VOICES).toHaveLength(26);
    expect(XAI_VOICE_IDS).toHaveLength(26);
    expect(XAI_DEFAULT_VOICE).toBe('eve');
    expect(XAI_VOICES[0].id).toBe('eve');
  });

  it('has all-lowercase, unique ids in both XAI_VOICE_IDS and XAI_VOICES', () => {
    for (const id of XAI_VOICE_IDS) {
      expect(id).toBe(id.toLowerCase());
    }
    expect(new Set(XAI_VOICE_IDS).size).toBe(XAI_VOICE_IDS.length);

    const infoIds = XAI_VOICES.map((v) => v.id);
    for (const id of infoIds) {
      expect(id).toBe(id.toLowerCase());
    }
    expect(new Set(infoIds).size).toBe(infoIds.length);
    // Both exports describe the same 26 ids.
    expect(new Set(infoIds)).toEqual(new Set(XAI_VOICE_IDS));
  });

  it('isXaiBuiltinVoice is case-insensitive for built-ins and false for anything else', () => {
    expect(isXaiBuiltinVoice('EVE')).toBe(true);
    expect(isXaiBuiltinVoice('eve')).toBe(true);
    expect(isXaiBuiltinVoice('Leo')).toBe(true);
    // A custom cloned voice_id is NOT a built-in, but it is never "invalid" —
    // isXaiBuiltinVoice just says "not a documented preset".
    expect(isXaiBuiltinVoice('my-custom-id')).toBe(false);
  });

  it('normalizeXaiVoice trims + lowercases built-ins, and only trims custom ids', () => {
    expect(normalizeXaiVoice(' Leo ')).toBe('leo');
    expect(normalizeXaiVoice('EVE')).toBe('eve');
    // Unrecognized id: trimmed, but case is preserved — xAI matches custom
    // voice_ids exactly.
    expect(normalizeXaiVoice('CustomVoice_1')).toBe('CustomVoice_1');
    expect(normalizeXaiVoice('  CustomVoice_1  ')).toBe('CustomVoice_1');
  });

  it('getXaiVoice looks up tone/use-case metadata case-insensitively', () => {
    expect(getXaiVoice('carina')?.useCases).toContain('Wellness');
    expect(getXaiVoice('CARINA')?.useCases).toContain('Wellness');
    expect(getXaiVoice('  carina  ')?.tone).toBe('Soft, empathetic, and soothing');
    expect(getXaiVoice('not-a-voice')).toBeUndefined();
  });
});

describe('[unit] xai() / xaiAsr() config helpers', () => {
  it('xai() defaults to the "xai_tts" provider key and the eve voice', () => {
    const config = xai({ apiKey: 'xai-test-key' });
    expect(config.provider).toBe('xai_tts');
    expect(config.apiKey).toBe('xai-test-key');
    expect(config.voice).toBe('eve');
  });

  it('xai() normalizes a caller-supplied voice before storing it', () => {
    const config = xai({ apiKey: 'xai-test-key', voice: ' LEO ' });
    expect(config.voice).toBe('leo');
  });

  it('xai() leaves a custom voice_id untouched (trimmed only)', () => {
    const config = xai({ apiKey: 'xai-test-key', voice: 'CustomVoice_1' });
    expect(config.voice).toBe('CustomVoice_1');
  });

  it('xaiAsr() defaults to the "xai" provider key and "en" language', () => {
    const config = xaiAsr({ apiKey: 'xai-test-key' });
    expect(config.provider).toBe('xai');
    expect(config.apiKey).toBe('xai-test-key');
    expect(config.language).toBe('en');
  });

  it('xaiAsr() threads a caller-supplied language through', () => {
    const config = xaiAsr({ apiKey: 'xai-test-key', language: 'it' });
    expect(config.language).toBe('it');
  });
});

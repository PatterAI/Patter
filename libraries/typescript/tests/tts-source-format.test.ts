import { describe, it, expect } from 'vitest';
import { CartesiaTTS } from '../src/providers/cartesia-tts';
import { OpenAITTS } from '../src/providers/openai-tts';
import { ElevenLabsTTS } from '../src/providers/elevenlabs-tts';
import {
  resolveTtsSourceFormat,
  CARRIER_WIRE_FORMAT,
  formatsMatch,
} from '../src/audio/format';

describe('[unit] TTS providers declare a uniform source audio format', () => {
  it('Cartesia default is PCM16 at its configured rate (no fixed assumption)', () => {
    const tts = new CartesiaTTS('k', { sampleRate: 24000 });
    expect(tts.sourceAudioFormat()).toEqual({ encoding: 'pcm_s16le', sampleRate: 24000 });
    expect(resolveTtsSourceFormat(tts)).toEqual({ encoding: 'pcm_s16le', sampleRate: 24000 });
  });

  it('Cartesia at 8 kHz declares 8 kHz — the chipmunk source the old sender ignored', () => {
    const tts = new CartesiaTTS('k', { sampleRate: 8000 });
    expect(resolveTtsSourceFormat(tts).sampleRate).toBe(8000);
    // Not μ-law, so it is NOT passthrough — the sender must resample 8k→8k
    // (identity rate, correct pitch) NOT decimate 16k→8k.
    expect(formatsMatch(resolveTtsSourceFormat(tts), CARRIER_WIRE_FORMAT.twilio)).toBe(false);
  });

  it('CartesiaTTS.forTwilio() emits μ-law 8 kHz → carrier-native passthrough (criterion 6)', () => {
    const tts = CartesiaTTS.forTwilio('k');
    expect(tts.sourceAudioFormat()).toEqual({ encoding: 'mulaw', sampleRate: 8000 });
    expect(formatsMatch(tts.sourceAudioFormat(), CARRIER_WIRE_FORMAT.twilio)).toBe(true);
  });

  it('CartesiaTTS.forTelnyx() emits μ-law 8 kHz → passthrough on Telnyx PCMU wire', () => {
    const tts = CartesiaTTS.forTelnyx('k');
    expect(formatsMatch(tts.sourceAudioFormat(), CARRIER_WIRE_FORMAT.telnyx)).toBe(true);
  });

  it('OpenAI TTS declares its real target rate (16 kHz default, 8 kHz when configured)', () => {
    expect(new OpenAITTS('k').sourceAudioFormat()).toEqual({
      encoding: 'pcm_s16le',
      sampleRate: 16000,
    });
    expect(
      new OpenAITTS('k', 'alloy', 'gpt-4o-mini-tts', null, null, true, 8000).sourceAudioFormat(),
    ).toEqual({ encoding: 'pcm_s16le', sampleRate: 8000 });
  });

  it('ElevenLabs forTwilio resolves to μ-law passthrough via its outputFormat string', () => {
    const tts = ElevenLabsTTS.forTwilio('k');
    expect(formatsMatch(resolveTtsSourceFormat(tts), CARRIER_WIRE_FORMAT.twilio)).toBe(true);
    // Default (web) ElevenLabs is PCM 16 kHz → resample path, not passthrough.
    expect(formatsMatch(resolveTtsSourceFormat(new ElevenLabsTTS('k')), CARRIER_WIRE_FORMAT.twilio)).toBe(
      false,
    );
  });
});

/**
 * xAI (Grok) built-in voice catalog.
 *
 * Shared by the TTS, Realtime, and STT providers so the roster, the default
 * voice, and the wire-normalization rule live in exactly one place instead of
 * being duplicated (and drifting) per adapter.
 *
 * xAI's built-in voice ids are lowercase and case-insensitive. A caller may
 * also pass an arbitrary custom cloned `voice_id` (see `xaiCreateCustomVoice`
 * in `./xai-tts`) — this module must never reject those, so
 * {@link isXaiBuiltinVoice} and {@link normalizeXaiVoice} are permissive by
 * design: anything that isn't a recognized built-in id is left alone.
 *
 * Source: https://docs.x.ai/developers/model-capabilities/audio/voice
 */

/** xAI's default built-in voice. */
export const XAI_DEFAULT_VOICE = 'eve';

/** All 26 built-in xAI voice ids, alphabetical. */
export const XAI_VOICE_IDS = [
  'altair',
  'ara',
  'atlas',
  'carina',
  'castor',
  'celeste',
  'cosmo',
  'eve',
  'helios',
  'helix',
  'iris',
  'kepler',
  'leo',
  'lumen',
  'luna',
  'lux',
  'naksh',
  'orion',
  'perseus',
  'rex',
  'rigel',
  'sal',
  'sirius',
  'ursa',
  'zagan',
  'zenith',
] as const;

/** One of xAI's 26 built-in voice ids (always lowercase). */
export type XaiVoiceId = (typeof XAI_VOICE_IDS)[number];

/** Tone and suggested use cases for one built-in xAI voice. */
export interface XaiVoiceInfo {
  readonly id: XaiVoiceId;
  readonly tone: string;
  readonly useCases: readonly string[];
}

/**
 * Full built-in voice roster in xAI's documented display order — `eve` (the
 * default) first, then the rest alphabetically. Tone and use cases are from
 * the xAI Voice Overview (docs.x.ai/developers/model-capabilities/audio/voice).
 */
export const XAI_VOICES: readonly XaiVoiceInfo[] = [
  { id: 'eve', tone: 'Energetic and upbeat', useCases: [] },
  {
    id: 'altair',
    tone: 'Elegant, refined, and effortlessly premium',
    useCases: ['Advertising', 'Narration'],
  },
  { id: 'ara', tone: 'Warm and friendly', useCases: [] },
  {
    id: 'atlas',
    tone: 'Confident, commanding, and reassuring',
    useCases: ['Sales', 'Assistant'],
  },
  {
    id: 'carina',
    tone: 'Soft, empathetic, and soothing',
    useCases: ['Wellness', 'Support'],
  },
  {
    id: 'castor',
    tone: 'Charismatic, down-to-earth, and easygoing',
    useCases: ['Sales', 'Support'],
  },
  {
    id: 'celeste',
    tone: 'Compassionate, confident, and reassuring',
    useCases: ['Support', 'Assistant'],
  },
  {
    id: 'cosmo',
    tone: 'Bright, curious, and easy to follow',
    useCases: ['Education', 'Podcast'],
  },
  {
    id: 'helios',
    tone: 'Upbeat, energetic, and endlessly versatile',
    useCases: ['Assistant', 'Wellness'],
  },
  {
    id: 'helix',
    tone: 'Bold, dynamic, and adrenaline-fueled',
    useCases: ['Commentary', 'Podcast'],
  },
  {
    id: 'iris',
    tone: 'Friendly, upbeat, and naturally charming',
    useCases: ['Sales', 'Support'],
  },
  {
    id: 'kepler',
    tone: 'Inventive, forward-thinking, and charismatic',
    useCases: ['Advertising', 'Podcast'],
  },
  { id: 'leo', tone: 'Authoritative and strong', useCases: [] },
  {
    id: 'lumen',
    tone: 'Warm, articulate, and engaging',
    useCases: ['Education', 'Advertising'],
  },
  {
    id: 'luna',
    tone: 'Gentle, patient, and deeply nurturing',
    useCases: ['Education', 'Assistant'],
  },
  {
    id: 'lux',
    tone: 'Grounded, calm, and quietly wise',
    useCases: ['Wellness', 'Narration'],
  },
  {
    id: 'naksh',
    tone: 'Warm, thoughtful, and wise',
    useCases: ['Assistant', 'Support'],
  },
  {
    id: 'orion',
    tone: 'Rich, cinematic, and resonant',
    useCases: ['Narration', 'Audiobooks'],
  },
  {
    id: 'perseus',
    tone: 'Strong, confident, and trustworthy',
    useCases: ['Advertising', 'Narration'],
  },
  { id: 'rex', tone: 'Confident and clear', useCases: [] },
  {
    id: 'rigel',
    tone: 'Precise, professional, and calmly confident',
    useCases: ['Assistant', 'Support'],
  },
  { id: 'sal', tone: 'Smooth and balanced', useCases: [] },
  {
    id: 'sirius',
    tone: 'Quick-witted, clever, and playful',
    useCases: ['Commentary', 'Characters'],
  },
  {
    id: 'ursa',
    tone: 'Friendly, warm, and steadfast',
    useCases: ['Assistant', 'Podcast'],
  },
  {
    id: 'zagan',
    tone: 'Powerful, dramatic, and unmistakable',
    useCases: ['Characters', 'Narration'],
  },
  {
    id: 'zenith',
    tone: 'Sharp, focused, and driven',
    useCases: ['Sales', 'Advertising'],
  },
];

/** Lookup set backing {@link isXaiBuiltinVoice} / {@link normalizeXaiVoice}. */
const BUILTIN_VOICE_SET: ReadonlySet<string> = new Set(XAI_VOICE_IDS);

/**
 * True when `voice` (trimmed, case-insensitive) names one of xAI's 26
 * built-in voices. `false` covers everything else, INCLUDING a legitimate
 * custom cloned `voice_id` — callers must treat `false` as "not a documented
 * preset", never as "invalid".
 */
export function isXaiBuiltinVoice(voice: string): voice is XaiVoiceId {
  return BUILTIN_VOICE_SET.has(voice.trim().toLowerCase());
}

/**
 * Normalize a voice id for the wire: trim whitespace, then lowercase ONLY
 * when it names a built-in voice (xAI's built-in ids are case-insensitive).
 * A custom cloned `voice_id` is an arbitrary opaque string, so it is trimmed
 * but never case-folded — xAI matches those ids exactly.
 */
export function normalizeXaiVoice(voice: string): string {
  const trimmed = voice.trim();
  const lowered = trimmed.toLowerCase();
  return BUILTIN_VOICE_SET.has(lowered) ? lowered : trimmed;
}

/** Look up tone/use-case metadata for a built-in voice id (case-insensitive). */
export function getXaiVoice(id: string): XaiVoiceInfo | undefined {
  const normalized = id.trim().toLowerCase();
  return XAI_VOICES.find((v) => v.id === normalized);
}

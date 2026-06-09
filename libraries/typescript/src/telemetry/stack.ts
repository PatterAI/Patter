/**
 * Anonymous capture of the composed voice stack (carrier + STT + TTS + LLM).
 *
 * For a pipeline agent the interesting telemetry is *which providers and models*
 * were composed. Each provider adapter exposes a stable static `providerKey`
 * (minification-safe, the canonical vendor id) and stores its configured model.
 * This module turns those into two anonymous fields per layer: a low-cardinality
 * vendor (closed allowlist) and a sanitized model token.
 *
 * PII safety for the model token (the one field that is not a closed enum): a
 * model string can embed customer data — fine-tuned ids (`ft:gpt-4o:acme:...`),
 * self-hosted endpoints/paths, or custom names. The sanitizer emits
 * `"{vendor}-other"` for anything with `:` / `/` / whitespace / unusual chars or
 * longer than 40 chars, so a brand or org name can never reach the wire. Known
 * public ids (`nova-3`, `gpt-4o`, `claude-haiku-4-5`…) pass through as
 * `"{vendor}-{token}"` with the trailing release date stripped. Keep byte-for-byte
 * identical to `getpatter/telemetry/stack.py`.
 */

/** Closed vendor allowlist for the per-layer `*_provider` fields. */
export const STACK_VENDORS: ReadonlySet<string> = new Set([
  'openai',
  'anthropic',
  'google',
  'cerebras',
  'groq',
  'deepgram',
  'elevenlabs',
  'cartesia',
  'whisper',
  'soniox',
  'assemblyai',
  'speechmatics',
  'lmnt',
  'rime',
  'inworld',
  'telnyx',
  'other',
]);

// providerKey values that carry a layer suffix or alias → canonical vendor.
const VENDOR_ALIASES: Record<string, string> = {
  cartesia_stt: 'cartesia',
  cartesia_tts: 'cartesia',
  openai_tts: 'openai',
  openai_transcribe: 'openai',
  elevenlabs_ws: 'elevenlabs',
  telnyx_stt: 'telnyx',
  telnyx_tts: 'telnyx',
};

const MODEL_TOKEN_RE = /^[a-z0-9][a-z0-9.-]{0,40}$/;
const RAW_UNSAFE_RE = /[^a-z0-9._-]/;
const DATE_SUFFIX_RE = /-\d{8}$/;

/** Map a provider's `providerKey` to the closed vendor allowlist. */
export function vendorOf(providerKey: string | null | undefined): string {
  if (!providerKey) return 'other';
  const v = VENDOR_ALIASES[providerKey] ?? providerKey;
  return STACK_VENDORS.has(v) ? v : 'other';
}

/**
 * Sanitize a raw model string into `"{vendor}-{token}"` (or `"{vendor}-other"`).
 * Returns `"{vendor}-other"` for anything that could carry PII (fine-tuned ids,
 * self-hosted paths, custom names) — anything with `:` / `/` / whitespace / non
 * `[a-z0-9._-]` chars, or longer than 40 chars.
 */
export function modelToken(vendor: string, rawModel: string | null | undefined): string {
  if (!rawModel) return `${vendor}-other`;
  const m = rawModel.trim().toLowerCase();
  if (m.length > 40 || RAW_UNSAFE_RE.test(m)) return `${vendor}-other`;
  const token = m
    .replace(/_/g, '-')
    .replace(DATE_SUFFIX_RE, '')
    .replace(/^[-.]+|[-.]+$/g, '');
  return token ? `${vendor}-${token}` : `${vendor}-other`;
}

/** Export the shape guard so events.ts/the relay validate identically. */
export { MODEL_TOKEN_RE };

function readProviderKey(obj: unknown): string | null {
  const ctor = (obj as { constructor?: { providerKey?: unknown } } | null | undefined)
    ?.constructor;
  const key = ctor?.providerKey;
  return typeof key === 'string' && key ? key : null;
}

function readModel(obj: unknown): string {
  const rec = obj as Record<string, unknown> | null | undefined;
  for (const attr of ['model', 'modelId', '_model']) {
    const v = rec?.[attr];
    if (typeof v === 'string' && v) return v;
  }
  return '';
}

function layerDims(
  obj: unknown,
  providerField: string,
  modelField: string,
): Record<string, string> {
  if (obj === null || obj === undefined) return {};
  const vendor = vendorOf(readProviderKey(obj));
  return { [providerField]: vendor, [modelField]: modelToken(vendor, readModel(obj)) };
}

/**
 * Anonymous per-layer vendor+model dims for a composed (pipeline) agent. Absent
 * layers (realtime/convai engines) are omitted — buildEvent drops missing dims.
 */
export function stackDimensions(
  stt: unknown,
  tts: unknown,
  llm: unknown,
): Record<string, string> {
  return {
    ...layerDims(stt, 'stt_provider', 'stt_model'),
    ...layerDims(tts, 'tts_provider', 'tts_model'),
    ...layerDims(llm, 'llm_provider', 'llm_model'),
  };
}

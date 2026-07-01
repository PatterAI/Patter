/**
 * Bring-your-own-license denoiser registry (TypeScript parity with the Python
 * `getpatter.providers.denoiser` module).
 *
 * Maps a small set of stable, string model ids to a resolved {@link AudioFilter}.
 * Selection is opt-in via `AgentOptions.denoiser`; the default (`undefined`)
 * leaves the inbound audio path unchanged.
 *
 * The Krisp models are **bring-your-own-license**: Patter ships ZERO Krisp
 * binaries, license keys, or `.kef` model files. Because Krisp does not publish
 * an official Node.js (server) SDK, resolving a `krisp-*` id constructs the
 * {@link KrispVivaFilter} scaffold, which throws the same clear guidance
 * (use the Python SDK, or provide a custom `AudioFilter`). This keeps the TS
 * API surface and error semantics identical to Python — a selected denoiser
 * never silently degrades to a no-op.
 */
import type { AgentOptions, AudioFilter } from '../types';
import { KrispModelKind, KrispVivaFilter } from './krisp-filter';

/** Env var pointing at the operator's directory of licensed Krisp `.kef` models. */
export const KRISP_MODELS_DIR_ENV = 'KRISP_MODELS_DIR';

/** Static description of a selectable denoiser model (parity with Python `DenoiserSpec`). */
export interface DenoiserSpec {
  /** Public, stable string id surfaced through `AgentOptions.denoiser`. */
  readonly modelId: string;
  /** Krisp session family the model loads through. */
  readonly kind: KrispModelKind;
  /** BYO `.kef` file expected under `KRISP_MODELS_DIR`. */
  readonly filename: string;
  /** Human-readable one-liner (docs / error context only). */
  readonly description: string;
}

/** The two REAL Krisp model ids Patter recognises. */
export const DENOISERS: Readonly<Record<string, DenoiserSpec>> = {
  'krisp-viva-tel-v2': {
    modelId: 'krisp-viva-tel-v2',
    kind: KrispModelKind.NC,
    filename: 'krisp-viva-tel-v2.kef',
    description: 'Krisp VIVA telephony noise cancellation (NC session).',
  },
  'krisp-bvc-o-pro-v3': {
    modelId: 'krisp-bvc-o-pro-v3',
    kind: KrispModelKind.BVC,
    filename: 'krisp-bvc-o-pro-v3.kef',
    description: 'Krisp Background Voice Cancellation, pro (BVC session).',
  },
} as const;

/** Registered denoiser ids (see {@link DENOISERS}). */
export const DENOISER_IDS: readonly string[] = Object.keys(DENOISERS);

/**
 * Resolve a string denoiser id to a concrete {@link AudioFilter}.
 *
 * @throws if `name` is not a registered denoiser id, or (for Krisp ids) the
 *   Node Krisp SDK is unavailable — the thrown error names the Python SDK /
 *   custom-AudioFilter alternatives.
 */
export function resolveDenoiser(name: string): AudioFilter {
  const spec = DENOISERS[name];
  if (!spec) {
    const known = [...DENOISER_IDS].sort().join(', ');
    throw new Error(`Unknown denoiser '${name}'. Known denoisers: ${known}.`);
  }
  const modelsDir = process.env[KRISP_MODELS_DIR_ENV];
  const modelPath = modelsDir ? `${modelsDir}/${spec.filename}` : undefined;
  // Constructs the scaffold, which throws the clear "no Node Krisp SDK" guidance
  // (single source of truth for the error). Kept structurally identical to
  // Python: the session family is passed through for parity.
  return new KrispVivaFilter({ modelPath, sessionType: spec.kind });
}

/**
 * Resolve the effective inbound audio filter for an agent.
 *
 * `audioFilter` (explicit instance) and `denoiser` (string id) are PARALLEL
 * opt-ins: when both are set the explicit `audioFilter` wins and the denoiser
 * is never resolved. Returns `undefined` when neither is set.
 */
export function resolveEffectiveAudioFilter(
  agent: Pick<AgentOptions, 'audioFilter' | 'denoiser'>,
): AudioFilter | undefined {
  if (agent.audioFilter) {
    return agent.audioFilter;
  }
  if (agent.denoiser) {
    return resolveDenoiser(agent.denoiser);
  }
  return undefined;
}

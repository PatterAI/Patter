/**
 * OpenClaw agent-runtime LLM preset for Patter's pipeline mode.
 *
 * Thin preset over {@link OpenAICompatibleLLMProvider}, aligned with the
 * shipped ``openclawConsult`` builder in ``src/consult.ts``: same loopback
 * base URL (``:18789/v1``), same ``OPENCLAW_API_KEY`` env var, same
 * ``model="openclaw/<agent>"`` pass-through convention, same agent-id charset
 * rule, and the same ``x-openclaw-session-key`` session header. Takes an
 * ``agent`` id (not a raw model string), exactly like ``openclawConsult``.
 *
 * OpenClaw runs tools / memory / skills internally before replying, so a turn
 * can take 30-90 s — hence the 120 s default timeout (unlike the consult
 * preset's phone-safe 30 s filler default; here the runtime IS the per-turn
 * brain, not an on-demand escalation). It keys sessions off BOTH the OpenAI
 * ``user`` field and the ``x-openclaw-session-key`` header, so the preset
 * enables both for one runtime session per phone call.
 */
import {
  OpenAICompatibleLLMProvider,
  type OpenAICompatibleLLMOptions,
} from './openai-compatible';

/** Default OpenClaw base URL (loopback). Byte-identical to the consult preset. */
const BASE_URL = 'http://127.0.0.1:18789/v1';
/** Env var OpenClaw reads its operator-grade bearer from. */
const API_KEY_ENV = 'OPENCLAW_API_KEY';
/** Header OpenClaw keys sessions off (secondary to the ``user`` field). */
const SESSION_HEADER = 'x-openclaw-session-key';
/** Per-call session prefix → one OpenClaw session per phone call. */
const SESSION_USER_PREFIX = 'patter-call-';
/** Default timeout (seconds): runtimes run tools before replying. */
const DEFAULT_TIMEOUT_S = 120;
/**
 * Agent ids cross into the gateway via the model string — restrict to a safe
 * set. Byte-identical to ``OPENCLAW_AGENT_RE`` in ``src/consult.ts`` so an
 * agent valid for consult is valid here and vice-versa.
 */
const OPENCLAW_AGENT_RE = /^[A-Za-z0-9._:/-]+$/;

/** Constructor options for the OpenClaw ``LLM`` preset. */
export interface OpenClawLLMOptions {
  /**
   * OpenClaw agent id (e.g. ``"receptionist"``). Mapped to
   * ``model="openclaw/<agent>"``; an already-namespaced id (``"openclaw/x"``,
   * ``"agent:x"``) is passed through unchanged. **Required.**
   */
  agent: string;
  /** Override the OpenClaw base URL (rarely needed). */
  baseUrl?: string;
  /** Bearer token. Falls back to ``OPENCLAW_API_KEY`` env var when omitted. */
  apiKey?: string;
  /** Per-request timeout in seconds. Default ``120``. */
  timeout?: number;
  /** Extra headers merged after the SDK ``User-Agent``. */
  extraHeaders?: Record<string, string>;
  /** Sampling temperature [0, 2]. */
  temperature?: number;
  /** Max tokens in the assistant response (sent as ``max_completion_tokens``). */
  maxTokens?: number;
  /** OpenAI-style ``response_format`` for JSON mode / structured outputs. */
  responseFormat?: Record<string, unknown>;
  /** Whether to allow parallel tool calls. */
  parallelToolCalls?: boolean;
  /** ``"auto" | "none" | "required"`` or a specific tool object. */
  toolChoice?: string | Record<string, unknown>;
  /** Sampling seed for reproducible outputs. */
  seed?: number;
  /** Nucleus sampling cutoff in [0, 1]. */
  topP?: number;
  /** Penalty in [-2, 2] applied to repeated tokens. */
  frequencyPenalty?: number;
  /** Penalty in [-2, 2] applied to seen tokens. */
  presencePenalty?: number;
  /** Stop sequence(s). */
  stop?: string | string[];
}

/**
 * OpenClaw agent-runtime LLM provider (OpenAI-compatible, streaming).
 *
 * @example
 * ```ts
 * import * as openclaw from "getpatter/llm/openclaw";
 * const llm = new openclaw.LLM({ agent: "receptionist" }); // reads OPENCLAW_API_KEY
 * ```
 */
export class LLM extends OpenAICompatibleLLMProvider {
  static readonly providerKey = 'openclaw';

  constructor(opts: OpenClawLLMOptions) {
    const agent = opts?.agent;
    if (!agent || !OPENCLAW_AGENT_RE.test(agent)) {
      throw new Error(
        `Invalid OpenClaw agent id: ${JSON.stringify(agent)}. ` +
          'Allowed characters: letters, digits, dot, underscore, colon, slash, dash.',
      );
    }
    // Already-namespaced ids (``openclaw/x``, ``agent:x``) pass through; a
    // bare id is namespaced to ``openclaw/<agent>``. Identical rule to
    // ``openclawConsult`` in src/consult.ts.
    const model = agent.includes('/') || agent.includes(':') ? agent : `openclaw/${agent}`;
    const options: OpenAICompatibleLLMOptions = {
      apiKey: opts.apiKey,
      apiKeyEnv: API_KEY_ENV,
      baseUrl: opts.baseUrl ?? BASE_URL,
      model,
      timeout: opts.timeout ?? DEFAULT_TIMEOUT_S,
      sessionUserPrefix: SESSION_USER_PREFIX,
      // Wire-identical to the prior behaviour: header value is the raw call id
      // (empty prefix), and OpenClaw's gateway also derives the session from
      // the ``user`` field above. No separate memory-scope header.
      sessionIdHeader: SESSION_HEADER,
      sessionIdPrefix: '',
      extraHeaders: opts.extraHeaders,
      temperature: opts.temperature,
      maxTokens: opts.maxTokens,
      responseFormat: opts.responseFormat,
      parallelToolCalls: opts.parallelToolCalls,
      toolChoice: opts.toolChoice,
      seed: opts.seed,
      topP: opts.topP,
      frequencyPenalty: opts.frequencyPenalty,
      presencePenalty: opts.presencePenalty,
      stop: opts.stop,
    };
    super(options);
  }
}

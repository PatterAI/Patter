/**
 * Hermes agent-runtime LLM preset for Patter's pipeline mode.
 *
 * Thin preset over {@link OpenAICompatibleLLMProvider}: defaults the base URL,
 * model, env-key name, timeout, and session-continuity prefix for the Hermes
 * agent runtime so a user just writes ``phone.agent({ llm: new hermes.LLM() })``.
 *
 * Hermes runs tools / memory / skills internally before replying, so a single
 * conversation turn can take 30-90 s — hence the 120 s default timeout. Hermes
 * is stateless and keys continuity off HEADERS, not the OpenAI ``user`` field:
 * the preset sends ``X-Hermes-Session-Id: patter-call-<callId>`` on every turn
 * for per-call session / transcript continuity (on by default), and optionally
 * ``X-Hermes-Session-Key: <sessionKey>`` for long-term memory scoping when you
 * pass ``sessionKey``. (It also still emits ``user=patter-call-<callId>`` for
 * upstream-log correlation, but that is not what drives the session.)
 */
import {
  OpenAICompatibleLLMProvider,
  type OpenAICompatibleLLMOptions,
} from './openai-compatible';

/** Default Hermes agent-runtime base URL (loopback, operator-controlled). */
const BASE_URL = 'http://127.0.0.1:8642/v1';
/** Fallback model when neither ``model`` nor ``API_SERVER_MODEL_NAME`` is set. */
const DEFAULT_MODEL = 'hermes-agent';
/** Env var Hermes reads its bearer from. */
const API_KEY_ENV = 'API_SERVER_KEY';
/** Env var Hermes reads its model id from. */
const MODEL_ENV = 'API_SERVER_MODEL_NAME';
/** Per-call ``user`` prefix (upstream-log correlation; not the session driver). */
const SESSION_USER_PREFIX = 'patter-call-';
/** Header carrying the per-call session id (the primary continuity mechanism). */
const SESSION_ID_HEADER = 'X-Hermes-Session-Id';
/** Prefix for the session-id header value → ``X-Hermes-Session-Id: patter-call-<callId>``. */
const SESSION_ID_PREFIX = 'patter-call-';
/** Static header scoping long-term memory (sent only when ``sessionKey`` is set). */
const SESSION_KEY_HEADER = 'X-Hermes-Session-Key';
/** Default timeout (seconds): runtimes run tools before replying. */
const DEFAULT_TIMEOUT_S = 120;

/** Constructor options for the Hermes ``LLM`` preset. */
export interface HermesLLMOptions {
  /** Bearer token. Falls back to ``API_SERVER_KEY`` env var when omitted. */
  apiKey?: string;
  /** Override the Hermes base URL (rarely needed). */
  baseUrl?: string;
  /** Model id. Falls back to ``API_SERVER_MODEL_NAME`` env, then ``"hermes-agent"``. */
  model?: string;
  /** Per-request timeout in seconds. Default ``120``. */
  timeout?: number;
  /**
   * Long-term memory scope. When set, emits ``X-Hermes-Session-Key`` so Hermes
   * scopes durable memory to this value across calls. ``undefined`` (default)
   * means the header is not sent. Credential-grade — never logged.
   */
  sessionKey?: string;
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
 * Hermes agent-runtime LLM provider (OpenAI-compatible, streaming).
 *
 * @example
 * ```ts
 * import * as hermes from "getpatter/llm/hermes";
 * const llm = new hermes.LLM();                       // env-defaulted, keyless OK
 * const llm = new hermes.LLM({ apiKey: "...", model: "hermes-7b" });
 * ```
 */
export class LLM extends OpenAICompatibleLLMProvider {
  static readonly providerKey = 'hermes';

  constructor(opts: HermesLLMOptions = {}) {
    const model = opts.model ?? process.env[MODEL_ENV] ?? DEFAULT_MODEL;
    const options: OpenAICompatibleLLMOptions = {
      apiKey: opts.apiKey,
      apiKeyEnv: API_KEY_ENV,
      baseUrl: opts.baseUrl ?? BASE_URL,
      model,
      timeout: opts.timeout ?? DEFAULT_TIMEOUT_S,
      sessionUserPrefix: SESSION_USER_PREFIX,
      sessionIdHeader: SESSION_ID_HEADER,
      sessionIdPrefix: SESSION_ID_PREFIX,
      sessionKeyHeader: SESSION_KEY_HEADER,
      sessionKey: opts.sessionKey,
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

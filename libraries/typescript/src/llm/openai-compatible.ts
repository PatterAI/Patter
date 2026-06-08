/**
 * Generic OpenAI-compatible LLM provider for Patter's pipeline mode.
 *
 * Drives *any* OpenAI-compatible ``/chat/completions`` endpoint — an agent
 * runtime (Hermes, OpenClaw) or a local inference gateway (Ollama, vLLM,
 * LM Studio). Patter owns the carrier + STT + turn-taking + TTS; this
 * provider turns each conversation turn into a single
 * ``POST {baseUrl}/chat/completions`` request and speaks the response.
 *
 * PARITY NOTE (internal divergence, allowed by ``sdk-parity.md``): on the
 * Python side this provider subclasses ``OpenAILLMProvider`` and merely swaps
 * the ``AsyncOpenAI`` client (passing ``timeout=`` / ``base_url=``). The TS
 * base ``OpenAILLMProvider`` is a raw-``fetch`` class with a HARDCODED 30 s
 * timeout and ``baseUrl`` exposed as a ``protected get`` rather than a
 * constructor field, so the "swap the client" trick is impossible here.
 * Instead this is a STANDALONE ``implements LLMProvider`` class (same shape as
 * {@link GroqLLMProvider} / {@link CerebrasLLMProvider}) that owns its own
 * configurable timeout and reuses {@link parseOpenAISseStream}. Observably
 * identical to Python (same 60 s / 120 s ceilings, same ``user`` field, same
 * headers); only the timeout *mechanism* differs.
 *
 * Two additions over the base OpenAI provider:
 *
 * - **Long timeout.** Agent runtimes execute tools / memory / skills before
 *   replying, so a turn can take 30-90 s. The default is 60 s here (the
 *   presets raise it to 120 s), REPLACING the base provider's hardcoded 30 s.
 * - **Session continuity.** Three independent, opt-in signals — each gated on
 *   its own config, none coupled to another:
 *     - ``sessionUserPrefix`` → emits the OpenAI ``user`` field as
 *       ``` `${sessionUserPrefix}${callId}` ```. Used by runtimes that derive
 *       a session from ``user`` (e.g. OpenClaw's gateway).
 *     - ``sessionIdHeader`` (+ optional ``sessionIdPrefix``) → emits a per-call
 *       header carrying ``` `${sessionIdPrefix}${callId}` ``` for per-call
 *       session / transcript continuity on stateless runtimes that key off
 *       headers (e.g. Hermes' ``X-Hermes-Session-Id``).
 *     - ``sessionKeyHeader`` (+ ``sessionKey``) → emits a STATIC header for
 *       long-term memory scoping (e.g. Hermes' ``X-Hermes-Session-Key``); the
 *       value is the raw ``sessionKey``, never interpolated with the call id.
 *   All three are OFF by default — fully backward compatible. ``sessionKey`` is
 *   a credential-grade memory scope and is NEVER logged.
 *
 * Keyless gateways (Ollama / vLLM / LM Studio accept no key) are supported:
 * the ``Authorization`` header is simply omitted from the request (sending a
 * ``Bearer EMPTY`` placeholder breaks some gateways).
 */

import type { LLMChunk, LLMProvider, LLMStreamOptions } from '../llm-loop';
import { mergeAbortSignals } from '../llm-loop';
import { parseOpenAISseStream } from '../providers/groq-llm';
import { PatterConnectionError } from '../errors';
import { getLogger } from '../logger';
import { VERSION } from '../version';

/** Default per-request timeout in seconds for the generic provider. */
const DEFAULT_TIMEOUT_S = 60;

/** Constructor options for {@link OpenAICompatibleLLMProvider}. */
export interface OpenAICompatibleLLMOptions {
  /**
   * Bearer token. If omitted and ``apiKeyEnv`` is given, read from that
   * environment variable. May resolve to undefined for keyless local
   * gateways — the ``Authorization`` header is then omitted entirely.
   */
  apiKey?: string;
  /**
   * Environment variable to read the bearer from when ``apiKey`` is not given
   * (e.g. ``"OPENCLAW_API_KEY"``).
   */
  apiKeyEnv?: string;
  /**
   * OpenAI-compatible base URL ending in ``/v1`` — the whole point of this
   * provider, so it is **required**. Operator-controlled config, never derived
   * from caller / transcript input.
   */
  baseUrl: string;
  /** Model / agent target — **required**. */
  model: string;
  /**
   * Per-request timeout in **seconds**. Default ``60`` (the base OpenAI
   * provider hardcodes 30 s — raised here because agent runtimes run tools
   * before replying). Converted to ``AbortSignal.timeout(timeout * 1000)``.
   */
  timeout?: number;
  /**
   * Extra headers merged into the request *after* the ``User-Agent`` so the
   * SDK attribution is not silently clobbered (a caller can still override
   * ``User-Agent`` explicitly).
   */
  extraHeaders?: Record<string, string>;
  /**
   * When set, emits the OpenAI ``user`` field as
   * ``` `${sessionUserPrefix}${callId}` ``` for per-call session continuity.
   * ``undefined`` (default) means no ``user`` field is sent. Independent of the
   * session headers below.
   */
  sessionUserPrefix?: string;
  /**
   * Optional header NAME carrying a per-call session id, e.g.
   * ``"X-Hermes-Session-Id"`` or ``"x-openclaw-session-key"``. When set AND a
   * ``callId`` is available, the header VALUE is
   * ``` `${sessionIdPrefix}${callId}` ```. ``undefined`` (default) means off.
   */
  sessionIdHeader?: string;
  /**
   * Prefix for the session-id header VALUE. Defaults to ``""`` (raw call id).
   * Only meaningful when ``sessionIdHeader`` is set.
   */
  sessionIdPrefix?: string;
  /**
   * Optional STATIC header NAME for long-term memory scoping, e.g.
   * ``"X-Hermes-Session-Key"``. Emitted with the raw ``sessionKey`` value (no
   * call-id interpolation) only when BOTH ``sessionKeyHeader`` and
   * ``sessionKey`` are set. ``undefined`` (default) means off.
   */
  sessionKeyHeader?: string;
  /**
   * Static value emitted in ``sessionKeyHeader``. Credential-grade memory
   * scope — NEVER logged. ``undefined`` (default) means the header is omitted.
   */
  sessionKey?: string;
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
 * LLM provider for any OpenAI-compatible ``/chat/completions`` endpoint.
 *
 * Streams in the same ``{ type: "text" | "tool_call" | "usage" }`` chunk
 * format as the base OpenAI provider via the shared {@link parseOpenAISseStream}.
 */
export class OpenAICompatibleLLMProvider implements LLMProvider {
  /**
   * Stable pricing/dashboard key — read by stream-handler/metrics. Typed as
   * ``string`` (not the narrowed literal) so the Hermes / OpenClaw presets can
   * override it with their own key while still extending this class.
   */
  static readonly providerKey: string = 'openai_compatible';

  /** Resolved bearer; undefined for keyless gateways. */
  private readonly apiKey?: string;
  readonly model: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly extraHeaders?: Record<string, string>;
  private readonly sessionUserPrefix?: string;
  private readonly sessionIdHeader?: string;
  private readonly sessionIdPrefix?: string;
  private readonly sessionKeyHeader?: string;
  private readonly sessionKey?: string;
  private readonly temperature?: number;
  private readonly maxTokens?: number;
  private readonly responseFormat?: Record<string, unknown>;
  private readonly parallelToolCalls?: boolean;
  private readonly toolChoice?: string | Record<string, unknown>;
  private readonly seed?: number;
  private readonly topP?: number;
  private readonly frequencyPenalty?: number;
  private readonly presencePenalty?: number;
  private readonly stop?: string | string[];

  constructor(options: OpenAICompatibleLLMOptions) {
    if (!options.baseUrl) {
      throw new Error(
        'OpenAICompatibleLLMProvider requires a baseUrl (e.g. "http://127.0.0.1:11434/v1").',
      );
    }
    if (!options.model) {
      throw new Error('OpenAICompatibleLLMProvider requires a model.');
    }
    // Resolve the bearer: explicit apiKey wins, then apiKeyEnv, else undefined
    // (keyless local gateway). Never logged.
    this.apiKey =
      options.apiKey ??
      (options.apiKeyEnv ? process.env[options.apiKeyEnv] : undefined);
    this.model = options.model;
    this.baseUrl = options.baseUrl;
    this.timeoutMs = (options.timeout ?? DEFAULT_TIMEOUT_S) * 1000;
    this.extraHeaders = options.extraHeaders;
    this.sessionUserPrefix = options.sessionUserPrefix;
    this.sessionIdHeader = options.sessionIdHeader;
    this.sessionIdPrefix = options.sessionIdPrefix;
    this.sessionKeyHeader = options.sessionKeyHeader;
    this.sessionKey = options.sessionKey;
    this.temperature = options.temperature;
    this.maxTokens = options.maxTokens;
    this.responseFormat = options.responseFormat;
    this.parallelToolCalls = options.parallelToolCalls;
    this.toolChoice = options.toolChoice;
    this.seed = options.seed;
    this.topP = options.topP;
    this.frequencyPenalty = options.frequencyPenalty;
    this.presencePenalty = options.presencePenalty;
    this.stop = options.stop;
  }

  /**
   * Assemble the request headers. ``User-Agent`` is set first so any
   * ``extraHeaders`` (and the per-call session headers) layer on top without
   * silently dropping the SDK attribution, and the ``Authorization`` header is
   * only added when a key is present (keyless gateways omit it).
   *
   * The two session headers are emitted INDEPENDENTLY, each gated on its own
   * config (decoupled from ``sessionUserPrefix`` and from each other):
   *  - ``sessionIdHeader`` (+ ``callId``) → ``` `${sessionIdPrefix}${callId}` ```
   *  - ``sessionKeyHeader`` (+ ``sessionKey``) → the static ``sessionKey`` value.
   * ``sessionKey`` is a credential-grade memory scope and is never logged.
   */
  private buildHeaders(callId?: string): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'User-Agent': `getpatter/${VERSION}`,
      ...(this.extraHeaders ?? {}),
    };
    if (this.apiKey) {
      headers.Authorization = `Bearer ${this.apiKey}`;
    }
    if (this.sessionIdHeader && callId) {
      // Per-call session id for session / transcript continuity.
      headers[this.sessionIdHeader] = `${this.sessionIdPrefix ?? ''}${callId}`;
    }
    if (this.sessionKeyHeader && this.sessionKey) {
      // Truthy check (not `!== undefined`): an empty-string session key is not
      // a meaningful memory scope — treat it as unset rather than emitting a
      // confusing empty header. Value is the raw key (never logged).
      headers[this.sessionKeyHeader] = this.sessionKey;
    }
    return headers;
  }

  /**
   * Pre-call DNS / TLS warmup for the configured endpoint. Best-effort:
   * 5 s timeout, all exceptions swallowed at debug level. The ``Authorization``
   * header is only sent when a key is present so the operator-grade bearer is
   * never echoed for keyless gateways (and the key is never logged).
   */
  async warmup(): Promise<void> {
    try {
      const headers: Record<string, string> = {};
      if (this.apiKey) headers.Authorization = `Bearer ${this.apiKey}`;
      await fetch(`${this.baseUrl}/models`, {
        method: 'GET',
        headers,
        signal: AbortSignal.timeout(5_000),
      });
    } catch (err) {
      getLogger().debug(
        `OpenAI-compatible LLM warmup failed (best-effort): ${String(err)}`,
      );
    }
  }

  /**
   * Build the request body. Mirrors the base OpenAI provider's sampling-kwarg
   * assembly and additionally sets ``user`` for session continuity when
   * ``sessionUserPrefix`` is set AND a ``callId`` is available — so the default
   * (prefix unset) behaviour is byte-identical to the base provider.
   */
  private buildBody(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
    callId?: string,
  ): Record<string, unknown> {
    const body: Record<string, unknown> = {
      model: this.model,
      messages,
      stream: true,
      stream_options: { include_usage: true },
    };
    if (this.temperature !== undefined) body.temperature = this.temperature;
    if (this.maxTokens !== undefined) body.max_completion_tokens = this.maxTokens;
    if (this.responseFormat !== undefined) body.response_format = this.responseFormat;
    if (this.parallelToolCalls !== undefined) body.parallel_tool_calls = this.parallelToolCalls;
    if (this.toolChoice !== undefined) body.tool_choice = this.toolChoice;
    if (this.seed !== undefined) body.seed = this.seed;
    if (this.topP !== undefined) body.top_p = this.topP;
    if (this.frequencyPenalty !== undefined) body.frequency_penalty = this.frequencyPenalty;
    if (this.presencePenalty !== undefined) body.presence_penalty = this.presencePenalty;
    if (this.stop !== undefined) body.stop = this.stop;
    if (tools) body.tools = tools;
    if (this.sessionUserPrefix !== undefined && callId) {
      body.user = `${this.sessionUserPrefix}${callId}`;
    }
    return body;
  }

  /** Stream Patter-format LLM chunks from the configured chat completions API. */
  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
    opts?: LLMStreamOptions,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    const callId = opts?.callId;
    const body = this.buildBody(messages, tools, callId);

    const response = await fetch(`${this.baseUrl}/chat/completions`, {
      method: 'POST',
      headers: this.buildHeaders(callId),
      body: JSON.stringify(body),
      signal: mergeAbortSignals(opts?.signal, AbortSignal.timeout(this.timeoutMs)),
    });

    if (!response.ok) {
      const errText = await response.text();
      getLogger().error(
        `OpenAI-compatible API error: ${response.status} ${errText}`,
      );
      // Mirror the base OpenAILLMProvider.stream() — throw so LLMLoop can
      // surface the failure instead of silently producing an empty turn (the
      // agent would otherwise go silent with no error reaching the dashboard).
      throw new PatterConnectionError(
        `LLM API returned ${response.status}: ${errText.slice(0, 200)}`,
      );
    }

    yield* parseOpenAISseStream(response);
  }
}

/**
 * Public alias of {@link OpenAICompatibleLLMProvider} for the
 * ``getpatter/llm/openai-compatible`` namespace.
 *
 * @example
 * ```ts
 * import * as openaiCompatible from "getpatter/llm/openai-compatible";
 * // Ollama / vLLM / LM Studio (keyless local gateway):
 * const llm = new openaiCompatible.LLM({
 *   baseUrl: "http://127.0.0.1:11434/v1",
 *   model: "llama3.1",
 * });
 * ```
 */
export class LLM extends OpenAICompatibleLLMProvider {
  static readonly providerKey = 'openai_compatible';
}

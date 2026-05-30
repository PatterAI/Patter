/**
 * PatterTool — wrap a live Patter instance as a tool callable from external
 * agent frameworks (OpenAI Assistants, Anthropic Claude tool-use, LangChain,
 * Hermes Agent, MCP, generic OpenAI-compatible endpoints).
 *
 * Pattern this enables: a customer already runs an agent in their existing
 * stack (LangChain, OpenAI Assistant, Hermes Agent, …) and wants the agent
 * to *make phone calls* during a conversation. With this tool, the customer
 * registers `make_phone_call` and the agent's tool-call loop can dial out
 * via Patter, get a transcript + cost back, and continue reasoning.
 *
 * ## Design
 *
 * Each `PatterTool` wraps one `Patter` instance (carrier + agent + serve).
 * The tool exposes:
 *
 *   - `openaiSchema()`     — OpenAI / chat-completions tool spec
 *   - `anthropicSchema()`  — Anthropic Claude tool spec
 *   - `hermesSchema()`     — Hermes Agent / Nous registry schema (alias for
 *                            anthropicSchema; same JSON-Schema shape)
 *   - `execute(args)`      — dial outbound, await call end, return summary
 *   - `hermesHandler()`    — `(args, **kw) => Promise<string>` wrapper that
 *                            returns a JSON string and `{"error": "..."}` on
 *                            failure (matches Hermes' tool contract)
 *
 * ## Usage (OpenAI / Anthropic)
 *
 * ```ts
 * import { Patter, Twilio, DeepgramSTT, GroqLLM, ElevenLabsTTS } from 'getpatter';
 * import { PatterTool } from 'getpatter/integrations';
 *
 * const phone = new Patter({
 *   carrier: new Twilio(),
 *   phoneNumber: process.env.TWILIO_PHONE_NUMBER!,
 *   webhookUrl: 'agent.example.com',
 * });
 *
 * const tool = new PatterTool({
 *   phone,
 *   agent: { stt: new DeepgramSTT(), llm: new GroqLLM(), tts: new ElevenLabsTTS() },
 * });
 *
 * await tool.start();   // boots phone.serve() once
 *
 * // Register with your LLM
 * const tools = [tool.openaiSchema()];
 *
 * // When the LLM emits a tool_call:
 * const result = await tool.execute({
 *   to: '+15551234567',
 *   goal: 'Book a dentist appointment for next Tuesday afternoon.',
 * });
 * // → { call_id, status, duration_seconds, cost_usd, transcript, … }
 * ```
 *
 * ## Usage (Hermes Agent)
 *
 * Hermes' contract: handler takes `args: dict` + kwargs, returns a JSON
 * string. The TS SDK is meant to be invoked from Python via your own bridge
 * (HTTP, MCP, subprocess); this `hermesSchema()` + `hermesHandler()` pair
 * matches the Python adapter shipped under `getpatter.integrations` so the
 * two SDKs stay in lockstep.
 *
 * For pure-Python Hermes setups, use `PatterTool` from `getpatter.integrations`
 * directly inside a `tools/patter.py` module:
 *
 * ```python
 * from tools.registry import registry
 * from getpatter.integrations import PatterTool
 *
 * tool = PatterTool(phone=...)
 * tool.register_hermes(registry)
 * ```
 */

import type { Patter } from '../client';
import type { AgentOptions, CallResult } from '../types';

/** JSON-Schema of the call args. Identical wire shape across openai/anthropic/hermes. */
const PARAMETERS_SCHEMA = {
  type: 'object' as const,
  properties: {
    to: {
      type: 'string',
      description:
        'Destination phone number in E.164 format (e.g. "+15551234567"). Required.',
    },
    goal: {
      type: 'string',
      description:
        "What the agent should accomplish on the call. Becomes the in-call agent's system prompt for this single call.",
    },
    first_message: {
      type: 'string',
      description:
        'Optional first message the agent speaks when the callee answers. Defaults to a generic greeting.',
    },
    max_duration_sec: {
      type: 'integer',
      description:
        'Hard timeout for the call in seconds. Default 180. The call is force-ended at this deadline whether or not it has resolved.',
      minimum: 5,
      maximum: 1800,
    },
  },
  required: ['to'],
} as const;

const DEFAULT_NAME = 'make_phone_call';
const DEFAULT_DESCRIPTION =
  'Place a real outbound phone call. Returns a JSON object with the full transcript, ' +
  'call status, duration in seconds, and cost. Use this when the user asks you to call ' +
  'someone, schedule appointments by phone, or otherwise reach a human via voice.';

/** Constructor options for `PatterTool`. */
export interface PatterToolOptions {
  /**
   * Patter instance to dial through. Must be in local mode (have a `carrier`).
   * The tool boots `phone.serve()` on `start()`; do not call `serve()` yourself.
   */
  readonly phone: Patter;
  /**
   * Default agent config used for outbound calls. Per-call overrides come from
   * `execute({ goal, first_message })`.
   */
  readonly agent?: AgentOptions;
  /** Tool name shown to the LLM. Default `'make_phone_call'`. */
  readonly name?: string;
  /** Tool description for the LLM. Default tuned for English assistants. */
  readonly description?: string;
  /** Default per-call timeout in seconds. Default 180. */
  readonly maxDurationSec?: number;
  /**
   * Optional pass-through for `phone.serve()`'s `recording` flag — record all
   * outbound calls placed via this tool.
   */
  readonly recording?: boolean;
}

/** Args accepted by `PatterTool.execute()` (and the OpenAI/Anthropic/Hermes tool schemas). */
export interface PatterToolExecuteArgs {
  readonly to: string;
  readonly goal?: string;
  readonly first_message?: string;
  readonly max_duration_sec?: number;
}

/** Result envelope returned by `PatterTool.execute()` once the underlying call ends. */
export interface PatterToolResult {
  readonly call_id: string;
  readonly status: string;
  readonly duration_seconds: number;
  /**
   * Carrier-agnostic outcome (answered / voicemail / no_answer / busy /
   * failed) lifted from the SDK {@link CallResult}. Optional for backward
   * compatibility with any code constructing this envelope without it.
   */
  readonly outcome?: string;
  readonly cost_usd?: number;
  readonly transcript: ReadonlyArray<
    Readonly<{ role: string; text: string; timestamp?: number }>
  >;
  readonly metrics?: Readonly<Record<string, unknown>> | null;
}

/** Wraps a live `Patter` instance as a tool callable from external agent frameworks. */
export class PatterTool {
  readonly name: string;
  readonly description: string;
  private readonly phone: Patter;
  private readonly agent: AgentOptions | undefined;
  private readonly maxDurationSec: number;
  private readonly recording: boolean;
  private started = false;
  /** Cached in-progress (or completed) start promise so concurrent execute()
   *  callers all await the same boot sequence instead of each racing into
   *  phone.serve(). Reset to null on failure so callers can retry after a
   *  transient error. */
  private startPromise: Promise<void> | null = null;

  constructor(opts: PatterToolOptions) {
    if (!opts.phone) {
      throw new Error('PatterTool: `phone` (a Patter instance) is required.');
    }
    this.phone = opts.phone;
    this.agent = opts.agent;
    this.name = opts.name ?? DEFAULT_NAME;
    this.description = opts.description ?? DEFAULT_DESCRIPTION;
    this.maxDurationSec = Math.max(5, Math.min(1800, opts.maxDurationSec ?? 180));
    this.recording = opts.recording ?? false;
  }

  // --- Schema exporters ---------------------------------------------------

  /** OpenAI Chat Completions / Assistants tool spec. */
  openaiSchema(): {
    type: 'function';
    function: { name: string; description: string; parameters: typeof PARAMETERS_SCHEMA };
  } {
    return {
      type: 'function',
      function: {
        name: this.name,
        description: this.description,
        parameters: PARAMETERS_SCHEMA,
      },
    };
  }

  /** Anthropic Messages API tool spec. */
  anthropicSchema(): {
    name: string;
    description: string;
    input_schema: typeof PARAMETERS_SCHEMA;
  } {
    return {
      name: this.name,
      description: this.description,
      input_schema: PARAMETERS_SCHEMA,
    };
  }

  /**
   * Hermes Agent (Nous Research) registry schema. Same JSON-Schema shape as
   * Anthropic's; Hermes consumes it via `registry.register({ schema: ... })`.
   */
  hermesSchema(): {
    name: string;
    description: string;
    parameters: typeof PARAMETERS_SCHEMA;
  } {
    return {
      name: this.name,
      description: this.description,
      parameters: PARAMETERS_SCHEMA,
    };
  }

  // --- Lifecycle ----------------------------------------------------------

  /**
   * Start the underlying Patter server. Idempotent.
   *
   * `execute()` relies on `Patter.call({ wait: true })`, which requires an
   * active server to receive the carrier completion webhooks — that's what
   * `serve()` provides here. No `onCallEnd` callback is wired: the SDK's own
   * per-callId completion registry resolves the result, so the user's
   * `onCallEnd` slot is left free.
   *
   * Idempotent and concurrency-safe: concurrent callers all await the same
   * in-progress boot instead of each racing into `phone.serve()`.
   */
  async start(): Promise<void> {
    if (this.startPromise) return this.startPromise;
    this.startPromise = this._doStart();
    try {
      await this.startPromise;
    } catch (err) {
      // Allow retry after a transient failure.
      this.startPromise = null;
      throw err;
    }
  }

  private async _doStart(): Promise<void> {
    if (this.started) return;
    if (!this.agent) {
      throw new Error(
        'PatterTool.start: `agent` config is required. Pass `{ stt, llm, tts }` ' +
          'or an `engine` (e.g. OpenAIRealtime) when constructing PatterTool.',
      );
    }
    const builtAgent = this.phone.agent(this.agent);
    await this.phone.serve({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      agent: builtAgent as any,
      recording: this.recording,
    });
    this.started = true;
  }

  /** Best-effort shutdown — tear the Patter server down via `disconnect()`. */
  async stop(): Promise<void> {
    if (!this.started) return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const disconnectable = this.phone as unknown as { disconnect?: () => Promise<void> };
    if (typeof disconnectable.disconnect === 'function') {
      try {
        await disconnectable.disconnect();
      } catch {
        /* defensive — shutdown must not throw */
      }
    }
    this.started = false;
    this.startPromise = null;
  }

  // --- Execution ----------------------------------------------------------

  /**
   * Dial outbound, wait for the call to end, return a structured result.
   *
   * Thin wrapper over `Patter.call({ wait: true })`: the SDK now owns the
   * dial → callId → terminal-signal correlation, so this just bounds the wait
   * with `max_duration_sec` and maps the {@link CallResult} into the tool's
   * public envelope. Mirrors Python's `PatterTool.execute`.
   */
  async execute(args: PatterToolExecuteArgs): Promise<PatterToolResult> {
    if (!this.started) await this.start();
    if (!args || typeof args.to !== 'string' || !args.to.startsWith('+')) {
      throw new Error('PatterTool.execute: `to` must be an E.164 phone number (e.g. "+15551234567").');
    }
    const timeoutSec = Math.max(
      5,
      Math.min(1800, args.max_duration_sec ?? this.maxDurationSec),
    );

    const baseAgent = this.agent ?? ({} as AgentOptions);
    const overrideAgent = this.phone.agent({
      ...baseAgent,
      ...(args.goal !== undefined ? { systemPrompt: args.goal } : {}),
      ...(args.first_message !== undefined ? { firstMessage: args.first_message } : {}),
    });

    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        reject(
          new Error(
            `PatterTool.execute: call to ${args.to} exceeded ${timeoutSec}s timeout`,
          ),
        );
      }, timeoutSec * 1000);
      timer.unref?.();
    });

    let result: CallResult | void;
    try {
      result = await Promise.race([
        this.phone.call({
          to: args.to,
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          agent: overrideAgent as any,
          wait: true,
        }),
        timeout,
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }

    return resultFromCallResult(result);
  }

  /**
   * Hermes-style handler: `(args, kwargs) => Promise<string>` returning a JSON
   * string with either the result envelope or an `{"error": "..."}` payload.
   * Mirrors the Python `PatterTool.hermes_handler` so cross-SDK adapters share
   * the same wire contract.
   */
  hermesHandler(): (args: PatterToolExecuteArgs) => Promise<string> {
    return async (args: PatterToolExecuteArgs) => {
      try {
        const result = await this.execute(args);
        return JSON.stringify(result);
      } catch (err) {
        return JSON.stringify({ error: err instanceof Error ? err.message : String(err) });
      }
    };
  }
}

/**
 * Map an SDK {@link CallResult} into the tool's public envelope.
 *
 * Reads structured fields off the result directly — `cost.total` and
 * `durationSeconds` are real numbers here. `metrics` is passed through as a
 * plain object so JSON serialization for the Hermes/MCP wire envelope stays
 * clean. Mirrors Python's `_result_from_call_result`.
 */
function resultFromCallResult(result: CallResult | void): PatterToolResult {
  if (!result) {
    // call({ wait: true }) always resolves to a CallResult; this guard only
    // exists because the union type includes void. Treat the impossible case
    // as an empty completed result rather than throwing.
    return {
      call_id: '',
      status: 'completed',
      outcome: '',
      duration_seconds: 0,
      cost_usd: undefined,
      transcript: [],
      metrics: null,
    };
  }
  const costTotal = result.cost?.total;
  const costUsd = typeof costTotal === 'number' ? costTotal : undefined;
  const metrics = result.metrics
    ? (result.metrics as unknown as Record<string, unknown>)
    : null;
  return {
    call_id: result.callId || '',
    status: result.status || 'completed',
    outcome: result.outcome || '',
    duration_seconds:
      typeof result.durationSeconds === 'number' ? result.durationSeconds : 0,
    cost_usd: costUsd,
    transcript: result.transcript ? [...result.transcript] : [],
    metrics,
  };
}

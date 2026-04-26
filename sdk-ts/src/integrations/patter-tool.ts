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

import { EventEmitter } from 'node:events';
import type { Patter } from '../client';
import type { AgentOptions } from '../types';

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

export interface PatterToolOptions {
  /**
   * Patter instance to dial through. Must be in local mode (have a `carrier`).
   * The tool boots `phone.serve()` on `start()`; do not call `serve()` yourself.
   */
  phone: Patter;
  /**
   * Default agent config used for outbound calls. Per-call overrides come from
   * `execute({ goal, first_message })`.
   */
  agent?: AgentOptions;
  /** Tool name shown to the LLM. Default `'make_phone_call'`. */
  name?: string;
  /** Tool description for the LLM. Default tuned for English assistants. */
  description?: string;
  /** Default per-call timeout in seconds. Default 180. */
  maxDurationSec?: number;
  /**
   * Optional pass-through for `phone.serve()`'s `recording` flag — record all
   * outbound calls placed via this tool.
   */
  recording?: boolean;
}

export interface PatterToolExecuteArgs {
  to: string;
  goal?: string;
  first_message?: string;
  max_duration_sec?: number;
}

export interface PatterToolResult {
  call_id: string;
  status: string;
  duration_seconds: number;
  cost_usd?: number;
  transcript: Array<{ role: string; text: string; timestamp?: number }>;
  metrics?: Record<string, unknown> | null;
}

interface PendingCall {
  resolve: (r: PatterToolResult) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
  startedAt: number;
}

export class PatterTool {
  readonly name: string;
  readonly description: string;
  private readonly phone: Patter;
  private readonly agent: AgentOptions | undefined;
  private readonly maxDurationSec: number;
  private readonly recording: boolean;
  private started = false;
  /** Queue of pending dispatchers awaiting a call_id from the next `phone.call()`. */
  private pendingDial: ((callId: string) => void) | null = null;
  /** call_id → pending promise machinery. */
  private readonly pending = new Map<string, PendingCall>();
  private readonly bus = new EventEmitter();

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

  /** Start the underlying Patter server. Idempotent. */
  async start(): Promise<void> {
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
      onCallEnd: this.onCallEndHandler.bind(this),
    });

    // Subscribe to the metrics store so we can correlate outbound dials
    // (call_initiated) with the call_id Patter assigns at dial time.
    const store = this.phone.metricsStore;
    if (!store) {
      throw new Error(
        'PatterTool.start: phone.metricsStore is null after serve() — is the dashboard disabled?',
      );
    }
    store.on('sse', (event: { type: string; data: Record<string, unknown> }) => {
      if (event.type === 'call_initiated' && this.pendingDial) {
        const callId = (event.data.call_id as string) || '';
        if (callId) {
          const dispatch = this.pendingDial;
          this.pendingDial = null;
          dispatch(callId);
        }
      }
    });

    this.started = true;
  }

  /** Stop the underlying Patter server (and reject any pending calls). */
  async stop(): Promise<void> {
    if (!this.started) return;
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(new Error('PatterTool: shutdown while call pending'));
    }
    this.pending.clear();
    // Best-effort — Patter's `stop()` is on the embedded server; not all
    // versions expose a public stop on the Patter class.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const stoppable = this.phone as unknown as { stop?: () => Promise<void> };
    if (typeof stoppable.stop === 'function') {
      await stoppable.stop();
    }
    this.started = false;
  }

  // --- Execution ----------------------------------------------------------

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

    // Capture the call_id assigned by the metrics store when phone.call()
    // dispatches recordCallInitiated. Set the dispatcher BEFORE issuing the
    // dial to avoid the race window.
    const callIdPromise = new Promise<string>((resolve) => {
      this.pendingDial = resolve;
    });

    await this.phone.call({
      to: args.to,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      agent: overrideAgent as any,
    });

    const callId = await callIdPromise;

    return new Promise<PatterToolResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(callId);
        reject(new Error(`PatterTool.execute: call ${callId} exceeded ${timeoutSec}s timeout`));
      }, timeoutSec * 1000);
      this.pending.set(callId, {
        resolve,
        reject,
        timer,
        startedAt: Date.now() / 1000,
      });
    });
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

  // --- Internal: onCallEnd dispatcher -------------------------------------

  private async onCallEndHandler(data: Record<string, unknown>): Promise<void> {
    const callId = (data.call_id as string) || '';
    if (!callId) return;
    const pending = this.pending.get(callId);
    if (!pending) {
      this.bus.emit('orphan_end', { call_id: callId, data });
      return;
    }
    clearTimeout(pending.timer);
    this.pending.delete(callId);
    const metrics =
      data.metrics && typeof data.metrics === 'object'
        ? (data.metrics as Record<string, unknown>)
        : null;
    const cost =
      metrics &&
      typeof metrics.cost === 'object' &&
      metrics.cost &&
      typeof (metrics.cost as Record<string, unknown>).total === 'number'
        ? ((metrics.cost as Record<string, unknown>).total as number)
        : undefined;
    const duration =
      typeof (metrics?.duration_seconds as number | undefined) === 'number'
        ? (metrics?.duration_seconds as number)
        : Math.max(0, Date.now() / 1000 - pending.startedAt);
    const transcript = Array.isArray(data.transcript)
      ? (data.transcript as PatterToolResult['transcript'])
      : [];
    const status = (data.status as string) || 'completed';
    pending.resolve({
      call_id: callId,
      status,
      duration_seconds: duration,
      cost_usd: cost,
      transcript,
      metrics,
    });
  }
}

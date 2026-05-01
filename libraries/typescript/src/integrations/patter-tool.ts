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
import type { MetricsStore, SSEEvent } from '../dashboard/store';
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
  /** Resolver for the next `call_initiated` SSE event. Only set inside the
   *  dial mutex (`dialQueue`), so two parallel `execute()` calls never share
   *  it and never lose a dispatch. */
  private pendingDial: ((callId: string) => void) | null = null;
  /** Mutex that serializes the dial → call_id capture critical section.
   *  Each `execute()` chains a continuation onto this promise so the
   *  `pendingDial` slot is owned by exactly one caller at a time. */
  private dialQueue: Promise<void> = Promise.resolve();
  /** Captured SSE listener so `stop()` can detach it (prevents leaks when
   *  the underlying Patter instance outlives this tool). */
  private sseListener: ((event: SSEEvent) => void) | null = null;
  /** Captured Patter metrics store, for cleanup in `stop()`. */
  private metricsStoreRef: MetricsStore | null = null;
  /** call_id → pending promise machinery. */
  private readonly pending = new Map<string, PendingCall>();
  private readonly bus = new EventEmitter();
  /** How long to wait for the `call_initiated` SSE before failing the dial. */
  private static readonly DIAL_CAPTURE_TIMEOUT_MS = 10_000;

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
    const listener = (event: SSEEvent) => {
      if (event.type === 'call_initiated' && this.pendingDial) {
        const callId = (event.data.call_id as string) || '';
        if (callId) {
          const dispatch = this.pendingDial;
          this.pendingDial = null;
          dispatch(callId);
        }
      }
    };
    store.on('sse', listener);
    this.sseListener = listener;
    this.metricsStoreRef = store;

    this.started = true;
  }

  /** Stop the underlying Patter server (and reject any pending calls). */
  async stop(): Promise<void> {
    if (!this.started) return;
    // Detach the SSE listener so a long-lived `Patter` instance shared with
    // other consumers doesn't accumulate dead listeners every time a tool is
    // stopped/restarted.
    if (this.metricsStoreRef && this.sseListener) {
      this.metricsStoreRef.off('sse', this.sseListener);
    }
    this.sseListener = null;
    this.metricsStoreRef = null;
    // Drop any in-flight dial waiter (silently — caller will get the
    // shutdown rejection from `pending` below if it ever set its waiter).
    this.pendingDial = null;
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

    // Serialize the dial → call_id capture across concurrent execute() calls.
    // `pendingDial` is a single slot, so two parallel callers would clobber
    // each other's resolver and one of them would hang forever waiting for
    // an SSE event that already went to the other. Chain through dialQueue
    // so exactly one execute() owns the slot at a time. Once the call_id is
    // captured we release the queue immediately — the actual call_end can
    // run concurrently with later dials.
    const callId = await this.acquireCallId(args.to, overrideAgent);

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

  /** Issue the outbound dial under the mutex and return its assigned call_id. */
  private async acquireCallId(to: string, agent: AgentOptions): Promise<string> {
    // Chain on dialQueue. Each call replaces dialQueue with a tail promise so
    // the *next* execute() can also enqueue. We resolve the queue promise as
    // soon as we capture the call_id (or fail) so we don't hold the slot for
    // the call's full duration.
    let release!: () => void;
    const slot = new Promise<void>((r) => {
      release = r;
    });
    const previous = this.dialQueue;
    this.dialQueue = previous.then(() => slot);
    await previous;

    // We now own the slot. Set up the dispatcher, dial, and wait for the
    // call_initiated SSE — bounded by DIAL_CAPTURE_TIMEOUT_MS so a missed
    // event doesn't hang the caller forever.
    let captureTimer: NodeJS.Timeout | null = null;
    try {
      const callIdPromise = new Promise<string>((resolve, reject) => {
        this.pendingDial = resolve;
        captureTimer = setTimeout(() => {
          this.pendingDial = null;
          reject(
            new Error(
              `PatterTool.execute: did not observe call_initiated within ${PatterTool.DIAL_CAPTURE_TIMEOUT_MS}ms`,
            ),
          );
        }, PatterTool.DIAL_CAPTURE_TIMEOUT_MS);
      });

      await this.phone.call({
        to,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        agent: agent as any,
      });

      const callId = await callIdPromise;
      if (captureTimer) clearTimeout(captureTimer);
      return callId;
    } finally {
      // Always clear pendingDial; release the mutex so the next dial can run.
      if (captureTimer) clearTimeout(captureTimer);
      this.pendingDial = null;
      release();
    }
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

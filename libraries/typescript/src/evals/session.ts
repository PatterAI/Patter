/**
 * Eval session — drives the REAL pipeline call loop, no telephony required.
 *
 * Unlike the legacy ``reply()``-callable path in `runner.ts` (which never
 * touches the SDK), {@link EvalSession} constructs a real
 * {@link StreamHandler} in pipeline mode and injects user turns through the
 * exact same path a live phone call uses:
 *
 * ``FakeSTT → onTranscript → handleBargeIn → commitTranscript →
 * dispatchTurn → LLMLoop (real DefaultToolExecutor, hooks, guardrails) →
 * SentenceChunker → FakeTTS → FakeAudioSender``
 *
 * so tool calling, pipeline hooks, guardrail replacement,
 * dedup/hallucination filtering, history handling, metrics, and the
 * turn-taking state machine are all exercised for real. Only the
 * paid/external boundary is faked: the telephony bridge (audio sender), the
 * STT socket, the TTS socket, and — optionally — the LLM (a deterministic
 * {@link ScriptedLLMProvider} for CI, or any real {@link LLMProvider} for
 * live evals). See `fakes.ts` / `scripted-llm.ts`.
 *
 * Lifecycle (TS idiom — no async context managers in JS):
 *
 * ```ts
 * const session = await EvalSession.create({ agent, llmProvider });
 * try {
 *   const result = await session.userSays('where is my order?');
 *   expect(result)
 *     .toolCalled('lookup_order', { orderId: 'A1' })
 *     .agentTextContains('tomorrow');
 * } finally {
 *   await session.close();
 * }
 * ```
 *
 * ``EvalSession.create()`` = ``new EvalSession(options)`` + ``await
 * session.start()``; ``close()`` runs the handler's real teardown
 * (``handleStop``) and is idempotent. Always pair ``create`` with a
 * ``finally { await session.close() }`` so fakes and timers are released
 * even when an assertion throws.
 *
 * Notes
 * -----
 * - ``agent.stt`` / ``agent.tts`` configs are ignored (replaced by the
 *   fakes); ``agent.provider`` is forced to ``'pipeline'``.
 * - ``onMessage``-style agents are not supported — the session targets the
 *   built-in {@link LLMLoop} path.
 * - ``TurnResult.agentText`` is what the caller HEARD (post-guardrail,
 *   post-hook, post-text-transform sentences handed to TTS).
 *   ``historySnapshot`` mirrors the dashboard conversation history, where
 *   the streaming path records the raw LLM text.
 */

import { randomUUID } from 'node:crypto';

import { StreamHandler } from '../stream-handler';
import type { StreamHandlerDeps } from '../stream-handler';
import { MetricsStore } from '../dashboard/store';
import { RemoteMessageHandler } from '../remote-message';
import { sanitizeVariables, resolveVariables } from '../server';
import type { AgentOptions } from '../types';
import type { LLMProvider } from '../llm-loop';
import type { TurnMetrics } from '../metrics';
import type { HistoryEntry } from '../handler-utils';
import { ErrorCode, PatterConfigError, PatterError } from '../errors';
import { getLogger } from '../logger';
import type { TranscriptEntry } from './case';
import { FakeAudioSender, FakeSTT, FakeTTS, NoopVad, makeFakeCarrierWs } from './fakes';

// ---------------------------------------------------------------------------
// Result shapes
// ---------------------------------------------------------------------------

/**
 * One tool invocation observed through the handler's tool-event path.
 *
 * ``args`` is the parsed-JSON object the {@link LLMLoop} handed to the real
 * ``DefaultToolExecutor``; ``result`` is the executor's string return value
 * (``null`` only for hand-built records). Caller-immutable by convention.
 */
export interface ToolCallRecord {
  readonly name: string;
  readonly arguments: Readonly<Record<string, unknown>>;
  readonly result: string | null;
}

/**
 * The observable outcome of one {@link EvalSession.userSays} turn.
 *
 * - ``userText`` — the injected user utterance.
 * - ``agentText`` — what the caller heard this turn: the sentences handed to
 *   TTS after guardrails / hooks / text transforms, joined by a single
 *   space. Falls back to the assistant history entry when no TTS sentence
 *   was produced (e.g. the turn was cut short before synthesis).
 * - ``toolCalls`` — tool invocations recorded via the handler's tool-event
 *   path (``LLMLoop`` ``onToolCall`` → ``recordToolCall``), in execution
 *   order.
 * - ``historySnapshot`` — the handler's full conversation history right
 *   after the turn settled (``role`` / ``text`` / ``timestamp`` entries,
 *   including ``role: 'tool'`` timeline entries).
 * - ``interrupted`` — true when the turn was cut short (barge-in cancel,
 *   hook veto, or an LLM error that interrupted the turn) — derived from
 *   the handler's ``turn_ended`` events carrying ``[interrupted]``.
 * - ``metricsTurn`` — the {@link TurnMetrics} emitted for this turn via the
 *   handler's ``onMetrics`` callback, or ``null`` when the turn did not
 *   complete normally (vetoed / interrupted).
 */
export interface TurnResult {
  readonly userText: string;
  readonly agentText: string;
  readonly toolCalls: ReadonlyArray<ToolCallRecord>;
  readonly historySnapshot: ReadonlyArray<HistoryEntry>;
  readonly interrupted: boolean;
  readonly metricsTurn: TurnMetrics | null;
}

/**
 * Render a conversation history in the judge transcript shape
 * (``[{ role: 'user'|'agent'|'tool', text }]`` — assistant → agent).
 * Shared by {@link EvalSession.transcript} and the assertion ``judge``.
 */
export function historyTranscript(
  history: ReadonlyArray<{ readonly role: string; readonly text: string }>,
): TranscriptEntry[] {
  return history.map((entry) => ({
    role: entry.role === 'assistant' ? 'agent' : String(entry.role ?? 'user'),
    text: String(entry.text ?? ''),
  }));
}

// ---------------------------------------------------------------------------
// EvalSession
// ---------------------------------------------------------------------------

/**
 * The handler internals the harness reads/wires. The same private surface
 * the SDK's own unit suites exercise via casts — the harness observes the
 * REAL handler, it never replaces any of it.
 */
interface HandlerInternals {
  llmLoop: {
    setOnToolCall(
      cb?: (name: string, args: Record<string, unknown>, result: string) => Promise<void>,
    ): void;
  } | null;
  dispatchTask: Promise<void> | null;
  firstMessageTask: Promise<void> | null;
  lastCommitText: string;
  lastCommitAt: number;
  llmAbort: AbortController | null;
  history: { entries: HistoryEntry[] };
  recordToolCall(
    name: string,
    args: Record<string, unknown>,
    result: string,
  ): Promise<void>;
}

/** Options for {@link EvalSession}. Defaults match the Python SDK. */
export interface EvalSessionOptions {
  /**
   * The agent under test. Its ``stt`` / ``tts`` configs are replaced by
   * fakes and ``provider`` is forced to ``'pipeline'``; everything else
   * (tools, guardrails, hooks, text transforms, firstMessage, variables,
   * ...) is live.
   */
  readonly agent: AgentOptions;
  /**
   * Optional LLM provider override. Defaults to ``agent.llm``. Pass a
   * {@link ScriptedLLMProvider} for deterministic CI evals or a real
   * provider for live evals.
   */
  readonly llmProvider?: LLMProvider;
  /**
   * Legacy fallback — when neither ``llmProvider`` nor ``agent.llm`` is
   * set, the built-in OpenAI provider is built from this key (live evals
   * only; never use in CI).
   */
  readonly openaiKey?: string;
  /** Call identity threaded through the handler, tools' call context, and metrics. */
  readonly callId?: string;
  readonly caller?: string;
  readonly callee?: string;
  /**
   * Optional per-call variables resolved into the system prompt exactly
   * like ``phone.call({ customParams })``.
   */
  readonly customParams?: Readonly<Record<string, string>>;
  /**
   * Per-turn ceiling in seconds for {@link EvalSession.userSays} (LLM +
   * tools + TTS). Generous default (60) for live providers; scripted
   * providers finish in milliseconds.
   */
  readonly turnTimeoutS?: number;
}

/** Race ``promise`` against a timeout; the loser's timer is always cleared. */
async function awaitWithTimeout(
  promise: Promise<unknown>,
  timeoutMs: number,
): Promise<'ok' | 'timeout'> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise.then(() => 'ok' as const),
      new Promise<'timeout'>((resolve) => {
        timer = setTimeout(() => resolve('timeout'), timeoutMs);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/**
 * Harness around a real pipeline-mode {@link StreamHandler}.
 *
 * See the module docstring for usage. Construction is cheap; the handler is
 * built and started in {@link EvalSession.start} (``EvalSession.create``
 * calls it).
 */
export class EvalSession {
  readonly callId: string;
  readonly caller: string;
  readonly callee: string;

  /** The faked carrier boundary — records audio / clears / marks. */
  readonly audioSender = new FakeAudioSender();
  /** The faked STT boundary — inject transcripts via the session, not directly. */
  readonly stt = new FakeSTT();
  /** The faked TTS boundary — ``spoken`` records every synthesised sentence. */
  readonly tts = new FakeTTS();
  /**
   * Every payload the handler fired through ``onTranscript`` — user /
   * assistant / tool events, in emission order.
   */
  readonly transcriptEvents: Array<Record<string, unknown>> = [];

  private readonly sourceAgent: AgentOptions;
  private readonly llmProvider?: LLMProvider;
  private readonly openaiKey: string;
  private readonly customParams?: Readonly<Record<string, string>>;
  private readonly turnTimeoutS: number;

  private streamHandler: StreamHandler | null = null;
  private readonly toolCalls: ToolCallRecord[] = [];
  private readonly metricsTurns: TurnMetrics[] = [];
  private interruptedTurns = 0;
  private offTurnEnded: (() => void) | null = null;
  private started = false;
  private closed = false;

  constructor(options: EvalSessionOptions) {
    if (!options || !options.agent) {
      throw new PatterConfigError('EvalSession requires an agent');
    }
    this.sourceAgent = options.agent;
    this.llmProvider = options.llmProvider;
    this.openaiKey = options.openaiKey ?? '';
    this.callId = options.callId ?? `eval_${randomUUID().replace(/-/g, '').slice(0, 12)}`;
    this.caller = options.caller ?? '+15550000001';
    this.callee = options.callee ?? '+15550000002';
    this.customParams = options.customParams;
    this.turnTimeoutS = options.turnTimeoutS ?? 60.0;
  }

  /** Build AND start a session — the canonical entry point. */
  static async create(options: EvalSessionOptions): Promise<EvalSession> {
    const session = new EvalSession(options);
    await session.start();
    return session;
  }

  // -- lifecycle ------------------------------------------------------------

  /** Build and start the real handler (idempotent). */
  async start(): Promise<void> {
    if (this.started) return;

    const evalAgent = this.buildEvalAgent();
    if (evalAgent.llm == null && !this.openaiKey) {
      throw new PatterConfigError(
        'EvalSession needs an LLM: pass llmProvider (e.g. a ' +
          'ScriptedLLMProvider for CI), set agent.llm, or supply ' +
          'openaiKey for the built-in OpenAI provider.',
      );
    }

    this.audioSender.attachSttFactory(() => this.stt);
    const deps: StreamHandlerDeps = {
      config: this.openaiKey ? { openaiKey: this.openaiKey } : {},
      agent: evalAgent,
      bridge: this.audioSender,
      metricsStore: new MetricsStore(),
      pricing: null,
      remoteHandler: new RemoteMessageHandler(),
      recording: false,
      onTranscript: async (data) => {
        this.transcriptEvents.push(data);
      },
      onMetrics: async (data) => {
        const turn = data.turn as TurnMetrics | undefined;
        if (turn != null) this.metricsTurns.push(turn);
      },
      // Pipeline mode never builds a Realtime/ConvAI adapter; loud guard in
      // case a future refactor routes the eval agent elsewhere.
      buildAIAdapter: () => {
        throw new PatterConfigError('EvalSession supports pipeline mode only');
      },
      sanitizeVariables,
      resolveVariables,
    };

    const handler = new StreamHandler(deps, makeFakeCarrierWs(), this.caller, this.callee);
    const internals = handler as unknown as HandlerInternals;
    this.audioSender.attachHandler(handler);
    handler.setStreamSid('eval');
    // Interruption observability: the accumulator emits ``turn_ended`` with
    // ``agent_text: '[interrupted]'`` whenever a turn is cut short (barge-in
    // cancel, hook veto, LLM error) — the public analogue of Python's
    // ``_last_response_interrupted`` flag.
    this.offTurnEnded = handler.addObserver<{ turn?: TurnMetrics }>((payload) => {
      if (payload?.turn?.agent_text === '[interrupted]') this.interruptedTurns += 1;
    }, 'turn_ended');

    this.streamHandler = handler;
    try {
      // The REAL start path: initPipeline connects the FakeSTT, plays
      // ``firstMessage`` through the FakeTTS, builds the real LLMLoop +
      // DefaultToolExecutor, and installs the transcript handler.
      await handler.handleCallStart(this.callId, { ...(this.customParams ?? {}) });
      // The greeting runs as a tracked background task on the live path;
      // settle it here so ``tts.spoken`` / history are stable when the
      // first userSays lands (parity with the Python harness, where
      // ``start()`` returns after the greeting played).
      if (internals.firstMessageTask) await internals.firstMessageTask;
      if (internals.llmLoop == null) {
        throw new PatterConfigError(
          'EvalSession could not build the LLM loop — the agent has ' +
            'onMessage-style wiring or no usable LLM provider.',
        );
      }
      // Observe tool executions through the handler's tool-event path:
      // keep the handler's own transcript-timeline behaviour (role='tool'
      // history entries + onTranscript events) and additionally record
      // structured ToolCallRecords for assertions.
      internals.llmLoop.setOnToolCall(async (name, args, result) => {
        await internals.recordToolCall(name, args, result);
        this.toolCalls.push({ name, arguments: { ...(args ?? {}) }, result });
      });
    } catch (err) {
      // ``create()`` callers never reach their ``finally`` when start
      // throws — tear down here.
      try {
        await handler.handleStop();
      } catch {
        /* best-effort teardown */
      }
      this.offTurnEnded?.();
      this.offTurnEnded = null;
      this.streamHandler = null;
      throw err;
    }
    this.started = true;
  }

  /** Run the handler's real teardown (``handleStop``). Idempotent. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.offTurnEnded?.();
    this.offTurnEnded = null;
    const handler = this.streamHandler;
    if (handler === null) return;
    await handler.handleStop();
  }

  // -- turn driving -----------------------------------------------------------

  /**
   * Inject one final user transcript and await the full agent turn.
   *
   * The transcript flows through the handler's real receive path — barge-in
   * handling, dedup/hallucination filtering, hooks, the LLM loop with real
   * tool execution, guardrails, sentence chunking, and TTS — exactly as on
   * a live call. Returns once the turn's dispatch task settles.
   *
   * Throws:
   * - ``PatterError`` (``INPUT_VALIDATION``) — the REAL pipeline dropped
   *   the transcript (duplicate within the 2 s throttle window, known STT
   *   hallucination, or empty text) — same behaviour as a live call.
   * - ``PatterError`` (``TIMEOUT``) — the turn did not settle within
   *   ``timeoutS`` (default: the session's ``turnTimeoutS``).
   */
  async userSays(text: string, options: { readonly timeoutS?: number } = {}): Promise<TurnResult> {
    if (!this.started || this.streamHandler === null) {
      throw new PatterError(
        'EvalSession is not started — use `await EvalSession.create(...)`',
        { code: ErrorCode.CONFIG },
      );
    }
    if (this.closed) {
      throw new PatterError('EvalSession is closed', { code: ErrorCode.CONFIG });
    }
    const timeoutS = options.timeoutS ?? this.turnTimeoutS;
    const timeoutMs = timeoutS * 1000;
    const internals = this.streamHandler as unknown as HandlerInternals;

    // Per-turn snapshots — everything new after the turn belongs to it.
    const historyLen = internals.history.entries.length;
    const toolsLen = this.toolCalls.length;
    const spokenLen = this.tts.spoken.length;
    const turnsLen = this.metricsTurns.length;
    const interruptedLen = this.interruptedTurns;
    // Commit detection: ``commitTranscript`` stamps these exactly when a
    // final transcript is accepted (the dispatch promise nulls itself on
    // settle, so it cannot serve as the commit signal).
    const commitTextBefore = internals.lastCommitText;
    const commitAtBefore = internals.lastCommitAt;

    // ``pushFinal`` resolves once the handler's transcript drain loop has
    // fully processed this transcript (dispatch-task creation included).
    const consumed = await awaitWithTimeout(this.stt.pushFinal(text), timeoutMs);
    if (consumed === 'timeout') {
      throw new PatterError(
        `user turn ${JSON.stringify(text)} was not consumed by the STT ` +
          `transcript loop within ${timeoutS}s — a hook or the previous ` +
          'turn may be hung (check logs)',
        { code: ErrorCode.TIMEOUT },
      );
    }

    const committed =
      internals.lastCommitText !== commitTextBefore ||
      internals.lastCommitAt !== commitAtBefore;
    if (!committed) {
      throw new PatterError(
        `user turn ${JSON.stringify(text)} was dropped by the pipeline's ` +
          'commit filter (duplicate within 2s, known STT hallucination, or ' +
          'empty) — exactly as it would be on a live call. Vary the text ' +
          'between turns or split the case.',
        { code: ErrorCode.INPUT_VALIDATION },
      );
    }

    // The dispatch promise nulls itself when the turn settles — a null here
    // means the turn already finished (or was vetoed before dispatch).
    const dispatch = internals.dispatchTask;
    if (dispatch !== null) {
      const settled = await awaitWithTimeout(dispatch, timeoutMs);
      if (settled === 'timeout') {
        // Abort the in-flight LLM stream so the hung turn unwinds instead
        // of leaking into the next one (JS promises are not cancellable).
        try {
          internals.llmAbort?.abort();
        } catch {
          /* defensive */
        }
        // Await the aborted task's unwinding (bounded) before throwing so a
        // following close() cannot race a still-running dispatch through the
        // fakes' teardown — mirrors Python's cancel-then-await.
        await Promise.race([
          dispatch.catch(() => undefined),
          new Promise<void>((resolve) => setTimeout(resolve, 1_000)),
        ]);
        throw new PatterError(
          `agent turn for ${JSON.stringify(text)} did not settle within ${timeoutS}s`,
          { code: ErrorCode.TIMEOUT },
        );
      }
    }

    return this.buildTurnResult(text, { historyLen, toolsLen, spokenLen, turnsLen, interruptedLen });
  }

  // -- convenience accessors ----------------------------------------------------

  /** The live {@link StreamHandler} (``null`` before ``start``). */
  get handler(): StreamHandler | null {
    return this.streamHandler;
  }

  /** Current conversation history (shallow copies of the entries). */
  get history(): HistoryEntry[] {
    if (this.streamHandler === null) return [];
    const internals = this.streamHandler as unknown as HandlerInternals;
    return internals.history.entries.map((e) => ({ ...e }));
  }

  /** Full-session transcript in the judge shape (assistant → agent). */
  transcript(): TranscriptEntry[] {
    return historyTranscript(this.history);
  }

  // -- internals --------------------------------------------------------------

  /** Return the agent reshaped for harness execution. */
  private buildEvalAgent(): AgentOptions {
    const agent = this.sourceAgent;
    const overrides: Record<string, unknown> = {};
    // Unlike Python (whose handler is pipeline-only), the TS handler
    // defaults to realtime when ``provider`` is unset — force pipeline.
    if (agent.provider !== 'pipeline') {
      overrides.provider = 'pipeline';
    }
    if (agent.engine !== undefined) {
      getLogger().info('EvalSession: agent.engine ignored — the harness runs pipeline mode');
      overrides.engine = undefined;
    }
    if (agent.stt !== undefined) {
      getLogger().info(
        'EvalSession: agent.stt config ignored — transcripts are injected directly',
      );
    }
    // The real init path reads STT from the bridge (FakeSTT) and TTS from
    // ``agent.tts`` — install the fake there.
    overrides.stt = undefined;
    if (agent.tts !== undefined) {
      getLogger().info('EvalSession: agent.tts config replaced by FakeTTS');
    }
    overrides.tts = this.tts;
    if (this.llmProvider !== undefined) {
      overrides.llm = this.llmProvider;
    }
    // Without this, the real start path would try to auto-load SileroVAD
    // (ONNX model) — pointless here since the session feeds no audio frames.
    if (agent.vad === undefined) {
      overrides.vad = new NoopVad();
    }
    return { ...agent, ...overrides } as AgentOptions;
  }

  private buildTurnResult(
    userText: string,
    snapshot: {
      readonly historyLen: number;
      readonly toolsLen: number;
      readonly spokenLen: number;
      readonly turnsLen: number;
      readonly interruptedLen: number;
    },
  ): TurnResult {
    const internals = this.streamHandler as unknown as HandlerInternals;
    const spoken = this.tts.spoken.slice(snapshot.spokenLen);
    const fullHistory = internals.history.entries.map((e) => ({ ...e }));
    const newHistory = fullHistory.slice(snapshot.historyLen);
    // ``agentText`` is what the caller HEARD; when no TTS output was
    // produced this turn (cut short pre-synthesis / empty reply) fall back
    // to the assistant history entry, if any.
    const agentText =
      spoken.length > 0
        ? spoken.join(' ')
        : newHistory
            .filter((e) => e.role === 'assistant')
            .map((e) => e.text)
            .join(' ');
    const newTurns = this.metricsTurns.slice(snapshot.turnsLen);
    return {
      userText,
      agentText,
      toolCalls: this.toolCalls.slice(snapshot.toolsLen),
      historySnapshot: fullHistory,
      interrupted: this.interruptedTurns > snapshot.interruptedLen,
      metricsTurn: newTurns.length > 0 ? newTurns[newTurns.length - 1] : null,
    };
  }
}

/**
 * Patter — local-mode SDK client.
 *
 * The SDK runs in a single mode: locally on your own infrastructure.  You
 * bring a telephony carrier (``Twilio`` or ``Telnyx``) and Patter wires up the
 * media plane, agent loop, and webhook server in your process.
 *
 * ```ts
 * import { Patter, Twilio, OpenAIRealtime } from "getpatter";
 *
 * const phone = new Patter({
 *   carrier: new Twilio(),
 *   phoneNumber: "+15551234567",
 *   tunnel: true,
 * });
 *
 * await phone.serve({
 *   agent: phone.agent({
 *     engine: new OpenAIRealtime(),
 *     systemPrompt: "You are a helpful receptionist.",
 *   }),
 * });
 * ```
 *
 * Patter Cloud (a hosted backend that previously powered ``apiKey``-based
 * usage) is not part of this release.  Cloud mode will return in a future
 * release; until then, passing ``apiKey`` raises a clear error.
 */
import { ProvisionError, PatterConnectionError, ErrorCode } from "./errors";
import type { TunnelHandle } from "./tunnel";
import type {
  LocalOptions,
  LocalCallOptions,
  AgentOptions,
  ServeOptions,
  CarrierKind,
  CallResult,
} from "./types";
import { EmbeddedServer } from "./server";
import type { MetricsStore } from "./dashboard/store";
import { Carrier as TwilioCarrier } from "./telephony/twilio";
import { Carrier as TelnyxCarrier } from "./telephony/telnyx";
import { Carrier as PlivoCarrier } from "./telephony/plivo";
import { Realtime as OpenAIRealtime } from "./engines/openai";
import { Realtime2 as OpenAIRealtime2 } from "./engines/openai-2";
import { ConvAI as ElevenLabsConvAI } from "./engines/elevenlabs";
import { CloudflareTunnel, Static as StaticTunnel } from "./tunnels";
import { resolveLogRoot } from "./services/call-log";
import { validateAllToolSchemas } from "./tools/schema-validation";
import type { ToolDefinition } from "./types";
import { getLogger } from "./logger";
import { SpeechEvents } from "./_speech-events";
import type {
  ConversationStateSnapshot,
  SpeechEventCallback,
} from "./_speech-events";

/**
 * Maximum concurrent entries in the prewarm-first-message cache. Bounds
 * memory consumption when an outbound flood (or attacker-controlled
 * ``Patter.call`` invocations) would otherwise pile up tens of MB of
 * orphan TTS bytes that never evict because the carrier never fires
 * ``start``. When the cap is reached, new prewarm spawns are refused
 * (logged at warn, call still proceeds with live TTS). See FIX #96 in
 * the parity audit. Mirrors ``_PREWARM_CACHE_MAX`` in the Python client.
 */
export const PREWARM_CACHE_MAX = 200;

/**
 * Extra grace window beyond ``ringTimeout`` after which a prewarmed
 * entry that was never consumed is forcibly evicted. The TTS bill was
 * paid; without TTL eviction a carrier that never fires ``start`` (e.g.
 * on a never-completed dial that bypassed the status callback) would
 * leak the bytes for the lifetime of the Patter instance.
 */
export const PREWARM_TTL_GRACE_MS = 5_000;

/**
 * Safety TTL (ms) after which a parked provider WebSocket whose
 * carrier never fired ``start`` is force-closed. 30 s is a comfortable
 * superset of typical ring + AMD windows (Twilio ~25 s, Telnyx ~25 s).
 */
const PARKED_CONN_TTL_MS = 30_000;

/** Parked provider WebSockets ready for adoption by a per-call StreamHandler. */
export interface ParkedProviderConnections {
  /** Pre-opened STT WS (Cartesia today; other adapters may add support later). */
  stt?: import('ws').WebSocket;
  /**
   * Pre-opened TTS WS handle (ElevenLabs WS today). The `bosSent` flag
   * lets the live `synthesizeStream` skip its own BOS send when the
   * prewarm pipeline already wrote it.
   */
  tts?: import('./providers/elevenlabs-ws-tts').ElevenLabsParkedWS;
  /** Pre-opened OpenAI Realtime WS (already through `session.updated`). */
  openaiRealtime?: import('ws').WebSocket;
}

/** Internal local-mode state — holds carrier + resolved runtime settings. */
export interface ResolvedLocalConfig {
  carrier: TwilioCarrier | TelnyxCarrier | PlivoCarrier;
  phoneNumber: string;
  webhookUrl?: string;
  tunnel?: CloudflareTunnel | StaticTunnel | boolean;
  openaiKey?: string;
  /**
   * Resolved on-disk persistence root for the dashboard's call history.
   * ``null`` means persistence is disabled. Computed once at constructor
   * time from the ``persist`` option + ``PATTER_LOG_DIR`` env var. See
   * ``LocalOptions.persist`` for the resolution rules.
   */
  persistRoot: string | null;
}

/**
 * Resolve the user-supplied ``persist`` option into a concrete
 * filesystem path or ``null``. Layered precedence:
 *
 *  - ``persist === false`` → ``null`` (force off, even if env var is set)
 *  - ``persist === true`` → platform default (``resolveLogRoot('auto')``)
 *  - ``persist`` is a string → exactly that path (after ``~`` expansion)
 *  - ``persist === undefined`` → fall back to ``PATTER_LOG_DIR`` env var,
 *    or ``null`` if the env is also unset (preserves the prior opt-in
 *    behaviour where persistence required setting the env explicitly)
 */
function resolvePersistRoot(persist: boolean | string | undefined): string | null {
  if (persist === false) return null;
  if (persist === true) return resolveLogRoot('auto');
  if (typeof persist === 'string') return resolveLogRoot(persist);
  // Restore opt-in semantics: when `persist` is omitted and PATTER_LOG_DIR
  // is not set, return null (no disk writes). This preserves the documented
  // backward-compatible default in LocalOptions.persist JSDoc.
  const envRoot = resolveLogRoot();
  if (envRoot !== null) return envRoot;
  return null;
}

/** Close every parked socket inside a ``ParkedProviderConnections`` slot. */
function closeParkedConnections(slot: ParkedProviderConnections): void {
  if (slot.stt) { try { slot.stt.close(); } catch { /* ignore */ } }
  if (slot.tts) { try { slot.tts.ws.close(); } catch { /* ignore */ } }
  if (slot.openaiRealtime) {
    const wsAny = slot.openaiRealtime as unknown as { _parkedKeepalive?: NodeJS.Timeout };
    if (wsAny._parkedKeepalive) {
      clearInterval(wsAny._parkedKeepalive);
      delete wsAny._parkedKeepalive;
    }
    try { slot.openaiRealtime.close(); } catch { /* ignore */ }
  }
}

/** Top-level SDK entry point — wraps a carrier + embedded server + agent loop. */
export class Patter {
  private localConfig: ResolvedLocalConfig;
  private embeddedServer: EmbeddedServer | null = null;
  private tunnelHandle: TunnelHandle | null = null;
  private _tunnelReadyResolve!: (host: string) => void;
  private _tunnelReadyReject!: (err: Error) => void;
  private _tunnelReady: Promise<string>;
  private _readyResolve!: (host: string) => void;
  private _readyReject!: (err: Error) => void;
  private _ready: Promise<string>;
  /**
   * True iff ``localConfig.webhookUrl`` was populated by ``serve()`` from a
   * freshly-started cloudflared tunnel (rather than by the constructor from
   * an explicit ``webhookUrl`` / ``StaticTunnel`` config). ``disconnect()``
   * uses this flag to clear ONLY the auto-assigned hostname so a subsequent
   * ``serve()`` call (e.g. from a plugin's ``ensureServing`` cycle that
   * disposes + restarts on agent-identity changes) does not throw
   * ``Cannot use both tunnel: true and webhookUrl``.
   */
  private tunnelOwnsWebhookUrl = false;

  /**
   * Pre-rendered first-message TTS audio per outbound call_id. Populated
   * by :meth:`call` when ``agent.prewarmFirstMessage`` is true; consumed
   * by the StreamHandler firstMessage emit so the greeting streams
   * instantly on ``start`` instead of paying the 200-700 ms TTS first-byte
   * latency. See ``AgentOptions.prewarmFirstMessage``.
   *
   * Stores raw bytes in the TTS provider's native sample rate; the
   * carrier-side audio sender resamples on emit.
   */
  private prewarmAudio: Map<string, Buffer> = new Map();
  /**
   * Call IDs whose prewarm cache slot has already been consumed —
   * either by ``popPrewarmAudio`` (cache hit OR miss on the firstMessage
   * emit path) or by ``recordPrewarmWaste`` (call ended before pickup).
   * The prewarm task checks this set BEFORE writing bytes so a slow
   * synth that finishes after the consumer already polled doesn't
   * orphan bytes in ``prewarmAudio``. See FIX #92 in the parity audit.
   */
  private prewarmConsumed: Set<string> = new Set();
  /**
   * Background tasks tracked so :meth:`disconnect` can wait on / drop any
   * still-running prewarm-first-message synth before tearing down.
   */
  private prewarmTasks: Set<Promise<unknown>> = new Set();
  /**
   * TTL eviction timers keyed by call_id so :meth:`disconnect` (and
   * normal consumption / waste-record paths) can cancel any pending
   * timer when the slot drains naturally. Without this, the timer
   * would WARN spuriously after the cache was already emptied.
   */
  private prewarmTtlTimers: Map<string, NodeJS.Timeout> = new Map();
  /**
   * Pre-opened, fully-handshaked provider WebSockets keyed by
   * carrier-issued call_id. Populated by ``parkProviderConnections``
   * during the carrier ringing window; consumed by the per-call
   * StreamHandler at ``start`` via ``adoptWebSocket(...)`` so STT / TTS
   * / Realtime audio can flow on the first turn without paying the
   * 150-900 ms TLS + WS-upgrade + protocol-handshake round-trip again.
   *
   * Distinct from ``prewarmAudio`` (which holds pre-rendered TTS bytes
   * for the first message); the two features are complementary and
   * orthogonal — both can be active for the same call.
   *
   * Each slot may hold up to three parked connections (STT, TTS,
   * Realtime). Drained by:
   *   - {@link popPrewarmedConnections} on the carrier ``start`` event
   *     (consumed normally — the handles transfer to the StreamHandler)
   *   - {@link recordPrewarmWaste} on call-termination paths (no-answer,
   *     busy, failed, canceled, AMD voicemail). Closes parked sockets.
   *   - {@link disconnect} on Patter teardown. Closes all parked sockets.
   */
  private prewarmedConnections: Map<string, ParkedProviderConnections> = new Map();
  /**
   * TTL eviction handles keyed by call_id for connections that are never
   * adopted (e.g. a carrier that swallows ``start``). Closes the parked
   * sockets so they don't leak past the safety window.
   */
  private prewarmedConnTimers: Map<string, NodeJS.Timeout> = new Map();

  /**
   * Speech-edge events for turn-taking instrumentation. Public surface: the
   * seven `on*` proxy accessors below plus the `conversationState` snapshot.
   * Defaults are no-ops — existing users who never set a callback see exactly
   * the previous behaviour.
   *
   * See `src/_speech-events.ts` for the full event taxonomy and the
   * OpenAI Realtime alignment table.
   */
  public readonly speechEvents: SpeechEvents = new SpeechEvents();

  // ---- Speech-edge event callback proxies ------------------------------
  // The seven `on*` properties below follow the canonical voice-agent
  // metric set (user/agent state transitions, turn boundaries, TTFT, audio
  // first-byte) and align with OpenAI Realtime where applicable. They
  // proxy to `speechEvents` so the dispatcher remains the single source of
  // truth (state + OTel).

  get onUserSpeechStarted(): SpeechEventCallback | null {
    return this.speechEvents.onUserSpeechStarted;
  }
  set onUserSpeechStarted(cb: SpeechEventCallback | null) {
    this.speechEvents.onUserSpeechStarted = cb;
  }

  get onUserSpeechEnded(): SpeechEventCallback | null {
    return this.speechEvents.onUserSpeechEnded;
  }
  set onUserSpeechEnded(cb: SpeechEventCallback | null) {
    this.speechEvents.onUserSpeechEnded = cb;
  }

  get onUserSpeechEos(): SpeechEventCallback | null {
    return this.speechEvents.onUserSpeechEos;
  }
  set onUserSpeechEos(cb: SpeechEventCallback | null) {
    this.speechEvents.onUserSpeechEos = cb;
  }

  get onAgentSpeechStarted(): SpeechEventCallback | null {
    return this.speechEvents.onAgentSpeechStarted;
  }
  set onAgentSpeechStarted(cb: SpeechEventCallback | null) {
    this.speechEvents.onAgentSpeechStarted = cb;
  }

  get onAgentSpeechEnded(): SpeechEventCallback | null {
    return this.speechEvents.onAgentSpeechEnded;
  }
  set onAgentSpeechEnded(cb: SpeechEventCallback | null) {
    this.speechEvents.onAgentSpeechEnded = cb;
  }

  get onLlmToken(): SpeechEventCallback | null {
    return this.speechEvents.onLlmToken;
  }
  set onLlmToken(cb: SpeechEventCallback | null) {
    this.speechEvents.onLlmToken = cb;
  }

  get onAudioOut(): SpeechEventCallback | null {
    return this.speechEvents.onAudioOut;
  }
  set onAudioOut(cb: SpeechEventCallback | null) {
    this.speechEvents.onAudioOut = cb;
  }

  /**
   * Snapshot of the current per-side state of the call.
   * Returns the user_state / agent_state payload shape — read-only and
   * safe to call at any time.
   */
  get conversationState(): ConversationStateSnapshot {
    return this.speechEvents.conversationState;
  }

  /**
   * Live `MetricsStore` for the embedded server. Returns `null` before
   * `serve()` is called. Exposed so integrations like `PatterTool` can
   * subscribe to per-call lifecycle events (`call_initiated`,
   * `call_start`, `call_end`).
   */
  get metricsStore(): MetricsStore | null {
    return this.embeddedServer?.metricsStore ?? null;
  }

  /**
   * Resolves to the public webhook hostname as soon as it is known —
   * either statically configured or freshly minted by the tunnel.
   *
   * **Prefer `phone.ready` for outbound calls.** This promise resolves
   * before the embedded HTTP / WebSocket server is in `listen` state, so
   * a `phone.call` placed immediately afterwards can still race the
   * Twilio Media Streams upgrade and produce a "11100 Invalid URL
   * format" call drop on answer.
   *
   * Kept as a separate signal because some integrations (e.g. webhook
   * registration) only need the hostname, not the WS server.
   */
  get tunnelReady(): Promise<string> {
    return this._tunnelReady;
  }

  /**
   * Resolves to the public webhook hostname once the SDK is fully ready
   * to handle carrier callbacks: tunnel resolved, carrier auto-config
   * complete, and the embedded HTTP / WS server in `listen` state.
   *
   * Use this for outbound calls instead of guessing `setTimeout` after
   * `void phone.serve(...)`:
   *
   * ```ts
   * void phone.serve({ agent, tunnel: true });
   * await phone.ready;
   * await phone.call({ to: '+15550001234', agent });
   * ```
   *
   * Rejects with the underlying exception if `serve()` fails before the
   * server is listening.
   */
  get ready(): Promise<string> {
    return this._ready;
  }

  constructor(options: LocalOptions) {
    // Hard-fail if the caller passed a Patter Cloud ``apiKey``.  Cloud mode
    // does not exist in this SDK release; surface the change loudly so users
    // discover it immediately rather than silently sending traffic nowhere.
    if ((options as { apiKey?: unknown }).apiKey !== undefined) {
      throw new Error(
        'Patter Cloud is not yet available in this SDK release. ' +
          'Use local mode with `carrier:` and `phoneNumber:`. ' +
          'Cloud mode will return in a future release.',
      );
    }

    if (!options.phoneNumber) {
      throw new Error('Local mode requires phoneNumber');
    }
    if (!options.carrier) {
      throw new Error(
        'Local mode requires a `carrier` instance. ' +
          'Pass `carrier: new Twilio({...})`, `carrier: new Telnyx({...})` or ' +
          '`carrier: new Plivo({...})`.',
      );
    }

    const carrier = options.carrier;

    // Tunnel normalization — StaticTunnel's hostname becomes webhookUrl.
    const tunnel = options.tunnel;
    let tunnelWebhookUrl: string | undefined;
    if (tunnel instanceof StaticTunnel) {
      if (options.webhookUrl) {
        throw new Error(
          'Cannot use both `tunnel: new StaticTunnel(...)` and `webhookUrl`. ' +
            'Pick one.',
        );
      }
      tunnelWebhookUrl = tunnel.hostname;
    }

    // Normalize webhookUrl: strip any http(s):// prefix and trailing slash
    // so downstream callers that prefix 'wss://' or 'https://' don't double-scheme.
    const rawWebhook = tunnelWebhookUrl ?? options.webhookUrl;
    const normalizedWebhook = rawWebhook
      ? rawWebhook.replace(/^https?:\/\//, '').replace(/\/$/, '')
      : undefined;

    this.localConfig = {
      carrier,
      phoneNumber: options.phoneNumber,
      webhookUrl: normalizedWebhook,
      tunnel: options.tunnel,
      openaiKey: options.openaiKey,
      persistRoot: resolvePersistRoot(options.persist),
    };

    // Initialise the tunnel-ready deferred. If the caller already has a
    // static webhookUrl (or StaticTunnel hostname), resolve immediately —
    // there is no tunnel cold-start to wait on. Otherwise serve() will
    // resolve it once the cloudflared hostname lands.
    this._tunnelReady = new Promise<string>((resolve, reject) => {
      this._tunnelReadyResolve = resolve;
      this._tunnelReadyReject = reject;
    });
    // See `_ready.catch` below — same rationale.
    this._tunnelReady.catch(() => {});
    if (normalizedWebhook) {
      this._tunnelReadyResolve(normalizedWebhook);
    }
    // ``ready`` resolves only after ``serve()`` has the embedded server
    // in listen state — never pre-resolved at construction even when
    // webhookUrl is static. This is the safe signal for outbound calls.
    this._ready = new Promise<string>((resolve, reject) => {
      this._readyResolve = resolve;
      this._readyReject = reject;
    });
    // Suppress Node's unhandled-rejection warning for callers that never
    // touch `phone.ready`. Awaiters of `phone.ready` still see the error.
    this._ready.catch(() => {});
  }

  // === Agent definition ===

  /** Resolve user-supplied agent options against engine defaults and return the merged config. */
  agent(opts: AgentOptions): AgentOptions {
    let working: AgentOptions = { ...opts };

    if (opts.engine) {
      if (opts.provider) {
        throw new Error(
          "Cannot pass both `engine:` and `provider:`. Use one (engine is preferred).",
        );
      }
      const engine = opts.engine;
      if (engine instanceof OpenAIRealtime || engine instanceof OpenAIRealtime2) {
        working = {
          ...working,
          provider: 'openai_realtime',
          model: working.model ?? engine.model,
          voice: working.voice ?? engine.voice,
        };
        // Surface the engine's apiKey to local config so pipeline-mode
        // ``LLMLoop`` and Realtime adapter have a key when no onMessage is set.
        if (!this.localConfig.openaiKey) {
          this.localConfig = { ...this.localConfig, openaiKey: engine.apiKey };
        }
      } else if (engine instanceof ElevenLabsConvAI) {
        working = {
          ...working,
          provider: 'elevenlabs_convai',
          voice: working.voice ?? engine.voice,
        };
      } else {
        throw new Error(
          "Unknown engine. Expected OpenAIRealtime, OpenAIRealtime2, or ElevenLabsConvAI instance.",
        );
      }
    } else if (
      !working.provider &&
      (working.stt !== undefined || working.tts !== undefined || working.llm !== undefined)
    ) {
      // Parity with Python: when the caller supplies any pipeline-mode piece
      // (stt / tts / llm) without an explicit engine or provider, derive
      // ``provider = "pipeline"`` so metrics, logs, and the ``Call started``
      // mode-label are accurate.
      working = { ...working, provider: 'pipeline' };
    }

    // Validate provider
    if (working.provider) {
      const valid = ['openai_realtime', 'elevenlabs_convai', 'pipeline'];
      if (!valid.includes(working.provider)) {
        throw new Error(`provider must be one of: ${valid.join(', ')}. Got: '${working.provider}'`);
      }
    }

    // Validate llm — must implement the LLMProvider interface (duck-typed on
    // ``.stream`` being a function).  Surface a clear error if the caller
    // passed a plain object literal by mistake.
    if (working.llm !== undefined) {
      const llm = working.llm as { stream?: unknown };
      if (!llm || typeof llm.stream !== 'function') {
        throw new Error(
          "`llm` must be an LLMProvider instance (e.g. new AnthropicLLM()). " +
            "Got a value without a `.stream` method.",
        );
      }
      // engine + llm: engine path owns LLM selection (realtime model is the
      // LLM). Warn once and keep the agent working — don't throw.
      if (working.engine) {
        getLogger().warn(
          "agent({ engine, llm }): `llm` is ignored when `engine` is set — " +
            "realtime/ConvAI engines run their own model. Remove `llm` or " +
            "switch to pipeline mode (stt + tts + llm) to silence this warning.",
        );
      }
    }

    // Validate tools — must be Tool class instances (structurally compatible with
    // ToolDefinition). Validation happens at the shape level.
    if (working.tools) {
      if (!Array.isArray(working.tools)) {
        throw new TypeError('tools must be an array');
      }
      working.tools.forEach((tool, i) => {
        if (!tool.name) throw new Error(`tools[${i}] missing required 'name' field`);
        if (!tool.webhookUrl && !tool.handler) throw new Error(`tools[${i}] requires either 'webhookUrl' or 'handler'`);
      });
    }

    // Validate variables
    if (working.variables !== undefined && (typeof working.variables !== 'object' || Array.isArray(working.variables))) {
      throw new TypeError('variables must be an object');
    }

    // Structural sanity + strict-mode validation for tool JSON schemas.
    // Surfaces typos / missing required fields at agent() build time so
    // they don't blow up mid-call. Built-in tools (transfer_call, end_call)
    // are injected later in buildAIAdapter and validated there.
    if (working.tools) {
      validateAllToolSchemas(working.tools as ToolDefinition[]);
    }

    // ``prewarmFirstMessage`` is opt-in (default false) — reverted from
    // 2026-05-18's default-on attempt after the 0.6.2 acceptance run
    // surfaced a phantom-barge-in interaction: prewarm bursts audio
    // at pickup, the very first inbound carrier frame triggered Silero
    // VAD speech_start, the firstMessage was cancelled mid-playback
    // and the user heard a clipped (graffiante) fragment. Until the
    // root cause (anchoring the barge-in gate on first-mark-echo
    // rather than ``firstAudioSentAt = beginSpeaking time``) is fully
    // addressed, default it off so most pipeline calls take the
    // live-streaming path that the user is happy with. Opt in
    // explicitly per agent when willing to pay the trade-off.
    return working;
  }

  // === Serve / test / call ===

  /** Boot the embedded HTTP/WebSocket server, configure the carrier webhook, and resolve `ready`. */
  async serve(opts: ServeOptions): Promise<void> {
    try {
      await this._serveImpl(opts);
    } catch (err) {
      // Make sure ``ready`` is rejected on any failure path so callers
      // doing ``await phone.ready`` after ``void phone.serve(...)`` don't
      // hang forever. Idempotent — no-op if ``ready`` already resolved.
      const e = err instanceof Error ? err : new Error(String(err));
      this._readyReject(e);
      throw e;
    }
  }

  private async _serveImpl(opts: ServeOptions): Promise<void> {
    // Validate agent
    if (!opts.agent || typeof opts.agent !== 'object') {
      throw new TypeError('agent is required. Use phone.agent() to create one.');
    }
    if (!opts.agent.systemPrompt && opts.agent.provider !== 'pipeline') {
      throw new Error('agent.systemPrompt is required');
    }

    // Pre-import AEC at serve startup so the first call doesn't pay the
    // 150-400 ms ESM dynamic-import compile / link cost on the hot path.
    // ``echoCancellation`` is opt-in and rarely set on PSTN, but when it
    // is the lazy ``await import('./audio/aec')`` inside StreamHandler
    // serialises with first-message TTS startup and eats first-turn
    // latency. Eagerly importing here costs nothing for users who never
    // enable AEC (the module is pure data — no side effects).
    if (opts.agent.echoCancellation) {
      try {
        await import('./audio/aec');
      } catch (err) {
        getLogger().debug(`AEC pre-import failed at serve(): ${String(err)}`);
      }
    }

    // Validate port
    if (opts.port !== undefined) {
      if (typeof opts.port !== 'number' || opts.port < 1 || opts.port > 65535) {
        throw new RangeError(`port must be between 1 and 65535, got ${opts.port}`);
      }
    }

    // Validate provider
    const validProviders = ['openai_realtime', 'elevenlabs_convai', 'pipeline'] as const;
    if (opts.agent.provider && !validProviders.includes(opts.agent.provider)) {
      throw new Error(`agent.provider must be one of: ${validProviders.join(', ')}`);
    }

    // Resolve webhookUrl: tunnel or explicit. Static tunnels have already
    // been normalized into webhookUrl by the constructor.
    let webhookUrl = this.localConfig.webhookUrl ?? '';
    const port = opts.port ?? 8000;

    const ctorTunnel = this.localConfig.tunnel;
    const wantsCloudflaredFromServe = opts.tunnel === true;
    const wantsCloudflaredFromCtor =
      ctorTunnel === true || ctorTunnel instanceof CloudflareTunnel;
    const wantsCloudflared = wantsCloudflaredFromServe || wantsCloudflaredFromCtor;

    if (wantsCloudflared && webhookUrl) {
      throw new Error('Cannot use both tunnel: true and webhookUrl. Pick one.');
    }

    const { showBanner } = await import('./banner');
    showBanner();

    if (wantsCloudflared) {
      try {
        const { startTunnel } = await import('./tunnel');
        this.tunnelHandle = await startTunnel(port);
        webhookUrl = this.tunnelHandle.hostname;
        // Propagate the freshly-resolved webhook host into localConfig so a
        // subsequent call() in the same process reads the same hostname instead
        // of the original undefined value. Mark as tunnel-owned so
        // ``disconnect()`` can clear it back out on the way down.
        this.localConfig = { ...this.localConfig, webhookUrl };
        this.tunnelOwnsWebhookUrl = true;
        // Resolve the public deferred so callers awaiting `phone.tunnelReady`
        // can proceed with `phone.call(...)` without race-prone setTimeouts.
        this._tunnelReadyResolve(webhookUrl);
      } catch (err) {
        const e = err instanceof Error ? err : new Error(String(err));
        this._tunnelReadyReject(e);
        throw e;
      }
    }

    if (!webhookUrl) {
      const err = new Error(
        'No webhookUrl configured. Either:\n' +
        '  - Pass webhookUrl in the Patter constructor\n' +
        '  - Use tunnel: true in serve() to auto-create a tunnel'
      );
      this._tunnelReadyReject(err);
      throw err;
    }

    const carrier = this.localConfig.carrier;
    const telephonyProvider = carrier.kind;

    // Auto-configure the carrier so inbound calls hit this server without
    // manual Console setup. Mirrors Python's server.py start() flow.
    //
    // Two opt-outs:
    //   1. `manageWebhook: false` — for users running behind a router/gateway
    //      whose Twilio voice_url is managed externally (Terraform, infra-as-code,
    //      a voice-router function in front of the agent). Without this opt-out,
    //      every boot silently overwrites the externally-managed value.
    //   2. `tunnel: true` overrides any opt-out — the dynamic tunnel hostname is
    //      only known at runtime, so the carrier MUST be reconfigured.
    const wantsCarrierManagement = opts.manageWebhook !== false || wantsCloudflared;
    if (wantsCarrierManagement) {
      const { autoConfigureCarrier } = await import('./carrier-config');
      await autoConfigureCarrier({
        telephonyProvider,
        twilioSid: carrier.kind === 'twilio' ? carrier.accountSid : undefined,
        twilioToken: carrier.kind === 'twilio' ? carrier.authToken : undefined,
        telnyxKey: carrier.kind === 'telnyx' ? carrier.apiKey : undefined,
        telnyxConnectionId: carrier.kind === 'telnyx' ? carrier.connectionId : undefined,
        plivoAuthId: carrier.kind === 'plivo' ? carrier.authId : undefined,
        plivoAuthToken: carrier.kind === 'plivo' ? carrier.authToken : undefined,
        phoneNumber: this.localConfig.phoneNumber,
        webhookHost: webhookUrl,
      });
    }

    this.embeddedServer = new EmbeddedServer(
      {
        twilioSid: carrier.kind === 'twilio' ? carrier.accountSid : undefined,
        twilioToken: carrier.kind === 'twilio' ? carrier.authToken : undefined,
        openaiKey: this.localConfig.openaiKey,
        phoneNumber: this.localConfig.phoneNumber,
        webhookUrl,
        telephonyProvider,
        telnyxKey: carrier.kind === 'telnyx' ? carrier.apiKey : undefined,
        telnyxConnectionId: carrier.kind === 'telnyx' ? carrier.connectionId : undefined,
        telnyxPublicKey: carrier.kind === 'telnyx' ? carrier.publicKey : undefined,
        plivoAuthId: carrier.kind === 'plivo' ? carrier.authId : undefined,
        plivoAuthToken: carrier.kind === 'plivo' ? carrier.authToken : undefined,
        persistRoot: this.localConfig.persistRoot,
      },
      opts.agent,
      opts.onCallStart,
      opts.onCallEnd,
      opts.onTranscript,
      opts.onMessage,
      opts.recording ?? false,
      opts.voicemailMessage ?? '',
      opts.onMetrics,
      opts.pricing,
      opts.dashboard ?? true,
      opts.dashboardToken ?? '',
    );
    // Forward the prewarm-audio accessor so the per-call StreamHandler can
    // consume the pre-rendered first-message audio (if any) on ``start``.
    this.embeddedServer.popPrewarmAudio = this.popPrewarmAudio;
    // Forward the parked-connections accessor so the per-call
    // StreamHandler can adopt pre-opened STT / TTS / Realtime WSs at
    // ``start`` instead of paying the cold-handshake on first turn.
    this.embeddedServer.popPrewarmedConnections = this.popPrewarmedConnections;
    // Forward the waste-recorder so the carrier status / hangup webhook
    // handlers can evict the cache when a call terminates before the
    // media stream starts (no-answer, busy, failed, canceled, or AMD
    // voicemail). Without this, ``recordPrewarmWaste`` is only invoked
    // from ``endCall`` and the server-side teardown path leaks the
    // bytes for the lifetime of the Patter instance. See FIX #91.
    this.embeddedServer.recordPrewarmWaste = this.recordPrewarmWaste;
    try {
      await this.embeddedServer.start(port);
      // Server is now in `listen` state on 127.0.0.1:port — safe to place
      // outbound calls because the WS upgrade has a route to land on.

      // Tunnel reachability self-test: cloudflared returns the URL the
      // moment its control plane has issued it, but the public DNS edge
      // (and the cloudflared origin bridge) can take several extra
      // seconds to start serving the trycloudflare.com hostname. Until
      // that propagation completes, Twilio (and any other webhook
      // caller) gets HTTP 502 "Unknown host" and the call is torn down
      // before it ever reaches the WS media stream. We block
      // `phone.ready` until DNS resolves through the public resolvers
      // Twilio's edge uses, then add a short grace window for
      // cloudflared's origin bridge to stabilise. Static /
      // explicit-webhookUrl paths skip the probe (the operator already
      // knows the host is up). See `waitForTunnelPubliclyReachable`
      // for the rationale behind DNS-only vs full HTTP probing.
      if (this.tunnelHandle) {
        await waitForTunnelPubliclyReachable(webhookUrl);
      }

      this._readyResolve(webhookUrl);
    } catch (err) {
      const e = err instanceof Error ? err : new Error(String(err));
      this._readyReject(e);
      throw e;
    }
  }

  /** Run the agent in interactive terminal-test mode (no real telephony). */
  async test(opts: ServeOptions): Promise<void> {
    const { TestSession } = await import('./test-mode');
    const session = new TestSession();
    await session.run({
      agent: opts.agent,
      openaiKey: this.localConfig.openaiKey,
      onMessage: typeof opts.onMessage === 'function' ? opts.onMessage : undefined,
      onCallStart: opts.onCallStart,
      onCallEnd: opts.onCallEnd,
    });
  }

  /**
   * Pop and return the pre-synthesised first-message audio for ``callId``.
   *
   * Returns ``undefined`` when ``agent.prewarmFirstMessage`` was not set
   * for the originating outbound call, or when the synth was still in
   * flight at the moment the carrier emitted ``start`` (cache miss — the
   * StreamHandler falls back to live TTS).
   *
   * Called by the per-call StreamHandler at the start of the firstMessage
   * emit. Returning bytes here lets the handler skip the live TTS
   * synthesis and stream the cached buffer directly.
   *
   * Marks ``callId`` as consumed regardless of cache hit/miss so a slow
   * synth task that finishes after this call drops its bytes instead of
   * orphaning them in ``prewarmAudio``. See FIX #92.
   */
  popPrewarmAudio = (callId: string): Buffer | undefined => {
    this.prewarmConsumed.add(callId);
    const ttl = this.prewarmTtlTimers.get(callId);
    if (ttl !== undefined) {
      clearTimeout(ttl);
      this.prewarmTtlTimers.delete(callId);
    }
    const buf = this.prewarmAudio.get(callId);
    if (buf !== undefined) this.prewarmAudio.delete(callId);
    return buf;
  };

  /**
   * Log a warning if a prewarmed greeting was paid for but never used.
   * The TTS bill for ``agent.firstMessage`` has already been incurred by
   * the background synth task, so the user should know — opt-in feature
   * with a known cost surface.
   *
   * Idempotent: the second call for the same ``callId`` is a no-op, so
   * the status callback firing first and ``endCall`` running afterwards
   * (or vice-versa) does not double-WARN. Public so the embedded
   * server's webhook handlers can invoke it on no-answer / busy /
   * failed / canceled / AMD-machine paths. See FIX #91.
   */
  recordPrewarmWaste = (callId: string): void => {
    // Always drain any parked provider WS — they're cheap to discard
    // and we don't want to leak open sockets when the call dies.
    this.closePrewarmedConnections(callId);
    if (this.prewarmConsumed.has(callId)) {
      this.prewarmAudio.delete(callId);
      return;
    }
    this.prewarmConsumed.add(callId);
    const ttl = this.prewarmTtlTimers.get(callId);
    if (ttl !== undefined) {
      clearTimeout(ttl);
      this.prewarmTtlTimers.delete(callId);
    }
    const buf = this.prewarmAudio.get(callId);
    if (buf !== undefined) {
      this.prewarmAudio.delete(callId);
      getLogger().warn(
        `Prewarm wasted for call ${callId} — first-message TTS already paid ` +
          `(~${buf.byteLength} bytes synthesised) but call ended before pickup.`,
      );
    }
  };

  /**
   * Pop and return the parked provider WebSockets for ``callId``, or
   * ``undefined`` when no parked connections exist.
   *
   * Wired into ``EmbeddedServer.popPrewarmedConnections`` so the
   * per-call ``StreamHandler`` can adopt the parked sockets at the
   * carrier ``start`` event instead of opening fresh ones — saving
   * ~150-900 ms of cold-start handshake on the first turn.
   */
  popPrewarmedConnections = (callId: string): ParkedProviderConnections | undefined => {
    const slot = this.prewarmedConnections.get(callId);
    if (slot === undefined) return undefined;
    this.prewarmedConnections.delete(callId);
    const ttl = this.prewarmedConnTimers.get(callId);
    if (ttl !== undefined) {
      clearTimeout(ttl);
      this.prewarmedConnTimers.delete(callId);
    }
    return slot;
  };

  /**
   * Close any parked provider WebSockets for ``callId``. Wired into
   * ``EmbeddedServer.closePrewarmedConnections`` so call-termination
   * paths (no-answer, busy, failed, canceled, AMD voicemail) drop the
   * sockets cleanly instead of leaving them to the upstream timeout.
   */
  closePrewarmedConnections = (callId: string): void => {
    const slot = this.prewarmedConnections.get(callId);
    if (slot === undefined) return;
    this.prewarmedConnections.delete(callId);
    const ttl = this.prewarmedConnTimers.get(callId);
    if (ttl !== undefined) {
      clearTimeout(ttl);
      this.prewarmedConnTimers.delete(callId);
    }
    closeParkedConnections(slot);
  };

  /**
   * Open and park provider WebSockets in parallel with the carrier-side
   * ``initiateCall``. Unlike :meth:`spawnProviderWarmup` (which closes
   * the WS after a brief idle), the sockets opened here stay OPEN and
   * are handed off to the per-call ``StreamHandler`` on ``start``.
   *
   * This is the structural fix for first-turn cold-start: on Node's
   * ``ws`` package, opening + closing a WS does NOT warm TLS for the
   * next open — every fresh ``new WebSocket()`` re-pays the full
   * TCP + TLS + HTTP-101 round-trip. By keeping the WS open and
   * adopting it directly, the live first turn skips the handshake
   * entirely (saves ~150-900 ms depending on provider).
   *
   * Best-effort: each provider's parking task is wrapped in
   * ``Promise.allSettled`` so a slow or failing endpoint cannot block
   * the others. Providers without ``openParkedConnection`` contribute
   * nothing — the call falls through to the cold ``connect()`` path
   * for that provider.
   */
  private parkProviderConnections(agent: AgentOptions, callId: string): void {
    const stt = agent.stt as { openParkedConnection?: () => Promise<import('ws').WebSocket> } | undefined;
    const tts = agent.tts as { openParkedConnection?: () => Promise<import('./providers/elevenlabs-ws-tts').ElevenLabsParkedWS> } | undefined;
    const sttOpen = typeof stt?.openParkedConnection === 'function' ? stt.openParkedConnection.bind(stt) : null;
    const ttsOpen = typeof tts?.openParkedConnection === 'function' ? tts.openParkedConnection.bind(tts) : null;
    // Detect OpenAI Realtime agents (provider == 'openai_realtime' or
    // 'openai_realtime_2'). The adapter isn't constructed yet — the
    // per-call StreamHandler builds it at `start`. We instantiate a
    // throw-away one here just long enough to call openParkedConnection
    // and produce a primed WS, then store the WS in the slot. The live
    // adapter (built per-call) adopts it via `adoptWebSocket`. Cast
    // through `string` because the public ``AgentOptions.provider``
    // literal union doesn't yet enumerate ``openai_realtime_2`` (the
    // GA engine carries it through internally).
    const providerStr = (agent.provider as unknown as string | undefined) ?? '';
    const wantsRealtimePark =
      providerStr === 'openai_realtime' || providerStr === 'openai_realtime_2';
    if (!sttOpen && !ttsOpen && !wantsRealtimePark) return;

    const slot: ParkedProviderConnections = {};
    this.prewarmedConnections.set(callId, slot);

    const startedAt = Date.now();
    const tasks: Array<Promise<void>> = [];
    if (sttOpen) {
      tasks.push((async () => {
        try {
          const ws = await sttOpen();
          // Slot may have been drained while we were opening (call
          // failed early, ``start`` already arrived and consumer
          // already adopted nothing, etc.). Close cleanly in that case.
          if (this.prewarmedConnections.get(callId) !== slot) {
            try { ws.close(); } catch { /* ignore */ }
            return;
          }
          slot.stt = ws;
          getLogger().info(
            `[PREWARM] callId=${callId} provider=stt ms=${Date.now() - startedAt}`,
          );
        } catch (err) {
          getLogger().debug(`Park STT failed for ${callId}: ${String(err)}`);
        }
      })());
    }
    if (ttsOpen) {
      tasks.push((async () => {
        try {
          const parked = await ttsOpen();
          if (this.prewarmedConnections.get(callId) !== slot) {
            try { parked.ws.close(); } catch { /* ignore */ }
            return;
          }
          slot.tts = parked;
          getLogger().info(
            `[PREWARM] callId=${callId} provider=tts ms=${Date.now() - startedAt}`,
          );
        } catch (err) {
          getLogger().debug(`Park TTS failed for ${callId}: ${String(err)}`);
        }
      })());
    }
    if (wantsRealtimePark) {
      tasks.push((async () => {
        // Defer the import so users that don't use Realtime don't pay
        // the load-time cost of the adapter + ws module.
        const { OpenAIRealtime2Adapter } = await import('./providers/openai-realtime-2');
        const apiKey = process.env.OPENAI_API_KEY ?? '';
        if (!apiKey) {
          getLogger().debug(`Park OpenAI Realtime skipped for ${callId}: no OPENAI_API_KEY`);
          return;
        }
        try {
          // Build a throw-away adapter just to call openParkedConnection.
          // The session.update payload mirrors what the per-call
          // StreamHandler would send so no second session.update is
          // needed after adoption. The constructor signature is
          // positional (inherited from OpenAIRealtimeAdapter).
          const tmpAdapter = new OpenAIRealtime2Adapter(
            apiKey,
            (agent.model as string | undefined) ?? 'gpt-realtime-mini',
            (agent.voice as string | undefined) ?? 'alloy',
            (agent.systemPrompt as string | undefined) ?? '',
            [],
            // audioFormat — the GA adapter always emits audio/pcm@24000
            // internally regardless of this value, but it's a required
            // positional param. Default to g711_ulaw (Twilio wire format).
            undefined,
          );
          const ws = await tmpAdapter.openParkedConnection();
          if (this.prewarmedConnections.get(callId) !== slot) {
            try { ws.close(); } catch { /* ignore */ }
            return;
          }
          slot.openaiRealtime = ws;
          getLogger().info(
            `[PREWARM] callId=${callId} provider=openai_realtime ms=${Date.now() - startedAt}`,
          );
        } catch (err) {
          getLogger().debug(`Park OpenAI Realtime failed for ${callId}: ${String(err)}`);
        }
      })());
    }

    const task = (async () => {
      await Promise.allSettled(tasks);
    })();
    this.prewarmTasks.add(task);
    void task.finally(() => {
      this.prewarmTasks.delete(task);
      // Schedule TTL cleanup so a never-adopted slot is force-closed.
      if (!this.prewarmedConnections.has(callId)) return;
      const handle = setTimeout(() => {
        this.prewarmedConnTimers.delete(callId);
        const orphan = this.prewarmedConnections.get(callId);
        if (orphan === undefined) return;
        this.prewarmedConnections.delete(callId);
        closeParkedConnections(orphan);
        getLogger().warn(
          `[PREWARM] parked connections evicted by TTL for ${callId} — ` +
            `call never reached start (~${(PARKED_CONN_TTL_MS / 1000).toFixed(0)}s).`,
        );
      }, PARKED_CONN_TTL_MS);
      handle.unref?.();
      this.prewarmedConnTimers.set(callId, handle);
    });
  }

  /**
   * Spawn a fire-and-forget task that warms up STT / TTS / LLM in
   * parallel with the carrier-side ``initiateCall``.
   *
   * Best-effort: each provider's optional ``warmup()`` is wrapped in
   * ``Promise.allSettled`` so a slow or failing endpoint cannot block
   * the others. Providers without ``warmup`` contribute nothing.
   */
  private spawnProviderWarmup(agent: AgentOptions): void {
    const targets: Array<{ name: string; fn: () => Promise<void> }> = [];
    const collect = (provider: unknown, label: string): void => {
      if (!provider || typeof provider !== 'object') return;
      const fn = (provider as { warmup?: () => Promise<void> }).warmup;
      if (typeof fn !== 'function') return;
      targets.push({
        name: label,
        fn: fn.bind(provider) as () => Promise<void>,
      });
    };
    collect(agent.stt, 'stt');
    collect(agent.tts, 'tts');
    collect(agent.llm, 'llm');
    if (targets.length === 0) return;

    const task = (async () => {
      const results = await Promise.allSettled(targets.map((t) => t.fn()));
      results.forEach((r, i) => {
        if (r.status === 'rejected') {
          getLogger().debug(
            `Provider warmup failed (${targets[i].name}): ${String(r.reason)}`,
          );
        }
      });
    })();
    this.prewarmTasks.add(task);
    void task.finally(() => this.prewarmTasks.delete(task));
  }

  /**
   * Pre-render ``agent.firstMessage`` to TTS bytes during the ringing
   * window and stash them in ``prewarmAudio.set(callId, buf)``.
   *
   * Skipped silently when ``agent.prewarmFirstMessage`` is false or
   * when ``agent.tts`` / ``agent.firstMessage`` is missing. The synth
   * is bounded by ``ringTimeout`` (default 25 s) so a never-answered
   * call doesn't tie up the TTS connection. On timeout / error the
   * cache is left empty and the StreamHandler falls back to live TTS.
   *
   * **Pipeline mode only.** Realtime / ConvAI provider modes never
   * consume the prewarm cache (the StreamHandler for those modes runs
   * its first-message emit through the provider's own audio path).
   * Spawning the prewarm in those modes pays the TTS bill for nothing
   * — refused with a warn.
   *
   * **Capped at ``PREWARM_CACHE_MAX`` concurrent entries.** Refused
   * with a warn when the cap is reached (the call still proceeds —
   * StreamHandler falls back to live TTS).
   */
  private spawnPrewarmFirstMessage(
    agent: AgentOptions,
    callId: string,
    ringTimeout: number | null | undefined,
    carrier?: CarrierKind,
  ): void {
    if (!agent.prewarmFirstMessage) return;
    // FIX #94 — Realtime / ConvAI never consume the cache. Refuse early
    // so the user notices the silent TTS waste instead of paying for a
    // synth no caller will ever hear.
    const providerMode = (agent.provider as string | undefined) ?? 'openai_realtime';
    if (providerMode !== 'pipeline') {
      getLogger().warn(
        `agent.prewarmFirstMessage=true is only supported in pipeline mode ` +
          `(provider=${providerMode}); skipping pre-synth to avoid wasted TTS spend.`,
      );
      return;
    }
    const firstMessage = agent.firstMessage ?? '';
    const tts = agent.tts;
    if (!firstMessage || !tts) return;
    if (typeof tts.synthesizeStream !== 'function') return;

    // Advise the TTS adapter of the telephony carrier BEFORE we trigger
    // the synth so it can produce wire-native bytes (``ulaw_8000`` for
    // Twilio, ``pcm_16000`` for Telnyx) — skipping the client-side
    // resample + mulaw encode that produced audible artifacts on the
    // prewarmed firstMessage during 0.6.2 acceptance. The hook is opt-in
    // per-adapter; adapters that don't expose it (or that the user
    // configured with an explicit outputFormat) keep their format.
    if (carrier) {
      const carrierAware = tts as unknown as {
        setTelephonyCarrier?: (c: string) => void;
      };
      if (typeof carrierAware.setTelephonyCarrier === 'function') {
        try {
          carrierAware.setTelephonyCarrier(carrier);
        } catch (err) {
          getLogger().debug(
            `Prewarm TTS setTelephonyCarrier failed for ${callId}: ${String(err)}`,
          );
        }
      }
    }

    // FIX #96 — refuse to spawn when the cache (live entries +
    // in-flight synth tasks) would exceed the cap. Counting both
    // active entries AND pending tasks keeps the bound honest under
    // outbound-flood conditions where carrier ``start`` events lag.
    const inFlight = this.prewarmAudio.size + this.prewarmTasks.size;
    if (inFlight >= PREWARM_CACHE_MAX) {
      getLogger().warn(
        `Prewarm cache full (${inFlight}/${PREWARM_CACHE_MAX} in-flight) — ` +
          `skipping pre-synth for call ${callId}; falling back to live TTS at pickup.`,
      );
      return;
    }

    const timeoutMs = (typeof ringTimeout === 'number' ? ringTimeout : 25) * 1000;

    const task = (async () => {
      try {
        const accumulate = async (): Promise<Buffer> => {
          const chunks: Buffer[] = [];
          for await (const chunk of tts.synthesizeStream(firstMessage)) {
            // ``synthesizeStream`` typed return is ``Buffer``, but real
            // adapters may yield a ``Uint8Array`` (or anything Buffer-y).
            // Guard at runtime so we never crash on a typed-but-untrue
            // chunk.
            const u = chunk as unknown;
            if (Buffer.isBuffer(u)) chunks.push(u);
            else if (ArrayBuffer.isView(u))
              chunks.push(Buffer.from((u as Uint8Array).buffer, (u as Uint8Array).byteOffset, (u as Uint8Array).byteLength));
          }
          return Buffer.concat(chunks);
        };
        const timer = new Promise<Buffer>((_resolve, reject) =>
          setTimeout(
            () => reject(new Error('prewarm-first-message timeout')),
            timeoutMs,
          ).unref?.(),
        );
        const buf = await Promise.race([accumulate(), timer]);
        if (buf.byteLength > 0) {
          // FIX #92 — race guard. If the consumer already polled (cache
          // hit or miss) before the synth finished, the StreamHandler
          // has already fallen back to live TTS; writing bytes here
          // would orphan them in ``prewarmAudio`` until ``endCall`` ever
          // runs.
          if (this.prewarmConsumed.has(callId)) {
            getLogger().warn(
              `Prewarm orphaned for call ${callId} — synth completed ` +
                `(~${buf.byteLength} bytes) AFTER consumer polled; bytes dropped, ` +
                `TTS bill already paid.`,
            );
            return;
          }
          this.prewarmAudio.set(callId, buf);
          getLogger().debug(
            `Prewarm first-message ready for call ${callId} (${buf.byteLength} bytes)`,
          );
        }
      } catch (err) {
        getLogger().debug(
          `Prewarm first-message failed for call ${callId}: ${String(err)}`,
        );
      }
    })();
    this.prewarmTasks.add(task);
    void task.finally(() => {
      this.prewarmTasks.delete(task);
      // FIX #96 — schedule TTL eviction once the synth task has produced
      // (or failed to produce) cache bytes. If the carrier never fires
      // ``start`` AND the status / hangup callback never runs (e.g.
      // cloud-side telephony quirk), the entry would otherwise leak.
      // The timer is no-op when the slot has already been drained.
      if (!this.prewarmAudio.has(callId)) return;
      const ttlMs = timeoutMs + PREWARM_TTL_GRACE_MS;
      const handle = setTimeout(() => {
        this.prewarmTtlTimers.delete(callId);
        const orphan = this.prewarmAudio.get(callId);
        if (orphan === undefined) return;
        this.prewarmAudio.delete(callId);
        this.prewarmConsumed.add(callId);
        getLogger().warn(
          `Prewarm bytes evicted by TTL — call ${callId} never consumed them ` +
            `(~${orphan.byteLength} bytes synthesised, ${(ttlMs / 1000).toFixed(1)}s ` +
            `after ringTimeout).`,
        );
      }, ttlMs);
      // Don't keep the event loop alive on the eviction timer alone —
      // matches the behaviour of the timeout race above.
      handle.unref?.();
      this.prewarmTtlTimers.set(callId, handle);
    });
  }

  /**
   * Place an outbound call via the configured carrier.
   *
   * With `wait: false` (default) this resolves to `void` the instant the
   * carrier accepts the dial (fire-and-forget). With `wait: true` it blocks
   * until the call reaches a terminal state and resolves to a
   * {@link CallResult} — see {@link LocalCallOptions.wait}. Mirrors Python's
   * `Patter.call(..., wait=False)`.
   */
  async call(options: LocalCallOptions): Promise<CallResult | void> {
    if (!options.to) {
      throw new Error("'to' phone number is required");
    }
    if (!/^\+[1-9]\d{6,14}$/.test(options.to)) {
      throw new Error("'to' must be E.164 format (+<country><digits>). Got value with invalid format.");
    }
    if (options.wait && !this.embeddedServer) {
      throw new PatterConnectionError(
        'call({ wait: true }) requires an active server to receive the ' +
          'carrier completion webhooks. Call `await phone.serve(...)` first, ' +
          'or use `await using phone = new Patter(...)` (and serve inside the ' +
          'block) which keeps the server up for the duration of the block.',
      );
    }
    const { phoneNumber, webhookUrl, carrier } = this.localConfig;
    // Hoisted to method scope so the wait block below can correlate the
    // carrier-issued id with its completion promise. Assigned in each carrier
    // branch from the carrier API response.
    let callId = '';

    // Default ring timeout — 25 s limits phantom calls. Pass ``ringTimeout:
    // 60`` for legacy parity, or ``ringTimeout: null`` to omit and let the
    // carrier pick its own default.
    const effectiveRingTimeout: number | null =
      options.ringTimeout === undefined ? 25 : options.ringTimeout;

    // The per-call onMachineDetection callback is registered into the
    // embedded server's per-callSid Map (``onMachineDetectionByCallSid``)
    // inside each carrier branch below, once the callSid is known. Keying by
    // callSid (instead of a single shared slot) means concurrent outbound
    // calls each get their own callback and a fast AMD result for one call
    // can never leak into another caller's callback.
    // AMD is **on by default**; pass ``machineDetection: false`` to
    // explicitly skip it (e.g. to save per-call AMD billing when the
    // destination is known to be a human). A non-empty voicemailMessage
    // also implicitly requires AMD regardless of the flag.
    const wantsAmd = options.machineDetection !== false || Boolean(options.voicemailMessage);

    // Pre-warm provider connections in parallel with the carrier-side
    // ``initiateCall`` so DNS / TLS / HTTP/2 handshakes complete during
    // the ringing window (3-15 s typically). Best-effort: warmup
    // failures are logged at debug and never abort the call. Off when
    // the user explicitly sets ``agent.prewarm: false``.
    if (options.agent.prewarm !== false) {
      this.spawnProviderWarmup(options.agent);
    }

    if (carrier.kind === 'telnyx') {
      // Telnyx outbound call via Call Control API.
      // Note: ``stream_url``/``stream_track`` are NOT accepted on
      // ``POST /v2/calls`` — Telnyx ignores them at dial time. Streaming is
      // started later via ``actions/streaming_start`` once the call is
      // answered. Mirrors ``libraries/python/getpatter/providers/telnyx_adapter.py``.
      const telnyxKey = carrier.apiKey;
      const connectionId = carrier.connectionId;

      const telnyxPayload: Record<string, unknown> = {
        connection_id: connectionId,
        from: phoneNumber,
        to: options.to,
      };
      if (wantsAmd) {
        // ``greeting_end`` is the production-recommended mode: Telnyx
        // returns the human/machine classification on
        // ``call.machine.detection.ended`` AND emits a follow-up
        // ``call.machine.greeting.ended`` once the answering-machine
        // greeting reaches the beep, so a downstream voicemail-drop
        // can speak immediately after the prompt.
        telnyxPayload.answering_machine_detection = 'greeting_end';
      }
      if (effectiveRingTimeout !== null && effectiveRingTimeout !== undefined) {
        telnyxPayload.timeout_secs = Math.max(1, Math.floor(effectiveRingTimeout));
      }
      const response = await fetch('https://api.telnyx.com/v2/calls', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${telnyxKey}`,
        },
        body: JSON.stringify(telnyxPayload),
      });
      if (!response.ok) {
        throw new ProvisionError(`Failed to initiate Telnyx call: ${await response.text()}`);
      }
      let telnyxCallId: string | undefined;
      try {
        const body = (await response.clone().json()) as { data?: { call_control_id?: string } };
        telnyxCallId = body.data?.call_control_id;
      } catch {
        /* non-fatal */
      }
      if (telnyxCallId) {
        const initiatedPayload = {
          call_id: telnyxCallId,
          caller: phoneNumber,
          callee: options.to,
          direction: 'outbound',
          status: 'initiated',
        } as const;
        if (this.embeddedServer) {
          this.embeddedServer.metricsStore.recordCallInitiated(initiatedPayload);
          // Register the per-callSid AMD callback now that we have the
          // call_control_id. Keying by callSid avoids a single-slot race
          // when multiple outbound calls are in flight simultaneously.
          if (options.onMachineDetection) {
            this.embeddedServer.onMachineDetectionByCallSid.set(
              telnyxCallId,
              options.onMachineDetection,
            );
          }
        }
        // Relay to a standalone dashboard (running in a separate process)
        // so it surfaces the dial attempt during ringing, not only when
        // media arrives on pickup. Fire-and-forget — silent when no
        // standalone dashboard is listening.
        try {
          const { notifyDashboard } = await import('./dashboard/persistence');
          notifyDashboard(initiatedPayload);
        } catch {
          /* ignore */
        }
      }
      if (telnyxCallId) {
        callId = telnyxCallId;
        this.spawnPrewarmFirstMessage(options.agent, telnyxCallId, effectiveRingTimeout, 'telnyx');
        // Park provider WebSockets in parallel so the per-call
        // StreamHandler can adopt them at ``start`` instead of paying
        // the cold-handshake on first turn. Off when the user
        // explicitly sets ``agent.prewarm: false``.
        if (options.agent.prewarm !== false) {
          this.parkProviderConnections(options.agent, telnyxCallId);
        }
      }
      return this.maybeAwaitCompletion(options, callId, effectiveRingTimeout);
    }

    if (carrier.kind === 'plivo') {
      // Plivo outbound: POST /Call/ with an answer_url. Plivo fetches that URL
      // on pickup and the /webhooks/plivo/voice handler returns the <Stream>
      // XML — so the WSS URL travels in the answer XML, not as a dial param.
      // Mirrors ``libraries/python/getpatter/providers/plivo_adapter.py``.
      const auth = `Basic ${Buffer.from(`${carrier.authId}:${carrier.authToken}`).toString('base64')}`;
      const plivoPayload: Record<string, unknown> = {
        from: phoneNumber,
        to: options.to,
        answer_url: `https://${webhookUrl}/webhooks/plivo/voice`,
        answer_method: 'POST',
        // hangup_url is Plivo's StatusCallback analogue — without it the
        // /webhooks/plivo/status route never fires for outbound calls and
        // the dashboard misses no-answer / busy / failed.
        hangup_url: `https://${webhookUrl}/webhooks/plivo/status`,
        hangup_method: 'POST',
      };
      if (effectiveRingTimeout !== null && effectiveRingTimeout !== undefined) {
        plivoPayload.ring_timeout = Math.max(1, Math.floor(effectiveRingTimeout));
      }
      if (wantsAmd) {
        plivoPayload.machine_detection = 'true';
        plivoPayload.machine_detection_time = 5000;
        plivoPayload.machine_detection_url = `https://${webhookUrl}/webhooks/plivo/amd`;
        plivoPayload.machine_detection_method = 'POST';
      }
      // Store voicemail message on the running server so the AMD webhook can use it.
      if (options.voicemailMessage && this.embeddedServer) {
        this.embeddedServer.voicemailMessage = options.voicemailMessage;
      }
      const response = await fetch(`https://api.plivo.com/v1/Account/${carrier.authId}/Call/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: auth },
        body: JSON.stringify(plivoPayload),
      });
      if (!response.ok) {
        throw new ProvisionError(`Failed to initiate Plivo call: ${await response.text()}`);
      }
      let plivoCallId: string | undefined;
      try {
        const body = (await response.clone().json()) as { request_uuid?: string };
        plivoCallId = body.request_uuid;
      } catch {
        /* non-fatal */
      }
      if (plivoCallId) {
        const initiatedPayload = {
          call_id: plivoCallId,
          caller: phoneNumber,
          callee: options.to,
          direction: 'outbound',
          status: 'initiated',
        } as const;
        if (this.embeddedServer) {
          this.embeddedServer.metricsStore.recordCallInitiated(initiatedPayload);
          // Register the per-callSid AMD callback now that we have an id.
          // NOTE: Plivo's POST /Call/ returns ``request_uuid`` (the queued
          // handle), while the inbound AMD webhook fires with the live
          // ``CallUUID`` — the two identifiers differ. The Plivo AMD webhook
          // in server.ts therefore falls back to the single pending callback
          // when the keyed lookup misses, so this registration still fires.
          if (options.onMachineDetection) {
            this.embeddedServer.onMachineDetectionByCallSid.set(
              plivoCallId,
              options.onMachineDetection,
            );
          }
        }
        try {
          const { notifyDashboard } = await import('./dashboard/persistence');
          notifyDashboard(initiatedPayload);
        } catch {
          /* ignore */
        }
        this.spawnPrewarmFirstMessage(options.agent, plivoCallId, effectiveRingTimeout, 'plivo');
        if (options.agent.prewarm !== false) {
          this.parkProviderConnections(options.agent, plivoCallId);
        }
      }
      return;
    }

    // Twilio
    const twilioSid = carrier.accountSid;
    const twilioToken = carrier.authToken;
    const statusCallbackUrl = `https://${webhookUrl}/webhooks/twilio/status`;
    const url = `https://api.twilio.com/2010-04-01/Accounts/${twilioSid}/Calls.json`;
    // Inline TwiML avoids the extra Twilio→webhook round-trip (~100-200ms)
    // that the ``Url:`` parameter would trigger. Mirrors the Python adapter
    // (``libraries/python/getpatter/providers/twilio_adapter.py``) which uses
    // ``twiml=...`` for outbound calls.
    const streamUrl = `wss://${webhookUrl}/ws/stream/outbound`;
    const inlineTwiml = `<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="${streamUrl}"/></Connect></Response>`;
    const params = new URLSearchParams({
      To: options.to,
      From: phoneNumber,
      Twiml: inlineTwiml,
      StatusCallback: statusCallbackUrl,
      StatusCallbackMethod: 'POST',
    });
    // StatusCallbackEvent is a multi-value parameter — Twilio expects
    // repeated keys, NOT a space-separated single value. The previous
    // ``'initiated ringing answered completed'`` form triggered Twilio
    // notification 21626 ("invalid statusCallbackEvents") on every call,
    // and on some ingestion paths also broke the answer-handler webhook
    // (root cause of intermittent 11100 WS-upgrade failures).
    // See https://www.twilio.com/docs/voice/api/call-resource#statuscallbackevent
    for (const evt of ['initiated', 'ringing', 'answered', 'completed']) {
      params.append('StatusCallbackEvent', evt);
    }
    if (wantsAmd) {
      // DetectMessageEnd waits for the greeting to finish before reporting
      // ``machine_end_*`` so a follow-up voicemail-drop lands after the
      // beep (~100% accuracy in US, slightly lower internationally).
      // AsyncAmd avoids the 3-5 s answer-latency penalty on human pickups
      // — the call connects immediately and AMD result arrives via the
      // ``/webhooks/twilio/amd`` callback. Twilio best-practice default.
      params.append('MachineDetection', 'DetectMessageEnd');
      params.append('AsyncAmd', 'true');
      params.append('AsyncAmdStatusCallback', `https://${webhookUrl}/webhooks/twilio/amd`);
    }
    if (effectiveRingTimeout !== null && effectiveRingTimeout !== undefined) {
      params.append('Timeout', String(Math.max(1, Math.floor(effectiveRingTimeout))));
    }
    // Store voicemail message on the running server so AMD webhook can use it
    if (options.voicemailMessage && this.embeddedServer) {
      this.embeddedServer.voicemailMessage = options.voicemailMessage;
    }
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        Authorization: `Basic ${Buffer.from(`${twilioSid}:${twilioToken}`).toString('base64')}`,
      },
      body: params.toString(),
    });
    if (!response.ok) {
      throw new ProvisionError(`Failed to initiate call: ${await response.text()}`);
    }
    // Pre-register the call so the dashboard shows attempts even when the
    // callee never answers (no-answer, busy, carrier-rejected). BUG #06.
    // Also log the Twilio notifications URL so users can self-diagnose
    // call-quality issues (warning 21626, fatal 11100, etc.) without
    // having to hunt them down via the Twilio Console.
    let twilioCallSid: string | undefined;
    let twilioNotificationsPath: string | undefined;
    try {
      const body = (await response.clone().json()) as {
        sid?: string;
        subresource_uris?: { notifications?: string };
      };
      twilioCallSid = body.sid;
      twilioNotificationsPath = body.subresource_uris?.notifications;
    } catch {
      /* non-fatal — the statusCallback will register anyway */
    }
    if (twilioCallSid) {
      const initiatedPayload = {
        call_id: twilioCallSid,
        caller: phoneNumber,
        callee: options.to,
        direction: 'outbound',
        status: 'initiated',
      } as const;
      if (this.embeddedServer) {
        this.embeddedServer.metricsStore.recordCallInitiated(initiatedPayload);
        // Register the per-callSid AMD callback now that we have the CallSid.
        // Keying by callSid avoids a single-slot race when multiple outbound
        // calls are in flight simultaneously.
        if (options.onMachineDetection) {
          this.embeddedServer.onMachineDetectionByCallSid.set(
            twilioCallSid,
            options.onMachineDetection,
          );
        }
        if (twilioNotificationsPath) {
          getLogger().info(
            `Outbound call ${twilioCallSid} placed. ` +
              `Twilio notifications: https://api.twilio.com${twilioNotificationsPath} ` +
              '(check here if the call drops with no audio).',
          );
        }
      }
      try {
        const { notifyDashboard } = await import('./dashboard/persistence');
        notifyDashboard(initiatedPayload);
      } catch {
        /* ignore */
      }
    }
    if (twilioCallSid) {
      callId = twilioCallSid;
      this.spawnPrewarmFirstMessage(options.agent, twilioCallSid, effectiveRingTimeout, 'twilio');
      // Park provider WebSockets in parallel so the per-call
      // StreamHandler can adopt them at ``start`` instead of paying
      // the cold-handshake on first turn. Off when the user
      // explicitly sets ``agent.prewarm: false``.
      if (options.agent.prewarm !== false) {
        this.parkProviderConnections(options.agent, twilioCallSid);
      }
    }
    return this.maybeAwaitCompletion(options, callId, effectiveRingTimeout);
  }

  /**
   * When `options.wait` is set, register a completion promise keyed by the
   * carrier-issued `callId` and await it (bounded by a backstop timeout).
   * Otherwise resolve to `void` immediately (fire-and-forget).
   *
   * The registration happens here — after the carrier accepted the dial and
   * issued the id — so the future correlates to the right call. The race
   * window between `initiateCall` returning and this registration is
   * harmless: the callee is still ringing, so no terminal signal can fire
   * before we register. Mirrors the Python `call(wait=True)` tail block.
   */
  private async maybeAwaitCompletion(
    options: LocalCallOptions,
    callId: string,
    ringTimeout: number | null,
  ): Promise<CallResult | void> {
    if (!options.wait) return;
    const server = this.embeddedServer;
    if (!server || !callId) {
      // Should be unreachable — the precondition in call() threw when there
      // was no server, and both carrier branches set callId — but stay
      // defensive rather than await a promise that can never resolve.
      throw new PatterConnectionError(
        'call({ wait: true }): no active server or carrier call id.',
      );
    }
    const completion = server.registerCompletion(callId);
    // Backstop only — the real resolution comes from a carrier signal. Sized
    // at the ring window plus a generous in-call ceiling so a legitimately
    // long conversation is never cut short. Matches Python's
    // ``(ring_timeout or 25) + 1800``.
    const backstopMs = ((ringTimeout ?? 25) + 1800) * 1000;
    let timer: NodeJS.Timeout | undefined;
    const backstop = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        // Drop the dangling completion so a late signal can't resolve a
        // result nobody is awaiting.
        server.deleteCompletion(callId);
        reject(
          new PatterConnectionError(
            `call({ wait: true }): no terminal signal for call ${callId} ` +
              `within ${(backstopMs / 1000).toFixed(0)}s`,
            { code: ErrorCode.TIMEOUT },
          ),
        );
      }, backstopMs);
      timer.unref?.();
    });
    try {
      return await Promise.race([completion, backstop]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /**
   * Stop the embedded server and any running tunnel. Safe to call multiple
   * times. Leaves the instance reusable: a subsequent ``serve()`` works as
   * if the previous lifecycle never happened.
   *
   * Also clears any pending TTL eviction timers, awaits in-flight
   * prewarm-first-message synth tasks (best-effort, with a 1 s safety
   * timeout), and clears the prewarm cache. Without this a still-running
   * TTS WS keeps the user billed long after SDK teardown, and stale
   * entries leak across ``serve`` / ``disconnect`` cycles. See FIX #93.
   */
  async disconnect(): Promise<void> {
    // Clear pending TTL eviction timers and drain in-flight prewarm
    // synth tasks BEFORE tearing the server down so the synth tasks
    // observe a clean cancellation point and don't end up writing
    // bytes to a cache we're about to drop.
    for (const handle of this.prewarmTtlTimers.values()) {
      clearTimeout(handle);
    }
    this.prewarmTtlTimers.clear();
    if (this.prewarmTasks.size > 0) {
      // Promise.allSettled with a 1 s safety timeout — most synth tasks
      // observe their wait_for-style timer and return promptly; a
      // pathological hang must not block the disconnect path.
      const drain = Promise.allSettled(Array.from(this.prewarmTasks));
      const timer = new Promise<void>((resolve) =>
        setTimeout(resolve, 1_000).unref?.(),
      );
      await Promise.race([drain, timer]);
    }
    this.prewarmTasks.clear();
    this.prewarmAudio.clear();
    this.prewarmConsumed.clear();
    // Close every parked provider WS so we don't leak sockets across
    // ``serve`` / ``disconnect`` cycles (or process shutdown).
    for (const handle of this.prewarmedConnTimers.values()) {
      clearTimeout(handle);
    }
    this.prewarmedConnTimers.clear();
    for (const slot of this.prewarmedConnections.values()) {
      closeParkedConnections(slot);
    }
    this.prewarmedConnections.clear();
    if (this.tunnelHandle) {
      this.tunnelHandle.stop();
      this.tunnelHandle = null;
    }
    if (this.embeddedServer) {
      // Fail any in-flight call({ wait: true }) awaiters before the server
      // goes away — otherwise they'd hang until their backstop timeout since
      // no terminal signal can reach a stopped server. Mirrors the Python
      // disconnect() change.
      this.embeddedServer.failPendingCompletions(
        new PatterConnectionError(
          'Patter.disconnect() called while a call({ wait: true }) was still in flight.',
        ),
      );
      await this.embeddedServer.stop();
      this.embeddedServer = null;
    }
    // Clear tunnel-owned hostname so the next ``serve()`` does not trip the
    // ``Cannot use both tunnel: true and webhookUrl`` guard. Static / explicit
    // ``webhookUrl`` values stay in place — they were not ours to drop.
    if (this.tunnelOwnsWebhookUrl) {
      this.localConfig = { ...this.localConfig, webhookUrl: undefined };
      this.tunnelOwnsWebhookUrl = false;
    }
    // Recreate the deferred handles so a follow-up ``serve()`` can resolve
    // them again. Without this, the next ``await phone.ready`` returns the
    // stale hostname from the previous lifecycle.
    this._tunnelReady = new Promise<string>((resolve, reject) => {
      this._tunnelReadyResolve = resolve;
      this._tunnelReadyReject = reject;
    });
    this._tunnelReady.catch(() => {});
    if (this.localConfig.webhookUrl) {
      this._tunnelReadyResolve(this.localConfig.webhookUrl);
    }
    this._ready = new Promise<string>((resolve, reject) => {
      this._readyResolve = resolve;
      this._readyReject = reject;
    });
    this._ready.catch(() => {});
  }

  /**
   * Explicit-resource-management disposer so callers can write
   * ``await using phone = new Patter(...)`` and have {@link disconnect} run
   * automatically when the block exits — on the normal path AND when the
   * body throws. This guarantees the embedded server, any auto-started
   * tunnel, and in-flight prewarm/TTS work are torn down so a still-running
   * TTS WebSocket cannot keep the user billed after the block ends, and any
   * in-flight ``call({ wait: true })`` awaiter is failed rather than left
   * hanging. ``disconnect()`` is idempotent, so an explicit ``disconnect()``
   * inside the block is still safe. Mirrors Python's ``async with Patter(...)``.
   *
   * Note: this does NOT start the server (``serve()`` blocks until shutdown,
   * so it cannot run from a disposer) — call ``serve(...)`` inside the block:
   *
   * ```ts
   * await using phone = new Patter({ carrier: new Twilio(), phoneNumber: "+1555..." });
   * await phone.serve({ agent });               // inbound, or
   * const result = await phone.call({ to: "+1555...", agent, wait: true });
   * // disconnect() has run here — nothing left running.
   * ```
   */
  async [Symbol.asyncDispose](): Promise<void> {
    await this.disconnect();
  }

  /**
   * Terminate an active call on the configured carrier.
   *
   * Posts a hangup to the carrier (Twilio
   * ``Calls(callSid).update({status:'completed'})`` or Telnyx
   * ``/v2/calls/{callControlId}/actions/hangup``) so the bridge tears down
   * gracefully — the SDK's WebSocket handler then fires ``onCallEnd`` with
   * the final ``CallMetrics`` before the WS closes.
   *
   * Use this when the host application needs to end a call programmatically
   * without going through the LLM tool-call path (e.g. an admin override,
   * a watchdog, or an integration test runner).
   *
   * @param callSid - Carrier-issued call identifier (Twilio Call SID or
   *   Telnyx call_control_id) returned from a previous ``call(...)`` or
   *   captured in the ``onCallStart`` callback's payload.
   * @throws Error when ``callSid`` is empty or no carrier is configured.
   */
  async endCall(callSid: string): Promise<void> {
    if (!callSid) {
      throw new Error('callSid must be a non-empty string');
    }
    // If the call had a prewarmed first-message that was never consumed
    // (call ended before pickup), surface the wasted spend.
    this.recordPrewarmWaste(callSid);
    const carrier = this.localConfig.carrier;
    if (carrier.kind === 'twilio') {
      const auth = Buffer.from(`${carrier.accountSid}:${carrier.authToken}`).toString('base64');
      const url = `https://api.twilio.com/2010-04-01/Accounts/${carrier.accountSid}/Calls/${callSid}.json`;
      const body = new URLSearchParams({ Status: 'completed' });
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          Authorization: `Basic ${auth}`,
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body,
      });
      if (!res.ok) {
        throw new Error(`Twilio hangup failed: ${res.status} ${await res.text()}`);
      }
      return;
    }
    if (carrier.kind === 'telnyx') {
      const res = await fetch(`https://api.telnyx.com/v2/calls/${callSid}/actions/hangup`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${carrier.apiKey}`,
          'Content-Type': 'application/json',
        },
      });
      if (!res.ok) {
        throw new Error(`Telnyx hangup failed: ${res.status} ${await res.text()}`);
      }
      return;
    }
    if (carrier.kind === 'plivo') {
      const auth = Buffer.from(`${carrier.authId}:${carrier.authToken}`).toString('base64');
      const res = await fetch(
        `https://api.plivo.com/v1/Account/${carrier.authId}/Call/${encodeURIComponent(callSid)}/`,
        { method: 'DELETE', headers: { Authorization: `Basic ${auth}` } },
      );
      // Plivo returns 204 on success and 404 when the call already ended.
      if (!res.ok && res.status !== 404) {
        throw new Error(`Plivo hangup failed: ${res.status} ${await res.text()}`);
      }
      return;
    }
    throw new Error(`endCall() requires a configured carrier; got kind=${(carrier as { kind: string }).kind}`);
  }
}

/**
 * Wait for a freshly-minted cloudflared quick-tunnel hostname to be
 * publicly resolvable. Polls DNS until the OS resolver can resolve the
 * host (the same resolver path Twilio's edge will use), then adds a
 * small grace window for the cloudflared origin bridge to stabilise.
 *
 * Why DNS-only and not full HTTP: trycloudflare quick tunnels frequently
 * fail same-host loopback (the local machine resolving its own
 * tunnel back through Cloudflare's edge can race NAT / IPv4 vs IPv6
 * resolver paths) even when the URL is reachable from external hosts.
 * Twilio's edge resolves the hostname from public DNS — so DNS
 * resolution is the right proxy for "Twilio can reach us".
 *
 * Why a grace window: between "DNS resolves" and "cloudflared origin
 * bridge is ready to forward HTTP/WSS", there is a 1–4 s gap during
 * which Cloudflare returns 502 on HTTP and silently drops WSS upgrades.
 * The HTTP path is usually ready first; the WSS upgrade path takes
 * longer because it goes through a different cloudflared edge route.
 * Empirically 5 s covers >99 % of cases (was 2.5 s, dropped failure
 * rate from ~5 % to <1 % — see BUGS.md 2026-05-06 cartesia-openai-openai
 * attempt 1 entry).
 *
 * Without this guard, Twilio races the propagation and the first call
 * is silently torn down: HTTP webhooks succeed (`/voice` TwiML, AMD
 * callback) but Twilio's WSS upgrade for the media stream fails, the
 * call drops at pickup with no audio.
 */
async function waitForTunnelPubliclyReachable(
  hostname: string,
  totalTimeoutMs = 60_000,
  graceMs = 5_000,
): Promise<void> {
  const log = getLogger();
  const { Resolver } = await import('node:dns/promises');
  // Bypass the OS resolver (mDNSResponder on macOS aggressively caches
  // NXDOMAIN for several seconds, so the first lookup after a fresh
  // cloudflared tunnel comes up will keep returning ENOTFOUND long
  // after the public edge has the record). We query Cloudflare's
  // 1.1.1.1 + Google's 8.8.8.8 directly via c-ares — this is also the
  // exact resolver path Twilio's edge takes, so a positive result here
  // is a true proxy for "Twilio can reach us".
  //
  // ``timeout: 1500`` + ``tries: 1`` overrides c-ares's default of
  // 5000 ms × 4 attempts (= up to 20 s per resolve4 call) so the
  // outer retry loop actually retries — without this each NXDOMAIN
  // burns 5–20 s of wall-clock and the budget runs out after 1–2
  // attempts.
  const resolver = new Resolver({ timeout: 1500, tries: 1 });
  resolver.setServers(['1.1.1.1', '8.8.8.8']);
  const deadline = Date.now() + totalTimeoutMs;
  let attempt = 0;
  let lastErr: unknown;
  while (Date.now() < deadline) {
    attempt += 1;
    try {
      const records = await resolver.resolve4(hostname);
      const first = records[0] ?? '<unknown>';
      log.info('Tunnel DNS resolved → %s (attempt %d); waiting %d ms grace',
        first, attempt, graceMs);
      await new Promise((r) => setTimeout(r, graceMs));
      return;
    } catch (err) {
      lastErr = err;
    }
    // Backoff: 250 ms, 400 ms, 640 ms, 1.0 s, capped at 2 s.
    const delay = Math.min(250 * Math.pow(1.6, attempt - 1), 2_000);
    await new Promise((r) => setTimeout(r, delay));
  }
  throw new Error(
    `Tunnel hostname ${hostname} did not resolve within ${totalTimeoutMs}ms. ` +
    `Last error: ${lastErr instanceof Error ? lastErr.message : String(lastErr)}`,
  );
}

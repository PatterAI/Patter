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
import { ProvisionError } from "./errors";
import type { TunnelHandle } from "./tunnel";
import type {
  LocalOptions,
  LocalCallOptions,
  AgentOptions,
  ServeOptions,
} from "./types";
import { EmbeddedServer } from "./server";
import type { MetricsStore } from "./dashboard/store";
import { Carrier as TwilioCarrier } from "./telephony/twilio";
import { Carrier as TelnyxCarrier } from "./telephony/telnyx";
import { Realtime as OpenAIRealtime } from "./engines/openai";
import { ConvAI as ElevenLabsConvAI } from "./engines/elevenlabs";
import { CloudflareTunnel, Static as StaticTunnel } from "./tunnels";
import { getLogger } from "./logger";

/** Internal local-mode state — holds carrier + resolved runtime settings. */
export interface ResolvedLocalConfig {
  carrier: TwilioCarrier | TelnyxCarrier;
  phoneNumber: string;
  webhookUrl?: string;
  tunnel?: CloudflareTunnel | StaticTunnel | boolean;
  openaiKey?: string;
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
          'Pass `carrier: new Twilio({...})` or `carrier: new Telnyx({...})`.',
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
      if (engine instanceof OpenAIRealtime) {
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
          "Unknown engine. Expected OpenAIRealtime or ElevenLabsConvAI instance.",
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
    const telephonyProvider = carrier.kind === 'twilio' ? 'twilio' : 'telnyx';

    // Auto-configure the carrier so inbound calls hit this server without
    // manual Console setup. Mirrors Python's server.py start() flow.
    const { autoConfigureCarrier } = await import('./carrier-config');
    await autoConfigureCarrier({
      telephonyProvider,
      twilioSid: carrier.kind === 'twilio' ? carrier.accountSid : undefined,
      twilioToken: carrier.kind === 'twilio' ? carrier.authToken : undefined,
      telnyxKey: carrier.kind === 'telnyx' ? carrier.apiKey : undefined,
      telnyxConnectionId: carrier.kind === 'telnyx' ? carrier.connectionId : undefined,
      phoneNumber: this.localConfig.phoneNumber,
      webhookHost: webhookUrl,
    });

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

  /** Place an outbound call via the configured carrier. */
  async call(options: LocalCallOptions): Promise<void> {
    if (!options.to) {
      throw new Error("'to' phone number is required");
    }
    if (!options.to.startsWith('+')) {
      throw new Error(`'to' must be in E.164 format (e.g., '+1234567890'). Got: '${options.to}'`);
    }
    const { phoneNumber, webhookUrl, carrier } = this.localConfig;

    // Default ring timeout — 25 s limits phantom calls. Pass ``ringTimeout:
    // 60`` for legacy parity, or ``ringTimeout: null`` to omit and let the
    // carrier pick its own default.
    const effectiveRingTimeout: number | null =
      options.ringTimeout === undefined ? 25 : options.ringTimeout;

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
      if (this.embeddedServer) {
        try {
          const body = (await response.clone().json()) as { data?: { call_control_id?: string } };
          const callId = body.data?.call_control_id;
          if (callId) {
            this.embeddedServer.metricsStore.recordCallInitiated({
              call_id: callId,
              caller: phoneNumber,
              callee: options.to,
              direction: 'outbound',
            });
          }
        } catch {
          /* non-fatal */
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
    if (options.machineDetection) {
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
    if (this.embeddedServer) {
      try {
        const body = (await response.clone().json()) as {
          sid?: string;
          subresource_uris?: { notifications?: string };
        };
        const callSid = body.sid;
        if (callSid) {
          this.embeddedServer.metricsStore.recordCallInitiated({
            call_id: callSid,
            caller: phoneNumber,
            callee: options.to,
            direction: 'outbound',
          });
          const notificationsPath = body.subresource_uris?.notifications;
          if (notificationsPath) {
            getLogger().info(
              `Outbound call ${callSid} placed. ` +
                `Twilio notifications: https://api.twilio.com${notificationsPath} ` +
                '(check here if the call drops with no audio).',
            );
          }
        }
      } catch {
        /* non-fatal — the statusCallback will register anyway */
      }
    }
  }

  /**
   * Stop the embedded server and any running tunnel. Safe to call multiple
   * times. Leaves the instance reusable: a subsequent ``serve()`` works as
   * if the previous lifecycle never happened.
   */
  async disconnect(): Promise<void> {
    if (this.tunnelHandle) {
      this.tunnelHandle.stop();
      this.tunnelHandle = null;
    }
    if (this.embeddedServer) {
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
 * bridge is ready to forward HTTP", there is a 1–3 s gap during which
 * Cloudflare returns 502. Empirically 2.5 s covers >95 % of cases.
 *
 * Without this guard, Twilio races the propagation and the first call
 * is silently torn down by an HTTP 502 from the tunnel.
 */
async function waitForTunnelPubliclyReachable(
  hostname: string,
  totalTimeoutMs = 60_000,
  graceMs = 2_500,
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

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
import { Carrier as TwilioCarrier } from "./carriers/twilio";
import { Carrier as TelnyxCarrier } from "./carriers/telnyx";
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

export class Patter {
  private localConfig: ResolvedLocalConfig;
  private embeddedServer: EmbeddedServer | null = null;
  private tunnelHandle: TunnelHandle | null = null;

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
  }

  // === Agent definition ===

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
      // Parity with sdk-py: when the caller supplies any pipeline-mode piece
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

  async serve(opts: ServeOptions): Promise<void> {
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
      const { startTunnel } = await import('./tunnel');
      this.tunnelHandle = await startTunnel(port);
      webhookUrl = this.tunnelHandle.hostname;
      // Propagate the freshly-resolved webhook host into localConfig so a
      // subsequent call() in the same process reads the same hostname instead
      // of the original undefined value.
      this.localConfig = { ...this.localConfig, webhookUrl };
    }

    if (!webhookUrl) {
      throw new Error(
        'No webhookUrl configured. Either:\n' +
        '  - Pass webhookUrl in the Patter constructor\n' +
        '  - Use tunnel: true in serve() to auto-create a tunnel'
      );
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
    await this.embeddedServer.start(port);
  }

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

  async call(options: LocalCallOptions): Promise<void> {
    if (!options.to) {
      throw new Error("'to' phone number is required");
    }
    if (!options.to.startsWith('+')) {
      throw new Error(`'to' must be in E.164 format (e.g., '+1234567890'). Got: '${options.to}'`);
    }
    const { phoneNumber, webhookUrl, carrier } = this.localConfig;

    if (carrier.kind === 'telnyx') {
      // Telnyx outbound call via Call Control API.
      // Note: ``stream_url``/``stream_track`` are NOT accepted on
      // ``POST /v2/calls`` — Telnyx ignores them at dial time. Streaming is
      // started later via ``actions/streaming_start`` once the call is
      // answered. Mirrors ``sdk-py/getpatter/providers/telnyx_adapter.py``.
      const telnyxKey = carrier.apiKey;
      const connectionId = carrier.connectionId;

      const telnyxPayload: Record<string, unknown> = {
        connection_id: connectionId,
        from: phoneNumber,
        to: options.to,
      };
      if (options.ringTimeout !== undefined) {
        telnyxPayload.timeout_secs = Math.max(1, Math.floor(options.ringTimeout));
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
    const params = new URLSearchParams({
      To: options.to,
      From: phoneNumber,
      Url: `https://${webhookUrl}/webhooks/twilio/voice`,
      StatusCallback: statusCallbackUrl,
      StatusCallbackMethod: 'POST',
      // Full lifecycle so the dashboard sees ringing/no-answer/busy/failed
      // transitions even when media never arrives.
      StatusCallbackEvent: 'initiated ringing answered completed',
    });
    if (options.machineDetection) {
      params.append('MachineDetection', 'DetectMessageEnd');
      params.append('AsyncAmd', 'true');
      params.append('AsyncAmdStatusCallback', `https://${webhookUrl}/webhooks/twilio/amd`);
    }
    if (options.ringTimeout !== undefined) {
      params.append('Timeout', String(Math.max(1, Math.floor(options.ringTimeout))));
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
    if (this.embeddedServer) {
      try {
        const body = (await response.clone().json()) as { sid?: string };
        const callSid = body.sid;
        if (callSid) {
          this.embeddedServer.metricsStore.recordCallInitiated({
            call_id: callSid,
            caller: phoneNumber,
            callee: options.to,
            direction: 'outbound',
          });
        }
      } catch {
        /* non-fatal — the statusCallback will register anyway */
      }
    }
  }

  async disconnect(): Promise<void> {
    if (this.tunnelHandle) {
      this.tunnelHandle.stop();
      this.tunnelHandle = null;
    }
    if (this.embeddedServer) {
      await this.embeddedServer.stop();
      this.embeddedServer = null;
    }
  }
}

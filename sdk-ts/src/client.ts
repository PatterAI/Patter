import { PatterConnection } from "./connection";
import { PatterConnectionError, ProvisionError } from "./errors";
import type { TunnelHandle } from "./tunnel";
import type {
  PatterOptions,
  LocalOptions,
  ConnectOptions,
  CallOptions,
  LocalCallOptions,
  STTConfig,
  TTSConfig,
  CreateAgentOptions,
  Agent,
  PhoneNumber,
  Call,
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

const DEFAULT_BACKEND_URL = "wss://api.getpatter.com";
const DEFAULT_REST_URL = "https://api.getpatter.com";

function sttConfigToDict(cfg: STTConfig): Record<string, unknown> {
  const out: Record<string, unknown> = {
    provider: cfg.provider,
    api_key: cfg.apiKey,
    language: cfg.language,
  };
  if (cfg.options) out.options = { ...cfg.options };
  return out;
}

function ttsConfigToDict(cfg: TTSConfig): Record<string, unknown> {
  const out: Record<string, unknown> = {
    provider: cfg.provider,
    api_key: cfg.apiKey,
    voice: cfg.voice,
  };
  if (cfg.options) out.options = { ...cfg.options };
  return out;
}

/** Internal local-mode state — holds carrier + resolved runtime settings. */
export interface ResolvedLocalConfig {
  carrier: TwilioCarrier | TelnyxCarrier;
  phoneNumber: string;
  webhookUrl?: string;
  tunnel?: CloudflareTunnel | StaticTunnel | boolean;
  openaiKey?: string;
}

export class Patter {
  readonly apiKey: string;
  private readonly backendUrl: string;
  private readonly restUrl: string;
  private readonly connection: PatterConnection;
  private readonly mode: 'cloud' | 'local';
  private localConfig: ResolvedLocalConfig | null;
  private embeddedServer: EmbeddedServer | null = null;
  private tunnelHandle: TunnelHandle | null = null;

  constructor(options: PatterOptions | LocalOptions) {
    // Local mode is selected when a ``carrier`` instance is present or the
    // caller passes ``mode: 'local'`` explicitly.
    const hasCarrier =
      'carrier' in options &&
      (options as LocalOptions).carrier !== undefined;
    const isLocal =
      ('mode' in options && options.mode === 'local') || hasCarrier;

    if (isLocal) {
      const local = options as LocalOptions;

      if (!local.phoneNumber) {
        throw new Error('Local mode requires phoneNumber');
      }
      if (!local.carrier) {
        throw new Error(
          'Local mode requires a `carrier` instance. ' +
            'Pass `carrier: new Twilio({...})` or `carrier: new Telnyx({...})`.',
        );
      }

      const carrier = local.carrier;

      // Tunnel normalization — StaticTunnel's hostname becomes webhookUrl.
      const tunnel = local.tunnel;
      let tunnelWebhookUrl: string | undefined;
      if (tunnel instanceof StaticTunnel) {
        if (local.webhookUrl) {
          throw new Error(
            'Cannot use both `tunnel: new StaticTunnel(...)` and `webhookUrl`. ' +
              'Pick one.',
          );
        }
        tunnelWebhookUrl = tunnel.hostname;
      }

      this.mode = 'local';
      // Normalize webhookUrl: strip any http(s):// prefix and trailing slash
      // so downstream callers that prefix 'wss://' or 'https://' don't double-scheme.
      const rawWebhook = tunnelWebhookUrl ?? local.webhookUrl;
      const normalizedWebhook = rawWebhook
        ? rawWebhook.replace(/^https?:\/\//, '').replace(/\/$/, '')
        : undefined;

      this.localConfig = {
        carrier,
        phoneNumber: local.phoneNumber,
        webhookUrl: normalizedWebhook,
        tunnel: local.tunnel,
        openaiKey: local.openaiKey,
      };
      this.apiKey = '';
      this.backendUrl = DEFAULT_BACKEND_URL;
      this.restUrl = DEFAULT_REST_URL;
      this.connection = new PatterConnection('', DEFAULT_BACKEND_URL);
    } else {
      const cloudOpts = options as PatterOptions;
      this.mode = 'cloud';
      this.localConfig = null;
      this.apiKey = cloudOpts.apiKey;
      this.backendUrl = cloudOpts.backendUrl ?? DEFAULT_BACKEND_URL;
      this.restUrl = cloudOpts.restUrl ?? DEFAULT_REST_URL;
      this.connection = new PatterConnection(this.apiKey, this.backendUrl);
    }
  }

  // === Local mode ===

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
        if (this.localConfig && !this.localConfig.openaiKey) {
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

  async serve(opts: ServeOptions): Promise<void> {
    if (this.mode !== 'local' || !this.localConfig) {
      throw new Error('serve() is only available in local mode');
    }

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
    if (this.mode !== 'local') {
      throw new Error('test() is only available in local mode');
    }
    const { TestSession } = await import('./test-mode');
    const session = new TestSession();
    await session.run({
      agent: opts.agent,
      openaiKey: this.localConfig?.openaiKey,
      onMessage: typeof opts.onMessage === 'function' ? opts.onMessage : undefined,
      onCallStart: opts.onCallStart,
      onCallEnd: opts.onCallEnd,
    });
  }

  // === Cloud mode legacy ===

  async connect(options: ConnectOptions): Promise<void> {
    // Self-hosted: register number first
    if (options.provider && options.providerKey && options.number) {
      await this.registerNumber(
        options.provider,
        options.providerKey,
        options.number,
        options.providerSecret,
        options.country ?? "US",
        options.stt,
        options.tts
      );
    }

    await this.connection.connect({
      onMessage: options.onMessage,
      onCallStart: options.onCallStart,
      onCallEnd: options.onCallEnd,
    });
  }

  async call(options: CallOptions | LocalCallOptions): Promise<void> {
    if (this.mode === 'local') {
      const localOpts = options as LocalCallOptions;
      if (!localOpts.to) {
        throw new Error("'to' phone number is required");
      }
      if (!localOpts.to.startsWith('+')) {
        throw new Error(`'to' must be in E.164 format (e.g., '+1234567890'). Got: '${localOpts.to}'`);
      }
      if (!this.localConfig) {
        throw new Error('local config missing');
      }
      const { phoneNumber, webhookUrl, carrier } = this.localConfig;

      if (carrier.kind === 'telnyx') {
        // Telnyx outbound call via Call Control API
        const telnyxKey = carrier.apiKey;
        const connectionId = carrier.connectionId;
        const streamUrl =
          `wss://${webhookUrl}/ws/stream/${encodeURIComponent(localOpts.to)}` +
          `?caller=${encodeURIComponent(phoneNumber)}&callee=${encodeURIComponent(localOpts.to)}`;

        const telnyxPayload: Record<string, unknown> = {
          connection_id: connectionId,
          from: phoneNumber,
          to: localOpts.to,
          stream_url: streamUrl,
          stream_track: 'both_tracks',
        };
        if (localOpts.ringTimeout !== undefined) {
          telnyxPayload.timeout_secs = Math.max(1, Math.floor(localOpts.ringTimeout));
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
                callee: localOpts.to,
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
        To: localOpts.to,
        From: phoneNumber,
        Url: `https://${webhookUrl}/webhooks/twilio/voice`,
        StatusCallback: statusCallbackUrl,
        StatusCallbackMethod: 'POST',
        // Full lifecycle so the dashboard sees ringing/no-answer/busy/failed
        // transitions even when media never arrives.
        StatusCallbackEvent: 'initiated ringing answered completed',
      });
      if (localOpts.machineDetection) {
        params.append('MachineDetection', 'DetectMessageEnd');
        params.append('AsyncAmd', 'true');
        params.append('AsyncAmdStatusCallback', `https://${webhookUrl}/webhooks/twilio/amd`);
      }
      if (localOpts.ringTimeout !== undefined) {
        params.append('Timeout', String(Math.max(1, Math.floor(localOpts.ringTimeout))));
      }
      // Store voicemail message on the running server so AMD webhook can use it
      if (localOpts.voicemailMessage && this.embeddedServer) {
        this.embeddedServer.voicemailMessage = localOpts.voicemailMessage;
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
              callee: localOpts.to,
              direction: 'outbound',
            });
          }
        } catch {
          /* non-fatal — the statusCallback will register anyway */
        }
      }
      return;
    }

    const cloudOpts = options as CallOptions;
    if (!this.connection.isConnected) {
      if (cloudOpts.onMessage) {
        await this.connection.connect({ onMessage: cloudOpts.onMessage });
      } else {
        throw new PatterConnectionError(
          "Not connected. Call connect() first or pass onMessage."
        );
      }
    }

    await this.connection.requestCall(
      cloudOpts.fromNumber ?? "",
      cloudOpts.to,
      cloudOpts.firstMessage ?? ""
    );
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
    await this.connection.disconnect();
  }

  // === Agent Management ===

  async createAgent(opts: CreateAgentOptions): Promise<Agent> {
    const response = await fetch(`${this.restUrl}/api/agents`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": this.apiKey },
      body: JSON.stringify({
        name: opts.name, system_prompt: opts.systemPrompt,
        model: opts.model ?? "gpt-4o-mini-realtime-preview",
        voice: opts.voice ?? "alloy", voice_provider: opts.voiceProvider ?? "openai",
        language: opts.language ?? "en", first_message: opts.firstMessage ?? null,
        tools: opts.tools?.map(t => ({ name: t.name, description: t.description, parameters: t.parameters, webhook_url: t.webhookUrl })) ?? null,
      }),
    });
    if (response.status !== 201) throw new ProvisionError(`Failed to create agent: ${await response.text()}`);
    const data = await response.json() as { id: string; name: string; system_prompt: string; model: string; voice: string; voice_provider: string; language: string; first_message: string | null; tools: unknown };
    return { id: data.id, name: data.name, systemPrompt: data.system_prompt, model: data.model, voice: data.voice, voiceProvider: data.voice_provider, language: data.language, firstMessage: data.first_message, tools: data.tools as Agent['tools'] };
  }

  async listAgents(): Promise<Agent[]> {
    const response = await fetch(`${this.restUrl}/api/agents`, { headers: { "X-API-Key": this.apiKey } });
    if (!response.ok) throw new ProvisionError(`Failed to list agents: ${response.status}`);
    const data = await response.json() as Array<{ id: string; name: string; system_prompt: string; model: string; voice: string; voice_provider: string; language: string; first_message: string | null; tools: unknown }>;
    return data.map(a => ({ id: a.id, name: a.name, systemPrompt: a.system_prompt, model: a.model, voice: a.voice, voiceProvider: a.voice_provider, language: a.language, firstMessage: a.first_message, tools: a.tools as Agent['tools'] }));
  }

  async buyNumber(opts: { country?: string; provider?: string } = {}): Promise<PhoneNumber> {
    const response = await fetch(`${this.restUrl}/api/numbers/buy`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": this.apiKey },
      body: JSON.stringify({ country: opts.country ?? "US", provider: opts.provider ?? "twilio" }),
    });
    if (response.status !== 201) throw new ProvisionError(`Failed to buy number: ${await response.text()}`);
    const data = await response.json() as { id: string; number: string; provider: string; country: string; status: string; agent_id: string | null };
    return { id: data.id, number: data.number, provider: data.provider, country: data.country, status: data.status, agentId: data.agent_id };
  }

  async assignAgent(numberId: string, agentId: string): Promise<void> {
    const response = await fetch(`${this.restUrl}/api/phone-numbers/${numberId}/assign-agent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": this.apiKey },
      body: JSON.stringify({ agent_id: agentId }),
    });
    if (response.status !== 200) throw new ProvisionError(`Failed to assign agent: ${await response.text()}`);
  }

  async listCalls(limit: number = 50): Promise<Call[]> {
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
      throw new RangeError(`limit must be an integer between 1 and 1000, got ${limit}`);
    }
    const response = await fetch(`${this.restUrl}/api/calls?limit=${limit}`, { headers: { "X-API-Key": this.apiKey } });
    if (!response.ok) throw new ProvisionError(`Failed to list calls: ${response.status}`);
    const data = await response.json() as Array<{ id: string; direction: string; caller: string; callee: string; started_at: string; ended_at: string | null; duration_seconds: number | null; status: string; transcript: Call['transcript'] }>;
    return data.map(c => ({ id: c.id, direction: c.direction, caller: c.caller, callee: c.callee, startedAt: c.started_at, endedAt: c.ended_at, durationSeconds: c.duration_seconds, status: c.status, transcript: c.transcript }));
  }

  // Internal
  private async registerNumber(
    provider: string,
    providerKey: string,
    number: string,
    providerSecret?: string,
    country: string = "US",
    stt?: STTConfig,
    tts?: TTSConfig
  ): Promise<void> {
    const credentials: Record<string, string> = { api_key: providerKey };
    if (providerSecret) credentials.api_secret = providerSecret;

    const response = await fetch(`${this.restUrl}/api/phone-numbers`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": this.apiKey,
      },
      body: JSON.stringify({
        number,
        provider,
        provider_credentials: credentials,
        country,
        stt_config: stt ? (stt.toDict?.() ?? sttConfigToDict(stt)) : null,
        tts_config: tts ? (tts.toDict?.() ?? ttsConfigToDict(tts)) : null,
      }),
    });

    if (response.status === 409) return; // Already registered
    if (response.status !== 201) {
      throw new ProvisionError(
        `Failed to register number: ${await response.text()}`
      );
    }
  }
}

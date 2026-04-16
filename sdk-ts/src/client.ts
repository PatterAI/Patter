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
  Guardrail,
  ToolDefinition,
} from "./types";
import { deepgram, whisper, elevenlabs, openaiTts } from "./providers";
import { EmbeddedServer } from "./server";

const DEFAULT_BACKEND_URL = "wss://api.getpatter.com";
const DEFAULT_REST_URL = "https://api.getpatter.com";

export class Patter {
  readonly apiKey: string;
  private readonly backendUrl: string;
  private readonly restUrl: string;
  private readonly connection: PatterConnection;
  private readonly mode: 'cloud' | 'local';
  private readonly localConfig: LocalOptions | null;
  private embeddedServer: EmbeddedServer | null = null;
  private tunnelHandle: TunnelHandle | null = null;

  constructor(options: PatterOptions | LocalOptions) {
    if ('mode' in options && options.mode === 'local') {
      const local = options as LocalOptions;

      if (!local.phoneNumber) {
        throw new Error('Local mode requires phoneNumber');
      }
      if (!local.twilioSid && !local.telnyxKey) {
        throw new Error('Local mode requires twilioSid or telnyxKey');
      }
      if (local.twilioSid && !local.twilioToken) {
        throw new Error('twilioToken is required when using twilioSid');
      }

      this.mode = 'local';
      this.localConfig = options;
      // TODO: Remove beta warning when Telnyx is validated in production
      if (local.telnyxKey) {
        console.warn(
          '[patter] Telnyx support is in beta — tested locally but not yet validated in production. ' +
          'If you encounter issues, please report them at https://github.com/PatterAI/Patter/issues'
        );
      }
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
    // Validate provider
    if (opts.provider) {
      const valid = ['openai_realtime', 'elevenlabs_convai', 'pipeline'];
      if (!valid.includes(opts.provider)) {
        throw new Error(`provider must be one of: ${valid.join(', ')}. Got: '${opts.provider}'`);
      }
    }

    // Validate tools
    if (opts.tools) {
      if (!Array.isArray(opts.tools)) {
        throw new TypeError('tools must be an array');
      }
      opts.tools.forEach((tool, i) => {
        if (!tool.name) throw new Error(`tools[${i}] missing required 'name' field`);
        if (!tool.webhookUrl && !tool.handler) throw new Error(`tools[${i}] requires either 'webhookUrl' or 'handler'`);
      });
    }

    // Validate variables
    if (opts.variables !== undefined && (typeof opts.variables !== 'object' || Array.isArray(opts.variables))) {
      throw new TypeError('variables must be an object');
    }

    return { ...opts };
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

    // Resolve webhookUrl: tunnel or explicit
    let webhookUrl = this.localConfig.webhookUrl ?? '';
    const port = opts.port ?? 8000;

    if (opts.tunnel && webhookUrl) {
      throw new Error('Cannot use both tunnel: true and webhookUrl. Pick one.');
    }

    if (opts.tunnel) {
      const { startTunnel } = await import('./tunnel');
      this.tunnelHandle = await startTunnel(port);
      webhookUrl = this.tunnelHandle.hostname;
    }

    if (!webhookUrl) {
      throw new Error(
        'No webhookUrl configured. Either:\n' +
        '  - Pass webhookUrl in the Patter constructor\n' +
        '  - Use tunnel: true in serve() to auto-create a tunnel'
      );
    }

    this.embeddedServer = new EmbeddedServer(
      {
        twilioSid: this.localConfig.twilioSid,
        twilioToken: this.localConfig.twilioToken,
        openaiKey: this.localConfig.openaiKey,
        phoneNumber: this.localConfig.phoneNumber,
        webhookUrl,
        telephonyProvider: this.localConfig.telephonyProvider,
        telnyxKey: this.localConfig.telnyxKey,
        telnyxConnectionId: this.localConfig.telnyxConnectionId,
        telnyxPublicKey: this.localConfig.telnyxPublicKey,
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
      const { phoneNumber, webhookUrl, telephonyProvider } = this.localConfig;

      if (telephonyProvider === 'telnyx') {
        // Telnyx outbound call via Call Control API
        const telnyxKey = this.localConfig.telnyxKey ?? '';
        const connectionId = this.localConfig.telnyxConnectionId ?? '';
        const streamUrl =
          `wss://${webhookUrl}/ws/stream/${encodeURIComponent(localOpts.to)}` +
          `?caller=${encodeURIComponent(phoneNumber)}&callee=${encodeURIComponent(localOpts.to)}`;

        const response = await fetch('https://api.telnyx.com/v2/calls', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${telnyxKey}`,
          },
          body: JSON.stringify({
            connection_id: connectionId,
            from: phoneNumber,
            to: localOpts.to,
            stream_url: streamUrl,
            stream_track: 'both_tracks',
          }),
        });
        if (!response.ok) {
          throw new ProvisionError(`Failed to initiate Telnyx call: ${await response.text()}`);
        }
        return;
      }

      // Default: Twilio
      const twilioSid = this.localConfig.twilioSid ?? '';
      const twilioToken = this.localConfig.twilioToken ?? '';
      const statusCallbackUrl = `https://${webhookUrl}/webhooks/twilio/status`;
      const url = `https://api.twilio.com/2010-04-01/Accounts/${twilioSid}/Calls.json`;
      const params = new URLSearchParams({
        To: localOpts.to,
        From: phoneNumber,
        Url: `https://${webhookUrl}/webhooks/twilio/voice`,
        StatusCallback: statusCallbackUrl,
        StatusCallbackMethod: 'POST',
      });
      if (localOpts.machineDetection) {
        params.append('MachineDetection', 'DetectMessageEnd');
        params.append('AsyncAmd', 'true');
        params.append('AsyncAmdStatusCallback', `https://${webhookUrl}/webhooks/twilio/amd`);
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

  // Provider helpers
  static deepgram = deepgram;
  static whisper = whisper;
  static elevenlabs = elevenlabs;
  static openaiTts = openaiTts;

  static guardrail(opts: {
    name: string;
    blockedTerms?: string[];
    check?: (text: string) => boolean;
    replacement?: string;
  }): Guardrail {
    return {
      name: opts.name,
      blockedTerms: opts.blockedTerms,
      check: opts.check,
      replacement: opts.replacement ?? "I'm sorry, I can't respond to that.",
    };
  }

  /**
   * Create a tool definition for use with `agent({ tools: [...] })`.
   *
   * Either `handler` (a function) or `webhookUrl` must be provided.
   *
   * @param opts.name - Tool name (visible to the LLM).
   * @param opts.description - What the tool does (visible to the LLM).
   * @param opts.parameters - JSON Schema for tool arguments.
   * @param opts.handler - Async function called in-process when the LLM invokes the tool.
   * @param opts.webhookUrl - URL to POST to when the LLM invokes the tool.
   *
   * @example
   * ```ts
   * phone.agent({
   *   systemPrompt: 'You are a pizza bot.',
   *   tools: [
   *     Patter.tool({
   *       name: 'check_menu',
   *       description: 'Check available menu items',
   *       handler: async (args) => JSON.stringify({ items: ['margherita'] }),
   *     }),
   *   ],
   * });
   * ```
   */
  static tool(opts: {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
    handler?: (args: Record<string, unknown>, context: Record<string, unknown>) => Promise<string>;
    webhookUrl?: string;
  }): ToolDefinition {
    if (!opts.handler && !opts.webhookUrl) {
      throw new Error('tool() requires either handler or webhookUrl');
    }
    const t: ToolDefinition = {
      name: opts.name,
      description: opts.description ?? '',
      parameters: opts.parameters ?? { type: 'object', properties: {} },
    };
    if (opts.handler) {
      t.handler = opts.handler;
    }
    if (opts.webhookUrl) {
      t.webhookUrl = opts.webhookUrl;
    }
    return t;
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
        stt_config: stt?.toDict() ?? null,
        tts_config: tts?.toDict() ?? null,
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

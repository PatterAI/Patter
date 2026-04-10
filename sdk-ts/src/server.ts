import crypto from 'node:crypto';
import express from 'express';
import { createServer, Server as HTTPServer } from 'http';
import { WebSocketServer, WebSocket as WSWebSocket } from 'ws';
import { OpenAIRealtimeAdapter } from './providers/openai-realtime';
import { ElevenLabsConvAIAdapter } from './providers/elevenlabs-convai';
import { DeepgramSTT } from './providers/deepgram-stt';
import { WhisperSTT } from './providers/whisper-stt';
import { CallMetricsAccumulator } from './metrics';
import { mergePricing } from './pricing';
import { MetricsStore } from './dashboard/store';
import { mountDashboard, mountApi } from './dashboard/routes';
import { RemoteMessageHandler } from './remote-message';
import { StreamHandler } from './stream-handler';
import { getLogger } from './logger';
import type { TelephonyBridge } from './stream-handler';
import type { AgentOptions, PipelineMessageHandler } from './types';

export interface LocalConfig {
  twilioSid?: string;
  twilioToken?: string;
  openaiKey?: string;
  phoneNumber: string;
  webhookUrl: string;
  telephonyProvider?: 'twilio' | 'telnyx';
  telnyxKey?: string;
  telnyxConnectionId?: string;
  /**
   * Telnyx Ed25519 public key (base64-encoded, DER/SPKI format) used to verify
   * incoming webhook signatures. Obtain from the Telnyx portal under
   * API Keys → Webhook Keys. When provided, unauthenticated webhook requests
   * are rejected with HTTP 403.
   */
  telnyxPublicKey?: string;
}

type AIAdapter = OpenAIRealtimeAdapter | ElevenLabsConvAIAdapter;

const TRANSFER_CALL_TOOL = {
  name: 'transfer_call',
  description: 'Transfer the call to a human agent at the specified phone number',
  parameters: {
    type: 'object' as const,
    properties: {
      number: {
        type: 'string',
        description: 'Phone number to transfer to (E.164 format)',
      },
    },
    required: ['number'],
  },
};

const END_CALL_TOOL = {
  name: 'end_call',
  description: 'End the current phone call. Use when the conversation is complete or the user says goodbye.',
  parameters: {
    type: 'object' as const,
    properties: {
      reason: {
        type: 'string',
        description: "Reason for ending the call (e.g., 'conversation_complete', 'user_requested', 'no_response')",
      },
    },
  },
};

/**
 * Escape a string for safe inclusion inside XML/HTML attributes or text nodes.
 */
function xmlEscape(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

/**
 * Validate that a webhook URL is safe to fetch (SSRF protection).
 * Blocks private/internal IP ranges and non-HTTP(S) schemes.
 */
export function validateWebhookUrl(url: string): void {
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error(`Invalid webhook URL scheme: ${parsed.protocol}`);
  }
  const hostname = parsed.hostname;
  const blocked = [
    /^127\./,
    /^10\./,
    /^172\.(1[6-9]|2\d|3[01])\./,
    /^192\.168\./,
    /^169\.254\./,
    /^0\./,
    /^::1$/,
    /^localhost$/i,
    /^metadata\.google\.internal$/i,
  ];
  if (blocked.some((re) => re.test(hostname))) {
    throw new Error(`Webhook URL blocked: ${hostname} is a private/internal address`);
  }
}

/**
 * Validate a Telnyx webhook request signature using Ed25519.
 *
 * Telnyx signs the raw request body with an Ed25519 private key and includes
 * the base64-encoded signature in the ``telnyx-signature-ed25519`` header and
 * a Unix millisecond timestamp in ``telnyx-timestamp``.
 *
 * The signed payload is: timestamp + "|" + rawBody
 *
 * @param rawBody     Raw (unparsed) request body string
 * @param signature   Value of the ``telnyx-signature-ed25519`` header
 * @param timestamp   Value of the ``telnyx-timestamp`` header
 * @param publicKey   Ed25519 public key provided by Telnyx (base64-encoded)
 * @param toleranceSec Maximum age of the request in seconds (default 300)
 * @returns true if valid, false otherwise
 */
function validateTelnyxSignature(
  rawBody: string,
  signature: string,
  timestamp: string,
  publicKey: string,
  toleranceSec = 300,
): boolean {

  try {
    // Reject if timestamp is missing or too old (replay attack protection)
    const ts = parseInt(timestamp, 10);
    if (!Number.isFinite(ts)) return false;
    const ageMs = Date.now() - ts;
    if (ageMs < 0 || ageMs > toleranceSec * 1000) return false;

    const payload = `${timestamp}|${rawBody}`;
    const keyBuffer = Buffer.from(publicKey, 'base64');
    const sigBuffer = Buffer.from(signature, 'base64');

    // Node 15+ supports Ed25519 natively via createPublicKey / verify
    const keyObject = crypto.createPublicKey({
      key: keyBuffer,
      format: 'der',
      type: 'spki',
    });
    return crypto.verify(null, Buffer.from(payload), keyObject, sigBuffer);
  } catch {
    return false;
  }
}

/**
 * Validate a Twilio webhook request signature using HMAC-SHA1.
 * Returns true if the signature is valid, false otherwise.
 */
function validateTwilioSignature(
  url: string,
  params: Record<string, string>,
  signature: string,
  authToken: string,
): boolean {

  const data = url + Object.keys(params).sort().reduce((acc, key) => acc + key + (params[key] ?? ''), '');
  const expected = crypto.createHmac('sha1', authToken).update(data).digest('base64');
  try {
    return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
  } catch {
    return false;
  }
}

/**
 * Sanitise an untrusted key/value map by stripping keys that could enable
 * prototype pollution (__proto__, constructor, prototype) and ensuring all
 * values are strings. Returns a clean plain object with no inherited props.
 */
export function sanitizeVariables(raw: Record<string, unknown>): Record<string, string> {
  const BLOCKED_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
  const safe: Record<string, string> = Object.create(null);
  for (const key of Object.keys(raw)) {
    if (BLOCKED_KEYS.has(key)) continue;
    const val = raw[key];
    safe[key] = typeof val === 'string' ? val : String(val ?? '');
  }
  return safe;
}

/**
 * Replace ``{key}`` placeholders in a template string with values from the
 * provided variables map.
 */
export function resolveVariables(template: string, variables: Record<string, string>): string {
  let result = template;
  for (const [key, value] of Object.entries(variables)) {
    result = result.replaceAll(`{${key}}`, value);
  }
  return result;
}

/**
 * Build an AI adapter (OpenAI Realtime or ElevenLabs ConvAI) for a call.
 */
export function buildAIAdapter(config: LocalConfig, agent: AgentOptions, resolvedPrompt?: string): AIAdapter {
  if (agent.provider === 'elevenlabs_convai') {
    const key = agent.elevenlabsKey ?? '';
    return new ElevenLabsConvAIAdapter(
      key,
      agent.elevenlabsAgentId ?? '',
      agent.voice ?? '21m00Tcm4TlvDq8ikWAM',
      'eleven_turbo_v2_5',
      agent.language ?? 'en',
      agent.firstMessage ?? '',
    );
  }
  // Always inject transfer_call and end_call system tools alongside agent-defined tools
  const agentTools = agent.tools?.map((t) => ({
    name: t.name,
    description: t.description,
    parameters: t.parameters,
  })) ?? [];
  const tools = [...agentTools, TRANSFER_CALL_TOOL, END_CALL_TOOL];
  return new OpenAIRealtimeAdapter(
    config.openaiKey ?? '',
    agent.model,
    agent.voice,
    resolvedPrompt ?? agent.systemPrompt,
    tools,
  );
}

// ---------------------------------------------------------------------------
// Telephony bridge implementations
// ---------------------------------------------------------------------------

/** Twilio-specific telephony bridge. */
class TwilioBridge implements TelephonyBridge {
  readonly label = 'Twilio';
  readonly telephonyProvider = 'twilio' as const;

  constructor(private readonly config: LocalConfig) {}

  sendAudio(ws: WSWebSocket, audioBase64: string, streamSid: string): void {
    ws.send(JSON.stringify({ event: 'media', streamSid, media: { payload: audioBase64 } }));
  }

  sendMark(ws: WSWebSocket, markName: string, streamSid: string): void {
    ws.send(JSON.stringify({ event: 'mark', streamSid, mark: { name: markName } }));
  }

  sendClear(ws: WSWebSocket, streamSid: string): void {
    ws.send(JSON.stringify({ event: 'clear', streamSid }));
  }

  async transferCall(callId: string, toNumber: string): Promise<void> {
    if (this.config.twilioSid && this.config.twilioToken && callId) {
      const transferUrl = `https://api.twilio.com/2010-04-01/Accounts/${this.config.twilioSid}/Calls/${callId}.json`;
      await fetch(transferUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Authorization': `Basic ${Buffer.from(`${this.config.twilioSid}:${this.config.twilioToken}`).toString('base64')}`,
        },
        body: new URLSearchParams({ Twiml: `<Response><Dial>${xmlEscape(toNumber)}</Dial></Response>` }).toString(),
      });
      getLogger().info(`Call transferred to ${toNumber}`);
    }
  }

  async endCall(callId: string, _ws: WSWebSocket): Promise<void> {
    if (this.config.twilioSid && this.config.twilioToken && callId) {
      const endUrl = `https://api.twilio.com/2010-04-01/Accounts/${this.config.twilioSid}/Calls/${callId}.json`;
      await fetch(endUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Authorization': `Basic ${Buffer.from(`${this.config.twilioSid}:${this.config.twilioToken}`).toString('base64')}`,
        },
        body: new URLSearchParams({ Status: 'completed' }).toString(),
      });
    }
  }

  createStt(agent: AgentOptions): DeepgramSTT | WhisperSTT | null {
    if (agent.stt) {
      if (agent.stt.provider === 'deepgram') {
        return DeepgramSTT.forTwilio(agent.stt.apiKey, agent.stt.language ?? 'en');
      } else if (agent.stt.provider === 'whisper') {
        return WhisperSTT.forTwilio(agent.stt.apiKey, agent.stt.language ?? 'en');
      }
    } else if (agent.deepgramKey) {
      return DeepgramSTT.forTwilio(agent.deepgramKey, agent.language ?? 'en');
    }
    return null;
  }

  async queryTelephonyCost(metricsAcc: CallMetricsAccumulator, callId: string): Promise<void> {
    if (this.config.twilioSid && this.config.twilioToken && callId) {
      try {
        const resp = await fetch(
          `https://api.twilio.com/2010-04-01/Accounts/${this.config.twilioSid}/Calls/${callId}.json`,
          {
            headers: {
              'Authorization': `Basic ${Buffer.from(`${this.config.twilioSid}:${this.config.twilioToken}`).toString('base64')}`,
            },
            signal: AbortSignal.timeout(5000),
          },
        );
        if (resp.ok) {
          const data = await resp.json() as { price?: string };
          if (data.price != null) {
            metricsAcc.setActualTelephonyCost(Math.abs(parseFloat(data.price)));
            getLogger().info(`Twilio actual cost: $${Math.abs(parseFloat(data.price))}`);
          }
        }
      } catch {
        // Fallback to estimated cost
      }
    }
  }
}

/** Telnyx-specific telephony bridge. */
class TelnyxBridge implements TelephonyBridge {
  readonly label = 'Telnyx';
  readonly telephonyProvider = 'telnyx' as const;

  constructor(private readonly config: LocalConfig) {}

  sendAudio(ws: WSWebSocket, audioBase64: string, _streamSid: string): void {
    ws.send(JSON.stringify({ event_type: 'media', payload: { audio: { chunk: audioBase64 } } }));
  }

  sendMark(_ws: WSWebSocket, _markName: string, _streamSid: string): void {
    // Telnyx does not support mark events — no-op
  }

  sendClear(ws: WSWebSocket, _streamSid: string): void {
    ws.send(JSON.stringify({ event_type: 'media_stop' }));
  }

  async transferCall(callId: string, toNumber: string): Promise<void> {
    const telnyxKey = this.config.telnyxKey ?? '';
    await fetch(`https://api.telnyx.com/v2/calls/${callId}/actions/transfer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
      body: JSON.stringify({ to: toNumber }),
    });
    getLogger().info(`Telnyx call transferred to ${toNumber}`);
  }

  async endCall(_callId: string, ws: WSWebSocket): Promise<void> {
    ws.close();
  }

  createStt(agent: AgentOptions): DeepgramSTT | WhisperSTT | null {
    if (agent.stt) {
      if (agent.stt.provider === 'deepgram') {
        // Telnyx sends 16 kHz PCM — use linear16 encoding
        return new DeepgramSTT(agent.stt.apiKey, agent.stt.language ?? 'en', 'nova-3', 'linear16', 16000);
      } else if (agent.stt.provider === 'whisper') {
        return new WhisperSTT(agent.stt.apiKey, 'whisper-1', agent.stt.language ?? 'en');
      }
    } else if (agent.deepgramKey) {
      // Telnyx sends 16 kHz PCM — use linear16 encoding
      return new DeepgramSTT(agent.deepgramKey, agent.language ?? 'en', 'nova-3', 'linear16', 16000);
    }
    return null;
  }

  async queryTelephonyCost(metricsAcc: CallMetricsAccumulator, callId: string): Promise<void> {
    if (this.config.telnyxKey && callId) {
      try {
        const resp = await fetch(
          `https://api.telnyx.com/v2/calls/${callId}`,
          {
            headers: { 'Authorization': `Bearer ${this.config.telnyxKey}` },
            signal: AbortSignal.timeout(5000),
          },
        );
        if (resp.ok) {
          const body = await resp.json() as { data?: { cost?: { amount?: string } } };
          const amount = body.data?.cost?.amount;
          if (amount != null) {
            metricsAcc.setActualTelephonyCost(Math.abs(parseFloat(amount)));
            getLogger().info(`Telnyx actual cost: $${Math.abs(parseFloat(amount))}`);
          }
        }
      } catch {
        // Fallback to estimated cost
      }
    }
  }
}

// ---------------------------------------------------------------------------
// EmbeddedServer
// ---------------------------------------------------------------------------

/** Maximum seconds to wait for active calls to finish during graceful shutdown. */
const GRACEFUL_SHUTDOWN_TIMEOUT_MS = 10_000;

export class EmbeddedServer {
  private server: HTTPServer | null = null;
  private wss: WebSocketServer | null = null;
  private twilioTokenWarningLogged = false;
  private readonly metricsStore: MetricsStore;
  private readonly pricing: ReturnType<typeof mergePricing>;
  private readonly remoteHandler = new RemoteMessageHandler();

  /** Active WebSocket connections tracked for graceful shutdown. */
  private readonly activeConnections = new Set<WSWebSocket>();

  constructor(
    private readonly config: LocalConfig,
    private readonly agent: AgentOptions,
    public onCallStart?: (data: Record<string, unknown>) => Promise<void>,
    public onCallEnd?: (data: Record<string, unknown>) => Promise<void>,
    public onTranscript?: (data: Record<string, unknown>) => Promise<void>,
    public onMessage?: PipelineMessageHandler | string,
    private readonly recording: boolean = false,
    public voicemailMessage: string = '',
    public onMetrics?: (data: Record<string, unknown>) => Promise<void>,
    pricingOverrides?: Record<string, Record<string, unknown>>,
    private readonly dashboard: boolean = true,
    private readonly dashboardToken: string = '',
  ) {
    this.metricsStore = new MetricsStore();
    this.pricing = mergePricing(pricingOverrides as Record<string, { unit?: string; price?: number }> | undefined);
  }

  async start(port: number = 8000): Promise<void> {
    const webhookUrlPattern = /^[a-zA-Z0-9][a-zA-Z0-9.\-]+[a-zA-Z0-9]$/;
    if (!webhookUrlPattern.test(this.config.webhookUrl)) {
      throw new Error(`Invalid webhookUrl: must be a hostname with no protocol prefix or path (got: '${this.config.webhookUrl}')`);
    }

    const app = express();
    // Capture raw body for Telnyx signature verification before JSON parsing.
    // The rawBody property is attached to the request object when needed.
    app.use((req, _res, next) => {
      if (req.path === '/webhooks/telnyx/voice') {
        let raw = '';
        req.setEncoding('utf8');
        req.on('data', (chunk: string) => { raw += chunk; });
        req.on('end', () => {
          (req as express.Request & { rawBody?: string }).rawBody = raw;
          try {
            (req as express.Request & { body?: unknown }).body = JSON.parse(raw);
          } catch {
            (req as express.Request & { body?: unknown }).body = {};
          }
          next();
        });
      } else {
        next();
      }
    });
    app.use(express.json());
    app.use(express.urlencoded({ extended: true }));

    app.get('/health', (_req, res) => {
      res.json({ status: 'ok', mode: 'local' });
    });

    // Mount dashboard and B2B API routes
    if (this.dashboard) {
      if (!this.dashboardToken) {
        getLogger().warn(
          'Dashboard is enabled without authentication. ' +
          'Set dashboardToken to protect call data. ' +
          'This is safe for local development but should not be exposed on a public network.'
        );
      }
      mountDashboard(app, this.metricsStore, this.dashboardToken);
      mountApi(app, this.metricsStore, this.dashboardToken);
      getLogger().info('Dashboard: http://127.0.0.1:' + port + '/dashboard');
    }

    app.post('/webhooks/twilio/recording', (req, res) => {
      if (this.config.twilioToken) {
        const signature = (req.headers['x-twilio-signature'] as string) || '';
        const url = `https://${this.config.webhookUrl}${req.originalUrl}`;
        const params = (req.body ?? {}) as Record<string, string>;
        if (!validateTwilioSignature(url, params, signature, this.config.twilioToken)) {
          res.status(403).send('Invalid signature');
          return;
        }
      }
      const body = req.body as Record<string, string>;
      const recordingSid = body['RecordingSid'] ?? '';
      const recordingUrl = body['RecordingUrl'] ?? '';
      const callSid = body['CallSid'] ?? '';
      getLogger().info(`Recording ${recordingSid} for call ${callSid}: ${recordingUrl}`);
      res.status(204).send();
    });

    app.post('/webhooks/twilio/amd', async (req, res) => {
      if (this.config.twilioToken) {
        const signature = (req.headers['x-twilio-signature'] as string) || '';
        const url = `https://${this.config.webhookUrl}${req.originalUrl}`;
        const params = (req.body ?? {}) as Record<string, string>;
        if (!validateTwilioSignature(url, params, signature, this.config.twilioToken)) {
          res.status(403).send('Invalid signature');
          return;
        }
      }
      const body = req.body as Record<string, string>;
      const answeredBy = body['AnsweredBy'] ?? '';
      const callSid = body['CallSid'] ?? '';
      getLogger().info(`AMD result for ${callSid}: ${answeredBy}`);

      if (
        (answeredBy === 'machine_end_beep' || answeredBy === 'machine_end_silence') &&
        this.voicemailMessage &&
        this.config.twilioSid &&
        this.config.twilioToken
      ) {
        const twiml = `<Response><Say>${xmlEscape(this.voicemailMessage)}</Say><Hangup/></Response>`;
        try {
          const vmUrl = `https://api.twilio.com/2010-04-01/Accounts/${this.config.twilioSid}/Calls/${callSid}.json`;
          const vmResp = await fetch(vmUrl, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/x-www-form-urlencoded',
              'Authorization': `Basic ${Buffer.from(`${this.config.twilioSid}:${this.config.twilioToken}`).toString('base64')}`,
            },
            body: new URLSearchParams({ Twiml: twiml }).toString(),
          });
          if (vmResp.ok) {
            getLogger().info(`Voicemail dropped for ${callSid}`);
          } else {
            getLogger().warn(`Could not drop voicemail: ${await vmResp.text()}`);
          }
        } catch (e) {
          getLogger().warn(`Could not drop voicemail: ${String(e)}`);
        }
      }

      res.status(204).send();
    });

    app.post('/webhooks/twilio/voice', (req, res) => {
      if (this.config.twilioToken) {
        const signature = (req.headers['x-twilio-signature'] as string) || '';
        const url = `https://${this.config.webhookUrl}${req.originalUrl}`;
        const params = (req.body ?? {}) as Record<string, string>;
        if (!validateTwilioSignature(url, params, signature, this.config.twilioToken)) {
          res.status(403).send('Invalid signature');
          return;
        }
      } else if (!this.twilioTokenWarningLogged) {
        this.twilioTokenWarningLogged = true;
        getLogger().warn('Twilio webhook signature validation disabled — set twilioToken for production');
      }
      const callSid = (req.body.CallSid as string) || '';
      const caller = (req.body.From as string) || '';
      const callee = (req.body.To as string) || '';
      const rawStreamUrl = `wss://${this.config.webhookUrl}/ws/stream/${callSid}?caller=${encodeURIComponent(caller)}&callee=${encodeURIComponent(callee)}`;
      const xmlStreamUrl = xmlEscape(rawStreamUrl);
      const twiml = `<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="${xmlStreamUrl}"/></Connect></Response>`;
      res.type('text/xml').send(twiml);
    });

    app.post('/webhooks/telnyx/voice', (req, res) => {
      // Enforce Ed25519 signature verification when a public key is configured.
      if (this.config.telnyxPublicKey) {
        const rawBody = (req as express.Request & { rawBody?: string }).rawBody ?? '';
        const signature = (req.headers['telnyx-signature-ed25519'] as string) ?? '';
        const timestamp = (req.headers['telnyx-timestamp'] as string) ?? '';
        if (!signature || !timestamp || !validateTelnyxSignature(rawBody, signature, timestamp, this.config.telnyxPublicKey)) {
          getLogger().warn('Telnyx webhook rejected: invalid or missing Ed25519 signature');
          return res.status(403).send('Invalid signature');
        }
      } else {
        getLogger().warn('Telnyx webhook signature verification is disabled. Set telnyxPublicKey in LocalOptions for production use.');
      }

      const body = req.body as {
        data?: {
          event_type?: string;
          payload?: {
            call_control_id?: string;
            from?: string;
            to?: string;
          };
        };
      };

      if (typeof body?.data !== 'object' || body.data === null || Array.isArray(body.data)) {
        return res.status(400).send('Invalid body');
      }
      if (typeof body.data.event_type !== 'string' || typeof body.data.payload !== 'object' || body.data.payload === null) {
        return res.status(400).send('Invalid body');
      }

      const eventType = body?.data?.event_type ?? '';

      if (eventType === 'call.initiated') {
        const payload = body?.data?.payload ?? {};
        const callControlId = payload.call_control_id ?? '';
        const caller = payload.from ?? '';
        const callee = payload.to ?? '';
        const streamUrl =
          `wss://${this.config.webhookUrl}/ws/stream/${encodeURIComponent(callControlId)}` +
          `?caller=${encodeURIComponent(caller)}&callee=${encodeURIComponent(callee)}`;

        const commands = [
          { command: 'answer' },
          {
            command: 'stream_start',
            params: {
              stream_url: streamUrl,
              stream_track: 'both_tracks',
            },
          },
        ];

        res.json({ commands });
      } else {
        // Acknowledge other Telnyx webhook events
        res.json({ received: true });
      }
    });

    this.server = createServer(app);
    this.wss = new WebSocketServer({ noServer: true });

    // Per-IP WebSocket connection counter for DoS protection.
    // Telephony providers (Twilio/Telnyx) only open 1 connection per call;
    // a limit of 10 concurrent connections per IP is generous but blocks abuse.
    const MAX_WS_PER_IP = 10;
    const wsConnectionsByIp = new Map<string, number>();

    this.server.on('upgrade', (req, socket, head) => {
      const remoteIp = (req.socket?.remoteAddress ?? 'unknown').replace(/^::ffff:/, '');
      const currentCount = wsConnectionsByIp.get(remoteIp) ?? 0;
      if (currentCount >= MAX_WS_PER_IP) {
        getLogger().warn(`WebSocket upgrade rejected: too many connections from ${remoteIp}`);
        socket.write('HTTP/1.1 429 Too Many Requests\r\n\r\n');
        socket.destroy();
        return;
      }
      getLogger().info(`Upgrade request: ${req.url}`);
      this.wss!.handleUpgrade(req, socket, head, (ws) => {
        wsConnectionsByIp.set(remoteIp, (wsConnectionsByIp.get(remoteIp) ?? 0) + 1);
        ws.once('close', () => {
          const count = (wsConnectionsByIp.get(remoteIp) ?? 1) - 1;
          if (count <= 0) {
            wsConnectionsByIp.delete(remoteIp);
          } else {
            wsConnectionsByIp.set(remoteIp, count);
          }
        });
        this.wss!.emit('connection', ws, req);
      });
    });

    this.wss.on('connection', (ws, req) => {
      const url = new URL((req as { url?: string }).url ?? '', `http://localhost`);
      getLogger().info(`WebSocket connected: ${(req as { url?: string }).url}`);

      // Track active connections for graceful shutdown
      this.activeConnections.add(ws);
      ws.once('close', () => {
        this.activeConnections.delete(ws);
      });

      const isTelnyx = this.config.telephonyProvider === 'telnyx';
      if (isTelnyx) {
        this.handleTelnyxStream(ws, url);
      } else {
        this.handleTwilioStream(ws, url);
      }
    });

    await new Promise<void>((resolve) => {
      this.server!.listen(port, '0.0.0.0', () => {
        getLogger().info(`
██████╗  █████╗ ████████╗████████╗███████╗██████╗
██╔══██╗██╔══██╗╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
██████╔╝███████║   ██║      ██║   █████╗  ██████╔╝
██╔═══╝ ██╔══██║   ██║      ██║   ██╔══╝  ██╔══██╗
██║     ██║  ██║   ██║      ██║   ███████╗██║  ██║
╚═╝     ╚═╝  ╚═╝   ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝

Connect AI agents to phone numbers with ~10 lines of code
`);
        getLogger().info(`Server on port ${port}`);
        getLogger().info(`Webhook: https://${this.config.webhookUrl}`);
        getLogger().info(`Phone: ${this.config.phoneNumber}`);
        resolve();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Stream handler helpers
  // ---------------------------------------------------------------------------

  /** Build the shared StreamHandlerDeps for the current server configuration. */
  private buildStreamHandlerDeps(bridge: TelephonyBridge): import('./stream-handler').StreamHandlerDeps {
    return {
      config: this.config,
      agent: this.agent,
      bridge,
      metricsStore: this.metricsStore,
      pricing: this.pricing,
      remoteHandler: this.remoteHandler,
      onCallStart: this.onCallStart,
      onCallEnd: this.onCallEnd,
      onTranscript: this.onTranscript,
      onMessage: this.onMessage,
      onMetrics: this.onMetrics,
      recording: this.recording,
      buildAIAdapter: (resolvedPrompt: string) => buildAIAdapter(this.config, this.agent, resolvedPrompt),
      sanitizeVariables,
      resolveVariables,
    };
  }

  // ---------------------------------------------------------------------------
  // Twilio WebSocket message parser (thin layer)
  // ---------------------------------------------------------------------------

  private handleTwilioStream(ws: WSWebSocket, url: URL): void {
    const caller = url.searchParams.get('caller') ?? '';
    const callee = url.searchParams.get('callee') ?? '';
    const bridge = new TwilioBridge(this.config);
    const handler = new StreamHandler(this.buildStreamHandlerDeps(bridge), ws, caller, callee);

    ws.on('message', async (raw) => {
      try {
        let data: {
          event: string;
          streamSid?: string;
          start?: { callSid?: string; customParameters?: Record<string, string> };
          media?: { payload?: string };
          mark?: { name?: string };
          dtmf?: { digit?: string };
        };
        try {
          data = JSON.parse(raw.toString()) as typeof data;
        } catch (e) {
          getLogger().error('Failed to parse WS message:', e);
          return;
        }
        const event = data.event;
        getLogger().info(`WS event: ${event}`);

        if (event === 'start') {
          handler.setStreamSid(data.streamSid ?? '');
          const callSid = data.start?.callSid ?? '';
          const customParameters = data.start?.customParameters ?? {};
          await handler.handleCallStart(callSid, customParameters);
        } else if (event === 'media') {
          const payload = data.media?.payload ?? '';
          handler.handleAudio(Buffer.from(payload, 'base64'));
        } else if (event === 'mark') {
          // mark.name tracks last confirmed audio chunk (used for barge-in accuracy)
        } else if (event === 'dtmf') {
          const digit = data.dtmf?.digit ?? '';
          await handler.handleDtmf(digit);
        } else if (event === 'stop') {
          await handler.handleStop();
        }
      } catch (err) {
        getLogger().error('Stream handler error:', err);
      }
    });

    ws.on('close', async () => {
      await handler.handleWsClose();
    });
  }

  // ---------------------------------------------------------------------------
  // Telnyx WebSocket message parser (thin layer)
  // ---------------------------------------------------------------------------

  private handleTelnyxStream(ws: WSWebSocket, url: URL): void {
    const caller = url.searchParams.get('caller') ?? '';
    const callee = url.searchParams.get('callee') ?? '';
    const bridge = new TelnyxBridge(this.config);
    const handler = new StreamHandler(this.buildStreamHandlerDeps(bridge), ws, caller, callee);
    let streamStarted = false;

    ws.on('message', async (raw) => {
      try {
        let data: {
          event_type?: string;
          payload?: {
            call_control_id?: string;
            audio?: { chunk?: string };
          };
        };
        try {
          data = JSON.parse(raw.toString()) as typeof data;
        } catch (e) {
          getLogger().error('Failed to parse Telnyx WS message:', e);
          return;
        }

        const eventType = data.event_type ?? '';
        getLogger().info(`Telnyx event: ${eventType}`);

        if (eventType === 'stream_started' && !streamStarted) {
          streamStarted = true;
          const callControlId = data.payload?.call_control_id ?? '';
          await handler.handleCallStart(callControlId);
        } else if (eventType === 'media') {
          const audioChunk = data.payload?.audio?.chunk ?? '';
          if (!audioChunk) return;
          // Telnyx sends 16 kHz PCM — send directly
          handler.handleAudio(Buffer.from(audioChunk, 'base64'));
        } else if (eventType === 'stream_stopped') {
          await handler.handleStop();
        }
      } catch (err) {
        getLogger().error('Stream handler error (Telnyx):', err);
      }
    });

    ws.on('close', async () => {
      await handler.handleWsClose();
    });
  }

  // ---------------------------------------------------------------------------
  // Graceful shutdown
  // ---------------------------------------------------------------------------

  /**
   * Gracefully stop the server.
   *
   * 1. Stop accepting new connections (close the HTTP server).
   * 2. Send close to all active WebSockets.
   * 3. Wait up to 10 seconds for active calls to finish.
   * 4. Force-close remaining connections.
   * 5. Close the HTTP server.
   */
  async stop(): Promise<void> {
    if (!this.server) return;

    // 1. Stop accepting new HTTP connections
    const httpClosePromise = new Promise<void>((resolve) => {
      this.server!.close(() => resolve());
    });

    // 2. Send close to all active WebSocket connections
    for (const ws of this.activeConnections) {
      try {
        ws.close(1001, 'Server shutting down');
      } catch {
        // Connection may already be closing
      }
    }

    // 3. Wait up to 10 seconds for active calls to drain
    if (this.activeConnections.size > 0) {
      getLogger().info(`Waiting for ${this.activeConnections.size} active connection(s) to close...`);
      await Promise.race([
        new Promise<void>((resolve) => {
          const checkInterval = setInterval(() => {
            if (this.activeConnections.size === 0) {
              clearInterval(checkInterval);
              resolve();
            }
          }, 100);
        }),
        new Promise<void>((resolve) => setTimeout(resolve, GRACEFUL_SHUTDOWN_TIMEOUT_MS)),
      ]);
    }

    // 4. Force-close remaining connections
    if (this.activeConnections.size > 0) {
      getLogger().info(`Force-closing ${this.activeConnections.size} remaining connection(s)`);
      for (const ws of this.activeConnections) {
        try {
          ws.terminate();
        } catch {
          // Already terminated
        }
      }
      this.activeConnections.clear();
    }

    // 5. Wait for HTTP server to fully close
    await httpClosePromise;
    this.server = null;
    this.wss = null;
  }
}

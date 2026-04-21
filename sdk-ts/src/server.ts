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
import { StreamHandler, sanitizeLogValue } from './stream-handler';
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
 * Validate a Twilio SID (CallSid etc.) to prevent path traversal / injection
 * when interpolating into Twilio API URLs. Twilio SIDs are 34 characters:
 * a two-letter prefix (e.g. 'CA' for calls) followed by 32 hex characters.
 */
export function validateTwilioSid(sid: string, prefix = 'CA'): boolean {
  return sid.length === 34 && sid.startsWith(prefix) && /^[A-Z]{2}[0-9a-f]{32}$/.test(sid);
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

/**
 * Normalize the ``options`` bag carried on an ``STTConfig`` into the strongly
 * typed ``DeepgramSTTOptions`` shape. Accepts both snake_case (as produced by
 * the ``deepgram()`` factory to match Python serialisation) and camelCase keys.
 */
type DeepgramOptionsMutable = {
  -readonly [K in keyof import('./providers/deepgram-stt').DeepgramSTTOptions]:
    import('./providers/deepgram-stt').DeepgramSTTOptions[K];
};

function extractDeepgramOptions(options?: Record<string, unknown>): DeepgramOptionsMutable {
  if (!options) return {};
  const get = (snake: string, camel: string): unknown => options[snake] ?? options[camel];
  const out: DeepgramOptionsMutable = {};
  const model = get('model', 'model');
  if (typeof model === 'string') out.model = model;
  const endpointing = get('endpointing_ms', 'endpointingMs');
  if (typeof endpointing === 'number') out.endpointingMs = endpointing;
  const utteranceEnd = get('utterance_end_ms', 'utteranceEndMs');
  if (utteranceEnd === null) out.utteranceEndMs = null;
  else if (typeof utteranceEnd === 'number') out.utteranceEndMs = utteranceEnd;
  const smart = get('smart_format', 'smartFormat');
  if (typeof smart === 'boolean') out.smartFormat = smart;
  const interim = get('interim_results', 'interimResults');
  if (typeof interim === 'boolean') out.interimResults = interim;
  const vad = get('vad_events', 'vadEvents');
  if (typeof vad === 'boolean') out.vadEvents = vad;
  return out;
}

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
      if (!validateTwilioSid(callId)) {
        getLogger().warn(`TwilioBridge.transferCall rejected: invalid CallSid ${JSON.stringify(callId)}`);
        return;
      }
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
      if (!validateTwilioSid(callId)) {
        getLogger().warn(`TwilioBridge.endCall rejected: invalid CallSid ${JSON.stringify(callId)}`);
        return;
      }
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
    // BUG #12 — Pipeline mode transcodes Twilio's mulaw 8 kHz to PCM16 16 kHz
    // in ``StreamHandler.handleAudio`` before forwarding to STT. Configuring
    // Deepgram for mulaw 8 kHz here would cause it to misinterpret the
    // already-decoded PCM as garbage. The pipeline always uses linear16
    // 16 kHz; ``forTwilio`` is kept for non-pipeline Twilio integrations.
    const isPipeline = agent.provider === 'pipeline';
    if (agent.stt) {
      if (agent.stt.provider === 'deepgram') {
        const dgOptions = extractDeepgramOptions(agent.stt.options);
        if (isPipeline) {
          return new DeepgramSTT(agent.stt.apiKey, agent.stt.language ?? 'en', dgOptions.model, 'linear16', 16000, dgOptions);
        }
        return DeepgramSTT.forTwilio(agent.stt.apiKey, agent.stt.language ?? 'en', dgOptions.model, dgOptions);
      } else if (agent.stt.provider === 'whisper') {
        return isPipeline
          ? new WhisperSTT(agent.stt.apiKey, 'whisper-1', agent.stt.language ?? 'en')
          : WhisperSTT.forTwilio(agent.stt.apiKey, agent.stt.language ?? 'en');
      }
    } else if (agent.deepgramKey) {
      if (isPipeline) {
        return new DeepgramSTT(agent.deepgramKey, agent.language ?? 'en', 'nova-3', 'linear16', 16000);
      }
      return DeepgramSTT.forTwilio(agent.deepgramKey, agent.language ?? 'en');
    }
    return null;
  }

  async queryTelephonyCost(metricsAcc: CallMetricsAccumulator, callId: string): Promise<void> {
    if (this.config.twilioSid && this.config.twilioToken && callId) {
      if (!validateTwilioSid(callId)) {
        getLogger().warn(`TwilioBridge.queryTelephonyCost rejected: invalid CallSid ${JSON.stringify(callId)}`);
        return;
      }
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

/** Accept E.164 phone numbers and SIP(s) URIs as Telnyx transfer targets. */
function isValidTelnyxTransferTarget(target: string): boolean {
  if (typeof target !== 'string' || !target) return false;
  if (/^\+[1-9]\d{6,14}$/.test(target)) return true;
  return /^sips?:[^\s@]+(@[^\s]+)?$/i.test(target);
}

/** DTMF digits accepted by the Telnyx `send_dtmf` command. */
const TELNYX_DTMF_ALLOWED = new Set('0123456789*#ABCDabcd');
const TELNYX_DTMF_DURATION_MS = 250;

async function sleep(ms: number): Promise<void> {
  if (ms <= 0) return;
  await new Promise((resolve) => setTimeout(resolve, ms));
}

/** Telnyx-specific telephony bridge. */
export class TelnyxBridge implements TelephonyBridge {
  readonly label = 'Telnyx';
  readonly telephonyProvider = 'telnyx' as const;

  constructor(private readonly config: LocalConfig) {}

  sendAudio(ws: WSWebSocket, audioBase64: string, _streamSid: string): void {
    // BUG #18 — Telnyx media-stream outbound wire format is
    // ``{"event":"media","media":{"payload":b64}}``, not the legacy
    // ``event_type``/``payload.audio.chunk`` shape.
    ws.send(JSON.stringify({ event: 'media', media: { payload: audioBase64 } }));
  }

  sendMark(_ws: WSWebSocket, _markName: string, _streamSid: string): void {
    // Telnyx does not support mark events — no-op
  }

  sendClear(ws: WSWebSocket, _streamSid: string): void {
    // BUG #18 — matching clear signal.
    ws.send(JSON.stringify({ event: 'clear' }));
  }

  async transferCall(callId: string, toNumber: string): Promise<void> {
    if (!isValidTelnyxTransferTarget(toNumber)) {
      getLogger().warn(`TelnyxBridge.transferCall rejected: invalid target ${JSON.stringify(toNumber)}`);
      return;
    }
    const telnyxKey = this.config.telnyxKey ?? '';
    await fetch(`https://api.telnyx.com/v2/calls/${callId}/actions/transfer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
      body: JSON.stringify({ to: toNumber }),
    });
    getLogger().info(`Telnyx call transferred to ${toNumber}`);
  }

  async sendDtmf(callId: string, digits: string, delayMs: number): Promise<void> {
    if (!digits) {
      getLogger().warn('TelnyxBridge.sendDtmf called with empty digits');
      return;
    }
    const telnyxKey = this.config.telnyxKey ?? '';
    if (!telnyxKey || !callId) {
      getLogger().warn('TelnyxBridge.sendDtmf skipped: telnyxKey or callId missing');
      return;
    }
    const filtered = Array.from(digits).filter((d) => TELNYX_DTMF_ALLOWED.has(d));
    if (filtered.length === 0) {
      getLogger().warn(`TelnyxBridge.sendDtmf: no valid digits in ${JSON.stringify(digits)}`);
      return;
    }
    const duration = Math.max(100, Math.min(500, TELNYX_DTMF_DURATION_MS));
    for (let i = 0; i < filtered.length; i += 1) {
      await fetch(`https://api.telnyx.com/v2/calls/${callId}/actions/send_dtmf`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
        body: JSON.stringify({ digits: filtered[i], duration_millis: duration }),
      });
      if (i < filtered.length - 1) {
        await sleep(delayMs);
      }
    }
    getLogger().info(`Telnyx DTMF sent (${filtered.length} digits, delay=${delayMs}ms)`);
  }

  async startRecording(callId: string): Promise<void> {
    const telnyxKey = this.config.telnyxKey ?? '';
    if (!telnyxKey || !callId) return;
    try {
      const resp = await fetch(`https://api.telnyx.com/v2/calls/${callId}/actions/record_start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
        body: JSON.stringify({ format: 'mp3', channels: 'single' }),
      });
      if (!resp.ok) {
        getLogger().warn(`Telnyx record_start failed (${resp.status}): ${(await resp.text()).slice(0, 200)}`);
      } else {
        getLogger().info('Telnyx recording started');
      }
    } catch (e) {
      getLogger().warn(`Telnyx record_start error: ${String(e)}`);
    }
  }

  async stopRecording(callId: string): Promise<void> {
    const telnyxKey = this.config.telnyxKey ?? '';
    if (!telnyxKey || !callId) return;
    try {
      const resp = await fetch(`https://api.telnyx.com/v2/calls/${callId}/actions/record_stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
        body: JSON.stringify({}),
      });
      if (!resp.ok) {
        getLogger().warn(`Telnyx record_stop failed (${resp.status}): ${(await resp.text()).slice(0, 200)}`);
      } else {
        getLogger().info('Telnyx recording stopped');
      }
    } catch (e) {
      getLogger().warn(`Telnyx record_stop error: ${String(e)}`);
    }
  }

  async endCall(callId: string, ws: WSWebSocket): Promise<void> {
    // Hang up via Telnyx Call Control API, then close the media WebSocket
    const telnyxKey = this.config.telnyxKey ?? '';
    if (callId && telnyxKey) {
      try {
        await fetch(`https://api.telnyx.com/v2/calls/${callId}/actions/hangup`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
          body: JSON.stringify({}),
        });
      } catch { /* best effort — call may already be ended */ }
    }
    ws.close();
  }

  createStt(agent: AgentOptions): DeepgramSTT | WhisperSTT | null {
    if (agent.stt) {
      if (agent.stt.provider === 'deepgram') {
        // Telnyx pipeline also transcodes mulaw 8 kHz → PCM16 16 kHz before STT.
        const dgOptions = extractDeepgramOptions(agent.stt.options);
        return new DeepgramSTT(
          agent.stt.apiKey,
          agent.stt.language ?? 'en',
          dgOptions.model ?? 'nova-3',
          'linear16',
          16000,
          dgOptions,
        );
      } else if (agent.stt.provider === 'whisper') {
        return new WhisperSTT(agent.stt.apiKey, 'whisper-1', agent.stt.language ?? 'en');
      }
    } else if (agent.deepgramKey) {
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
  private telnyxSigWarningLogged = false;
  readonly metricsStore: MetricsStore;
  private readonly pricing: ReturnType<typeof mergePricing>;
  private readonly remoteHandler = new RemoteMessageHandler();

  /** Active WebSocket connections tracked for graceful shutdown. */
  private readonly activeConnections = new Set<WSWebSocket>();
  private readonly activeCallIds = new Map<WSWebSocket, string>();

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
      getLogger().info('Dashboard: http://127.0.0.1:' + port + '/');
    }

    // Twilio statusCallback — captures ringing/no-answer/busy/failed
    // transitions so the dashboard surfaces calls that never reach media.
    // See BUG #06.
    app.post('/webhooks/twilio/status', (req, res) => {
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
      const callSid = sanitizeLogValue(body['CallSid'] ?? '');
      const callStatus = sanitizeLogValue(body['CallStatus'] ?? '');
      const duration = body['CallDuration'] ?? body['Duration'] ?? '';
      getLogger().info(
        `Twilio status ${callStatus} for call ${callSid} (duration=${duration})`,
      );
      if (callSid && callStatus) {
        const extra: Record<string, unknown> = {};
        const parsed = parseFloat(duration);
        if (!Number.isNaN(parsed)) extra.duration_seconds = parsed;
        this.metricsStore.updateCallStatus(callSid, callStatus, extra);
      }
      res.status(204).send();
    });

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
      const recordingSid = sanitizeLogValue(body['RecordingSid'] ?? '');
      const recordingUrl = sanitizeLogValue(body['RecordingUrl'] ?? '');
      const callSid = sanitizeLogValue(body['CallSid'] ?? '');
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
      getLogger().info(`AMD result for ${sanitizeLogValue(callSid)}: ${sanitizeLogValue(answeredBy)}`);

      if (
        (answeredBy === 'machine_end_beep' || answeredBy === 'machine_end_silence') &&
        this.voicemailMessage &&
        this.config.twilioSid &&
        this.config.twilioToken
      ) {
        if (!validateTwilioSid(callSid)) {
          getLogger().warn(`AMD webhook rejected: invalid CallSid ${JSON.stringify(sanitizeLogValue(callSid))}`);
          res.status(400).send('Invalid CallSid');
          return;
        }
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
            getLogger().info(`Voicemail dropped for ${sanitizeLogValue(callSid)}`);
          } else {
            getLogger().warn(`Could not drop voicemail: ${sanitizeLogValue(await vmResp.text())}`);
          }
        } catch (e) {
          getLogger().warn(`Could not drop voicemail: ${sanitizeLogValue(String(e))}`);
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
      if (callSid && !validateTwilioSid(callSid)) {
        getLogger().warn(`Twilio voice webhook rejected: invalid CallSid ${JSON.stringify(callSid)}`);
        res.status(400).send('Invalid CallSid');
        return;
      }
      const caller = (req.body.From as string) || '';
      const callee = (req.body.To as string) || '';
      const rawStreamUrl = `wss://${this.config.webhookUrl}/ws/stream/${callSid}`;
      const xmlStreamUrl = xmlEscape(rawStreamUrl);
      const twiml = `<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="${xmlStreamUrl}"><Parameter name="caller" value="${xmlEscape(caller)}"/><Parameter name="callee" value="${xmlEscape(callee)}"/></Stream></Connect></Response>`;
      res.type('text/xml').send(twiml);
    });

    app.post('/webhooks/telnyx/voice', async (req, res) => {
      // Enforce Ed25519 signature verification when a public key is configured.
      if (this.config.telnyxPublicKey) {
        const rawBody = (req as express.Request & { rawBody?: string }).rawBody ?? '';
        const signature = (req.headers['telnyx-signature-ed25519'] as string) ?? '';
        const timestamp = (req.headers['telnyx-timestamp'] as string) ?? '';
        if (!signature || !timestamp || !validateTelnyxSignature(rawBody, signature, timestamp, this.config.telnyxPublicKey)) {
          getLogger().warn('Telnyx webhook rejected: invalid or missing Ed25519 signature');
          return res.status(403).send('Invalid signature');
        }
      } else if (!this.telnyxSigWarningLogged) {
        this.telnyxSigWarningLogged = true;
        getLogger().warn('Telnyx webhook signature verification is disabled. Set telnyxPublicKey in LocalOptions for production use.');
      }

      const body = req.body as {
        data?: {
          event_type?: string;
          payload?: {
            call_control_id?: string;
            from?: string;
            to?: string;
            digit?: string;
            recording_urls?: { mp3?: string; wav?: string };
            public_recording_urls?: { mp3?: string; wav?: string };
          };
        };
      };

      if (typeof body?.data !== 'object' || body.data === null || Array.isArray(body.data)) {
        return res.status(400).send('Invalid body');
      }
      if (typeof body.data.event_type !== 'string' || typeof body.data.payload !== 'object' || body.data.payload === null) {
        return res.status(400).send('Invalid body');
      }

      const eventType = body.data.event_type ?? '';
      const payload = body.data.payload ?? {};

      if (eventType === 'call.dtmf.received') {
        const digit = String(payload.digit ?? '').trim();
        if (digit) {
          getLogger().info(`Telnyx DTMF received (webhook): ${sanitizeLogValue(digit)}`);
        }
        return res.status(200).send();
      }

      if (eventType === 'call.recording.saved') {
        const recordingUrl =
          payload.recording_urls?.mp3 ??
          payload.recording_urls?.wav ??
          payload.public_recording_urls?.mp3 ??
          '';
        if (recordingUrl) {
          getLogger().info(`Telnyx recording saved (webhook): ${sanitizeLogValue(recordingUrl)}`);
        }
        return res.status(200).send();
      }

      const callControlId = payload.call_control_id ?? '';
      if (!callControlId) {
        getLogger().warn('Telnyx webhook rejected: missing call_control_id');
        return res.status(400).send('Invalid webhook payload');
      }

      // BUG #16 — Telnyx Call Control is a REST API. The webhook body is an
      // informational notification; the response body is ignored. To answer
      // a call we POST ``actions/answer``, and to start audio streaming we
      // POST ``actions/streaming_start`` (once the call is answered).
      const apiKey = this.config.telnyxKey;
      if (!apiKey) {
        getLogger().warn('Telnyx webhook: missing telnyxKey in LocalOptions');
        return res.status(500).send('Missing Telnyx API key');
      }

      const apiBase = 'https://api.telnyx.com/v2';
      const authHeaders = {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      } as const;

      try {
        if (eventType === 'call.initiated') {
          getLogger().info(`Telnyx call.initiated ${callControlId} — answering`);
          const resp = await fetch(`${apiBase}/calls/${encodeURIComponent(callControlId)}/actions/answer`, {
            method: 'POST',
            headers: authHeaders,
            body: JSON.stringify({}),
            signal: AbortSignal.timeout(10_000),
          });
          if (!resp.ok) {
            getLogger().warn(`Telnyx answer failed: ${resp.status} ${(await resp.text()).slice(0, 200)}`);
          }
        } else if (eventType === 'call.answered') {
          const caller = payload.from ?? '';
          const callee = payload.to ?? '';
          const streamUrl =
            `wss://${this.config.webhookUrl}/ws/stream/${encodeURIComponent(callControlId)}` +
            `?caller=${encodeURIComponent(caller)}&callee=${encodeURIComponent(callee)}`;
          getLogger().info(`Telnyx call.answered ${callControlId} — starting stream`);
          const resp = await fetch(`${apiBase}/calls/${encodeURIComponent(callControlId)}/actions/streaming_start`, {
            method: 'POST',
            headers: authHeaders,
            body: JSON.stringify({
              stream_url: streamUrl,
              stream_track: 'both_tracks',
              stream_bidirectional_mode: 'rtp',
              stream_bidirectional_codec: 'PCMU',
              stream_bidirectional_sampling_rate: 8000,
              stream_bidirectional_target_legs: 'self',
            }),
            signal: AbortSignal.timeout(10_000),
          });
          if (!resp.ok) {
            getLogger().warn(`Telnyx streaming_start failed: ${resp.status} ${(await resp.text()).slice(0, 200)}`);
          }
        } else {
          getLogger().debug(`Telnyx event ignored: ${eventType}`);
        }
      } catch (e) {
        getLogger().error(`Telnyx webhook handler error: ${String(e)}`);
      }

      // Telnyx ignores the response body. Acknowledge with 200 OK.
      return res.status(200).send();
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
      // Bind to loopback only. Public exposure should go through a reverse
      // proxy or tunnel so the Node process is never directly reachable.
      this.server!.listen(port, '127.0.0.1', () => {
        getLogger().info(`
██████╗  █████╗ ████████╗████████╗███████╗██████╗
██╔══██╗██╔══██╗╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
██████╔╝███████║   ██║      ██║   █████╗  ██████╔╝
██╔═══╝ ██╔══██║   ██║      ██║   ██╔══╝  ██╔══██╗
██║     ██║  ██║   ██║      ██║   ███████╗██║  ██║
╚═╝     ╚═╝  ╚═╝   ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝

Connect AI agents to phone numbers in 4 lines of code
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
          if (callSid) this.activeCallIds.set(ws, callSid);
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
      this.activeCallIds.delete(ws);
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
        // BUG #17 — Telnyx media-stream WebSocket uses ``event`` (not
        // ``event_type``, which is a Call Control REST notification field),
        // and the frame layout is ``{event, start|media|stop|dtmf}`` —
        // mirror of the Python bridge.
        let data: {
          event?: string;
          start?: { call_control_id?: string; from?: string; to?: string };
          media?: { payload?: string; track?: string };
          dtmf?: { digit?: string };
          stop?: Record<string, unknown>;
        };
        try {
          data = JSON.parse(raw.toString()) as typeof data;
        } catch (e) {
          getLogger().error('Failed to parse Telnyx WS message:', e);
          return;
        }

        const event = data.event ?? '';
        if (event === 'connected') return;  // first ping, nothing to do

        getLogger().info(`Telnyx event: ${event}`);

        if (event === 'start' && !streamStarted) {
          streamStarted = true;
          const callControlId = data.start?.call_control_id ?? '';
          if (callControlId) this.activeCallIds.set(ws, callControlId);
          await handler.handleCallStart(callControlId);
          if (this.recording) {
            try {
              await bridge.startRecording?.(callControlId);
            } catch (e) {
              getLogger().warn(`Could not start recording: ${String(e)}`);
            }
          }
        } else if (event === 'media') {
          // BUG #19 — with ``stream_track=both_tracks`` Telnyx sends media
          // for the caller leg (``track=inbound``) AND for our injected
          // outbound leg (``track=outbound``). Forwarding the outbound
          // echo feeds the agent its own voice and breaks turn detection.
          const track = data.media?.track ?? 'inbound';
          if (track !== 'inbound') return;
          const audioChunk = data.media?.payload ?? '';
          if (!audioChunk) return;
          handler.handleAudio(Buffer.from(audioChunk, 'base64'));
        } else if (event === 'dtmf') {
          const digit = String(data.dtmf?.digit ?? '').trim();
          if (digit) {
            getLogger().info(`Telnyx DTMF received: ${digit}`);
            await handler.handleDtmf(digit);
          }
        } else if (event === 'error') {
          getLogger().warn(`Telnyx stream error: ${JSON.stringify(data)}`);
        } else if (event === 'stop') {
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

    // 2. Hang up all active telephony calls via provider API
    const isTelnyx = this.config.telephonyProvider === 'telnyx';
    for (const [ws, callId] of this.activeCallIds) {
      try {
        const bridge = isTelnyx ? new TelnyxBridge(this.config) : new TwilioBridge(this.config);
        await bridge.endCall(callId, ws);
      } catch { /* best effort */ }
    }
    this.activeCallIds.clear();

    // 3. Send close to all active WebSocket connections
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

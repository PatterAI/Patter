import express from 'express';
import { createServer, Server as HTTPServer } from 'http';
import { WebSocketServer, WebSocket as WSWebSocket } from 'ws';
import { OpenAIRealtimeAdapter } from './providers/openai-realtime';
import { ElevenLabsConvAIAdapter } from './providers/elevenlabs-convai';
import { DeepgramSTT } from './providers/deepgram-stt';
import { WhisperSTT } from './providers/whisper-stt';
import { ElevenLabsTTS } from './providers/elevenlabs-tts';
import { OpenAITTS } from './providers/openai-tts';
import type { AgentOptions, Guardrail, PipelineMessageHandler } from './types';

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
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const crypto = require('crypto') as typeof import('crypto');
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
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const crypto = require('crypto') as typeof import('crypto');
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
function resolveVariables(template: string, variables: Record<string, string>): string {
  let result = template;
  for (const [key, value] of Object.entries(variables)) {
    result = result.replaceAll(`{${key}}`, value);
  }
  return result;
}

/**
 * Validate that a string is a valid E.164 phone number.
 */
function isValidE164(number: string): boolean {
  return /^\+[1-9]\d{6,14}$/.test(number);
}

/**
 * Strip control characters and truncate a user-controlled string before logging.
 * Prevents log injection and limits log line size.
 */
function sanitizeLogValue(v: string, maxLen = 200): string {
  // eslint-disable-next-line no-control-regex
  const cleaned = v.replace(/[\x00-\x1f\x7f]/g, '');
  return cleaned.length > maxLen ? cleaned.slice(0, maxLen) + '...' : cleaned;
}

/**
 * Check if a text string triggers any of the provided guardrails.
 * Returns the first triggered guardrail, or null if none matched.
 */
function checkGuardrails(text: string, guardrails: Guardrail[] | undefined): Guardrail | null {
  if (!guardrails) return null;
  for (const guard of guardrails) {
    let blocked = false;
    if (guard.blockedTerms) {
      blocked = guard.blockedTerms.some((term) => text.toLowerCase().includes(term.toLowerCase()));
    }
    if (!blocked && guard.check) {
      blocked = guard.check(text);
    }
    if (blocked) return guard;
  }
  return null;
}

function buildAIAdapter(config: LocalConfig, agent: AgentOptions, resolvedPrompt?: string): AIAdapter {
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

export class EmbeddedServer {
  private server: HTTPServer | null = null;
  private twilioTokenWarningLogged = false;

  constructor(
    private readonly config: LocalConfig,
    private readonly agent: AgentOptions,
    public onCallStart?: (data: Record<string, unknown>) => Promise<void>,
    public onCallEnd?: (data: Record<string, unknown>) => Promise<void>,
    public onTranscript?: (data: Record<string, unknown>) => Promise<void>,
    public onMessage?: PipelineMessageHandler,
    private readonly recording: boolean = false,
    public voicemailMessage: string = '',
  ) {}

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
      console.log(`[PATTER] Recording ${recordingSid} for call ${callSid}: ${recordingUrl}`);
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
      console.log(`[PATTER] AMD result for ${callSid}: ${answeredBy}`);

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
            console.log(`[PATTER] Voicemail dropped for ${callSid}`);
          } else {
            console.warn(`[PATTER] Warning: could not drop voicemail: ${await vmResp.text()}`);
          }
        } catch (e) {
          console.warn(`[PATTER] Warning: could not drop voicemail: ${String(e)}`);
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
        console.warn('[PATTER] WARNING: Twilio webhook signature validation disabled — set twilioToken for production');
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
          console.warn('[PATTER] Telnyx webhook rejected: invalid or missing Ed25519 signature');
          return res.status(403).send('Invalid signature');
        }
      } else {
        console.warn('[PATTER] Warning: Telnyx webhook signature verification is disabled. Set telnyxPublicKey in LocalOptions for production use.');
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
    const wss = new WebSocketServer({ noServer: true });

    // Per-IP WebSocket connection counter for DoS protection.
    // Telephony providers (Twilio/Telnyx) only open 1 connection per call;
    // a limit of 10 concurrent connections per IP is generous but blocks abuse.
    const MAX_WS_PER_IP = 10;
    const wsConnectionsByIp = new Map<string, number>();

    this.server.on('upgrade', (req, socket, head) => {
      const remoteIp = (req.socket?.remoteAddress ?? 'unknown').replace(/^::ffff:/, '');
      const currentCount = wsConnectionsByIp.get(remoteIp) ?? 0;
      if (currentCount >= MAX_WS_PER_IP) {
        console.warn(`[PATTER] WebSocket upgrade rejected: too many connections from ${remoteIp}`);
        socket.write('HTTP/1.1 429 Too Many Requests\r\n\r\n');
        socket.destroy();
        return;
      }
      console.log(`[PATTER] Upgrade request: ${req.url}`);
      wss.handleUpgrade(req, socket, head, (ws) => {
        wsConnectionsByIp.set(remoteIp, (wsConnectionsByIp.get(remoteIp) ?? 0) + 1);
        ws.once('close', () => {
          const count = (wsConnectionsByIp.get(remoteIp) ?? 1) - 1;
          if (count <= 0) {
            wsConnectionsByIp.delete(remoteIp);
          } else {
            wsConnectionsByIp.set(remoteIp, count);
          }
        });
        wss.emit('connection', ws, req);
      });
    });

    wss.on('connection', (ws, req) => {
      const url = new URL((req as { url?: string }).url ?? '', `http://localhost`);
      console.log(`[PATTER] WebSocket connected: ${(req as { url?: string }).url}`);

      const isTelnyx = this.config.telephonyProvider === 'telnyx';
      if (isTelnyx) {
        this.handleTelnyxStream(ws, url);
      } else {
        this.handleTwilioStream(ws, url);
      }
    });

    await new Promise<void>((resolve) => {
      this.server!.listen(port, '127.0.0.1', () => {
        console.log(`
██████╗  █████╗ ████████╗████████╗███████╗██████╗
██╔══██╗██╔══██╗╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
██████╔╝███████║   ██║      ██║   █████╗  ██████╔╝
██╔═══╝ ██╔══██║   ██║      ██║   ██╔══╝  ██╔══██╗
██║     ██║  ██║   ██║      ██║   ███████╗██║  ██║
╚═╝     ╚═╝  ╚═╝   ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝

Connect AI agents to phone numbers with ~10 lines of code
`);
        console.log(`[PATTER] Server on port ${port}`);
        console.log(`[PATTER] Webhook: https://${this.config.webhookUrl}`);
        console.log(`[PATTER] Phone: ${this.config.phoneNumber}`);
        resolve();
      });
    });
  }

  private handleTwilioStream(ws: WSWebSocket, url: URL): void {
    const caller = url.searchParams.get('caller') ?? '';
    const callee = url.searchParams.get('callee') ?? '';
    let streamSid = '';
    let adapter: AIAdapter | null = null;
    let callSid = '';

    // Conversation history — accumulated per call, passed to callbacks (capped at 200 entries)
    const conversationHistory: Array<{ role: string; text: string; timestamp: number }> = [];
    const pushHistory = (entry: { role: string; text: string; timestamp: number }) => {
      if (conversationHistory.length >= 200) conversationHistory.shift();
      conversationHistory.push(entry);
    };

    // Pipeline mode state
    let stt: DeepgramSTT | WhisperSTT | null = null;
    let tts: ElevenLabsTTS | OpenAITTS | null = null;
    let isSpeaking = false;

    // Mark-based barge-in state
    let chunkCount = 0;

    // Guard to ensure onCallEnd is fired exactly once per call
    let callEndFired = false;

    console.log('[PATTER] WebSocket connection opened (Twilio)');

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
        console.error('[PATTER] Failed to parse WS message:', e);
        return;
      }
      const event = data.event;
      console.log(`[PATTER] WS event: ${event}`);

      if (event === 'start') {
        streamSid = data.streamSid ?? '';
        callSid = data.start?.callSid ?? '';
        const customParameters = data.start?.customParameters ?? {};
        console.log(`[PATTER] Call started: ${callSid}`);
        if (Object.keys(customParameters).length > 0) {
          console.log(`[PATTER] Custom params: ${sanitizeLogValue(JSON.stringify(customParameters))}`);
        }

        if (this.onCallStart) {
          await this.onCallStart({ call_id: callSid, caller, callee, direction: 'inbound', custom_params: customParameters });
        }

        // Start recording if requested
        if (this.recording && this.config.twilioSid && this.config.twilioToken && callSid) {
          try {
            const recUrl = `https://api.twilio.com/2010-04-01/Accounts/${this.config.twilioSid}/Calls/${callSid}/Recordings.json`;
            const recResp = await fetch(recUrl, {
              method: 'POST',
              headers: {
                'Authorization': `Basic ${Buffer.from(`${this.config.twilioSid}:${this.config.twilioToken}`).toString('base64')}`,
              },
            });
            if (recResp.ok) {
              console.log(`[PATTER] Recording started for ${callSid}`);
            } else {
              console.warn(`[PATTER] Warning: could not start recording: ${await recResp.text()}`);
            }
          } catch (e) {
            console.warn(`[PATTER] Warning: could not start recording: ${String(e)}`);
          }
        }

        // Resolve dynamic variables in system prompt.
        // agent.variables are merged with customParameters from TwiML (customParams win on conflict).
        const agentVars = sanitizeVariables(this.agent.variables ?? {});
        const safeCustomParams = sanitizeVariables(customParameters);
        const allVars = { ...agentVars, ...safeCustomParams };
        const resolvedPrompt = Object.keys(allVars).length > 0
          ? resolveVariables(this.agent.systemPrompt, allVars)
          : this.agent.systemPrompt;

        const provider = this.agent.provider ?? 'openai_realtime';

        if (provider === 'pipeline') {
          // ---- Pipeline mode: configurable STT + TTS ----

          // Create STT: prefer agent.stt config, fall back to agent.deepgramKey
          if (this.agent.stt) {
            if (this.agent.stt.provider === 'deepgram') {
              stt = DeepgramSTT.forTwilio(this.agent.stt.apiKey, this.agent.stt.language ?? 'en');
            } else if (this.agent.stt.provider === 'whisper') {
              stt = WhisperSTT.forTwilio(this.agent.stt.apiKey, this.agent.stt.language ?? 'en');
            }
          } else if (this.agent.deepgramKey) {
            stt = DeepgramSTT.forTwilio(this.agent.deepgramKey, this.agent.language ?? 'en');
          }

          // Create TTS: prefer agent.tts config, fall back to agent.elevenlabsKey
          if (this.agent.tts) {
            if (this.agent.tts.provider === 'elevenlabs') {
              tts = new ElevenLabsTTS(this.agent.tts.apiKey, this.agent.tts.voice ?? '21m00Tcm4TlvDq8ikWAM');
            }
            if (this.agent.tts.provider === 'openai') {
              tts = new OpenAITTS(this.agent.tts.apiKey, this.agent.tts.voice ?? 'alloy');
            }
            // other tts providers can be added here
          } else if (this.agent.elevenlabsKey) {
            const voiceId = (this.agent.voice && this.agent.voice !== 'alloy')
              ? this.agent.voice
              : '21m00Tcm4TlvDq8ikWAM';
            tts = new ElevenLabsTTS(this.agent.elevenlabsKey, voiceId);
          }

          if (!stt) {
            console.log('[PATTER] Pipeline mode (Twilio): no STT configured');
          }
          if (!tts) {
            console.log('[PATTER] Pipeline mode (Twilio): no TTS configured');
          }

          try {
            if (stt) await stt.connect();
            console.log('[PATTER] Pipeline mode (Twilio): STT + TTS connected');
          } catch (e) {
            console.error('[PATTER] Pipeline connect FAILED:', e);
            return;
          }

          if (this.agent.firstMessage && !this.onMessage && tts) {
            try {
              for await (const chunk of tts.synthesizeStream(this.agent.firstMessage)) {
                const encoded = chunk.toString('base64');
                ws.send(JSON.stringify({ event: 'media', streamSid, media: { payload: encoded } }));
              }
            } catch (e) {
              console.error('[PATTER] First message TTS error:', e);
            }
          }

          if (stt) stt.onTranscript(async (transcript) => {
            if (!transcript.isFinal || !transcript.text) return;

            console.log(`[PATTER] User: ${sanitizeLogValue(transcript.text)}`);

            pushHistory({ role: 'user', text: transcript.text, timestamp: Date.now() });

            if (this.onTranscript) {
              await this.onTranscript({
                role: 'user',
                text: transcript.text,
                call_id: callSid,
                history: [...conversationHistory],
              });
            }

            if (!this.onMessage) return;

            let responseText: string;
            try {
              responseText = await this.onMessage({
                text: transcript.text,
                call_id: callSid,
                caller,
                history: [...conversationHistory],
              });
            } catch (e) {
              console.error('[PATTER] onMessage error:', e);
              return;
            }

            if (!responseText) return;

            pushHistory({ role: 'assistant', text: responseText, timestamp: Date.now() });

            isSpeaking = true;
            try {
              for await (const chunk of tts!.synthesizeStream(responseText)) {
                if (!isSpeaking) break;
                const encoded = chunk.toString('base64');
                ws.send(JSON.stringify({ event: 'media', streamSid, media: { payload: encoded } }));
              }
            } catch (e) {
              console.error('[PATTER] TTS streaming error:', e);
            } finally {
              isSpeaking = false;
            }
          });

        } else {
          // ---- OpenAI Realtime / ElevenLabs ConvAI mode ----
          adapter = buildAIAdapter(this.config, this.agent, resolvedPrompt);
          try {
            await adapter.connect();
            console.log('[PATTER] AI adapter connected (Twilio)');
          } catch (e) {
            console.error('[PATTER] AI adapter connect FAILED:', e);
            return;
          }

          if (this.agent.firstMessage && adapter instanceof OpenAIRealtimeAdapter) {
            await adapter.sendText(this.agent.firstMessage);
          }

          adapter.onEvent(async (type, eventData) => {
            try {
            if (type === 'audio') {
              const encoded = (eventData as Buffer).toString('base64');
              ws.send(JSON.stringify({ event: 'media', streamSid, media: { payload: encoded } }));
              // Send mark so we track which audio chunk was played (for barge-in accuracy)
              chunkCount++;
              ws.send(JSON.stringify({ event: 'mark', streamSid, mark: { name: `audio_${chunkCount}` } }));
            } else if (type === 'transcript_input') {
              const inputText = eventData as string;
              console.log(`[PATTER] User: ${sanitizeLogValue(inputText)}`);
              pushHistory({ role: 'user', text: inputText, timestamp: Date.now() });
              if (this.onTranscript) {
                await this.onTranscript({
                  role: 'user',
                  text: inputText,
                  call_id: callSid,
                  history: [...conversationHistory],
                });
              }
            } else if (type === 'transcript_output') {
              const outputText = eventData as string;
              if (outputText) {
                const triggered = checkGuardrails(outputText, this.agent.guardrails);
                if (triggered) {
                  console.log(`[PATTER] Guardrail '${triggered.name}' triggered`);
                  if (adapter instanceof OpenAIRealtimeAdapter) {
                    adapter.cancelResponse();
                    await adapter.sendText(triggered.replacement ?? "I'm sorry, I can't respond to that.");
                  }
                }
                pushHistory({ role: 'assistant', text: outputText, timestamp: Date.now() });
              }
            } else if (type === 'speech_started' || type === 'interruption') {
              ws.send(JSON.stringify({ event: 'clear', streamSid }));
              if (adapter instanceof OpenAIRealtimeAdapter) {
                adapter.cancelResponse();
              }
            } else if (type === 'function_call' && adapter instanceof OpenAIRealtimeAdapter) {
              const fc = eventData as { call_id: string; name: string; arguments: string };
              if (fc.name === 'transfer_call') {
                // System tool — transfer the call
                let transferArgs: { number?: string };
                try {
                  transferArgs = JSON.parse(fc.arguments || '{}') as { number?: string };
                } catch {
                  transferArgs = {};
                }
                const transferTo = transferArgs.number ?? '';
                if (!isValidE164(transferTo)) {
                  console.warn(`[PATTER] transfer_call rejected: invalid number ${JSON.stringify(transferTo)}`);
                  await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ error: 'Invalid phone number format', status: 'rejected' }));
                  return;
                }
                console.log(`[PATTER] Transferring call to ${transferTo}`);
                await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ status: 'transferring', to: transferTo }));
                if (this.config.twilioSid && this.config.twilioToken && callSid) {
                  const transferUrl = `https://api.twilio.com/2010-04-01/Accounts/${this.config.twilioSid}/Calls/${callSid}.json`;
                  await fetch(transferUrl, {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/x-www-form-urlencoded',
                      'Authorization': `Basic ${Buffer.from(`${this.config.twilioSid}:${this.config.twilioToken}`).toString('base64')}`,
                    },
                    body: new URLSearchParams({ Twiml: `<Response><Dial>${xmlEscape(transferTo)}</Dial></Response>` }).toString(),
                  });
                  console.log(`[PATTER] Call transferred to ${transferTo}`);
                }
                if (this.onTranscript) {
                  await this.onTranscript({ role: 'system', text: `Call transferred to ${transferTo}`, call_id: callSid });
                }
                return; // Exit event handler
              } else if (fc.name === 'end_call') {
                // System tool — end the call
                let endCallArgs: { reason?: string };
                try {
                  endCallArgs = JSON.parse(fc.arguments || '{}') as { reason?: string };
                } catch {
                  endCallArgs = {};
                }
                const reason = endCallArgs.reason ?? 'conversation_complete';
                console.log(`[PATTER] Ending call: ${reason}`);
                await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ status: 'ending', reason }));
                if (this.config.twilioSid && this.config.twilioToken && callSid) {
                  const endUrl = `https://api.twilio.com/2010-04-01/Accounts/${this.config.twilioSid}/Calls/${callSid}.json`;
                  await fetch(endUrl, {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/x-www-form-urlencoded',
                      'Authorization': `Basic ${Buffer.from(`${this.config.twilioSid}:${this.config.twilioToken}`).toString('base64')}`,
                    },
                    body: new URLSearchParams({ Status: 'completed' }).toString(),
                  });
                }
                if (this.onTranscript) {
                  await this.onTranscript({ role: 'system', text: `Call ended: ${reason}`, call_id: callSid });
                }
                return; // Exit event handler
              }
              const toolDef = this.agent.tools?.find((t) => t.name === fc.name);
              if (toolDef?.webhookUrl) {
                let parsedArgs: unknown;
                try {
                  parsedArgs = JSON.parse(fc.arguments || '{}');
                } catch {
                  parsedArgs = {};
                }
                let result = '';
                try {
                  validateWebhookUrl(toolDef.webhookUrl);
                } catch (e) {
                  console.error(`[PATTER] Tool webhook URL rejected: ${String(e)}`);
                  await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ error: String(e), fallback: true }));
                  return;
                }
                for (let attempt = 0; attempt < 3; attempt++) {
                  try {
                    const resp = await fetch(toolDef.webhookUrl, {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        tool: fc.name,
                        arguments: parsedArgs,
                        call_id: callSid,
                        caller,
                        attempt: attempt + 1,
                      }),
                      signal: AbortSignal.timeout(10_000),
                    });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    result = JSON.stringify(await resp.json() as unknown);
                    // Cap response body at 64 KB to prevent oversized payloads from malicious webhooks
                    if (result.length > 65_536) {
                      console.warn(`[PATTER] Tool webhook response truncated from ${result.length} bytes to 64KB`);
                      result = result.slice(0, 65_536);
                    }
                    break;
                  } catch (e) {
                    if (attempt < 2) {
                      console.log(`[PATTER] Tool webhook retry ${attempt + 1}: ${String(e)}`);
                      await new Promise<void>((r) => setTimeout(r, 500));
                    } else {
                      result = JSON.stringify({ error: `Tool failed after 3 attempts: ${String(e)}`, fallback: true });
                    }
                  }
                }
                await adapter.sendFunctionResult(fc.call_id, result);
              }
            }
            } catch (err) {
              console.error('[PATTER] Adapter event handler error:', err);
            }
          });
        }

      } else if (event === 'media') {
        const payload = data.media?.payload ?? '';
        const provider = this.agent.provider ?? 'openai_realtime';
        if (provider === 'pipeline' && stt && !isSpeaking) {
          stt.sendAudio(Buffer.from(payload, 'base64'));
        } else if (adapter) {
          adapter.sendAudio(Buffer.from(payload, 'base64'));
        }
      } else if (event === 'mark') {
        // mark.name tracks last confirmed audio chunk (used for barge-in accuracy)
      } else if (event === 'dtmf') {
        const digit = data.dtmf?.digit ?? '';
        console.log(`[PATTER] DTMF: ${digit}`);
        if (adapter instanceof OpenAIRealtimeAdapter) {
          await adapter.sendText(`The user pressed key ${digit} on their phone keypad.`);
        }
        if (this.onTranscript) {
          await this.onTranscript({ role: 'user', text: `[DTMF: ${digit}]`, call_id: callSid });
        }
      } else if (event === 'stop') {
        stt?.close();
        adapter?.close();
        if (!callEndFired && this.onCallEnd) {
          callEndFired = true;
          await this.onCallEnd({ call_id: callSid, transcript: [...conversationHistory] });
        }
      }
      } catch (err) {
        console.error('[PATTER] Stream handler error:', err);
      }
    });

    ws.on('close', () => {
      if (!callEndFired && this.onCallEnd) {
        callEndFired = true;
        this.onCallEnd({ call_id: callSid, transcript: [...conversationHistory] }).catch(() => {});
      }
      stt?.close();
      adapter?.close();
    });
  }

  private handleTelnyxStream(ws: WSWebSocket, url: URL): void {
    const caller = url.searchParams.get('caller') ?? '';
    const callee = url.searchParams.get('callee') ?? '';
    let adapter: AIAdapter | null = null;
    let callControlId = '';
    let streamStarted = false;

    // Conversation history — accumulated per call, passed to callbacks (capped at 200 entries)
    const conversationHistory: Array<{ role: string; text: string; timestamp: number }> = [];
    const pushHistory = (entry: { role: string; text: string; timestamp: number }) => {
      if (conversationHistory.length >= 200) conversationHistory.shift();
      conversationHistory.push(entry);
    };

    // Pipeline mode state
    let stt: DeepgramSTT | WhisperSTT | null = null;
    let tts: ElevenLabsTTS | OpenAITTS | null = null;
    let isSpeaking = false;

    // Guard to ensure onCallEnd is fired exactly once per call
    let callEndFired = false;

    console.log('[PATTER] WebSocket connection opened (Telnyx)');

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
        console.error('[PATTER] Failed to parse Telnyx WS message:', e);
        return;
      }

      const eventType = data.event_type ?? '';
      console.log(`[PATTER] Telnyx event: ${eventType}`);

      if (eventType === 'stream_started' && !streamStarted) {
        streamStarted = true;
        callControlId = data.payload?.call_control_id ?? '';
        console.log(`[PATTER] Telnyx stream started: ${callControlId}`);

        if (this.onCallStart) {
          await this.onCallStart({
            call_id: callControlId,
            caller,
            callee,
            direction: 'inbound',
          });
        }

        // Resolve dynamic variables in system prompt (agent.variables only for Telnyx,
        // since Telnyx does not pass customParameters in the same way as Twilio).
        const agentVarsTelnyx = sanitizeVariables(this.agent.variables ?? {});
        const resolvedPromptTelnyx = Object.keys(agentVarsTelnyx).length > 0
          ? resolveVariables(this.agent.systemPrompt, agentVarsTelnyx)
          : this.agent.systemPrompt;

        const provider = this.agent.provider ?? 'openai_realtime';

        if (provider === 'pipeline') {
          // ---- Pipeline mode: configurable STT + TTS ----

          // Create STT: prefer agent.stt config, fall back to agent.deepgramKey
          if (this.agent.stt) {
            if (this.agent.stt.provider === 'deepgram') {
              // Telnyx sends 16 kHz PCM — use linear16 encoding
              stt = new DeepgramSTT(this.agent.stt.apiKey, this.agent.stt.language ?? 'en', 'nova-3', 'linear16', 16000);
            } else if (this.agent.stt.provider === 'whisper') {
              stt = new WhisperSTT(this.agent.stt.apiKey, 'whisper-1', this.agent.stt.language ?? 'en');
            }
          } else if (this.agent.deepgramKey) {
            // Telnyx sends 16 kHz PCM — use linear16 encoding
            stt = new DeepgramSTT(this.agent.deepgramKey, this.agent.language ?? 'en', 'nova-3', 'linear16', 16000);
          }

          // Create TTS: prefer agent.tts config, fall back to agent.elevenlabsKey
          if (this.agent.tts) {
            if (this.agent.tts.provider === 'elevenlabs') {
              tts = new ElevenLabsTTS(this.agent.tts.apiKey, this.agent.tts.voice ?? '21m00Tcm4TlvDq8ikWAM');
            }
            if (this.agent.tts.provider === 'openai') {
              tts = new OpenAITTS(this.agent.tts.apiKey, this.agent.tts.voice ?? 'alloy');
            }
            // other tts providers can be added here
          } else if (this.agent.elevenlabsKey) {
            const voiceId = (this.agent.voice && this.agent.voice !== 'alloy')
              ? this.agent.voice
              : '21m00Tcm4TlvDq8ikWAM';
            tts = new ElevenLabsTTS(this.agent.elevenlabsKey, voiceId);
          }

          if (!stt) {
            console.log('[PATTER] Pipeline mode (Telnyx): no STT configured');
          }
          if (!tts) {
            console.log('[PATTER] Pipeline mode (Telnyx): no TTS configured');
          }

          try {
            if (stt) await stt.connect();
            console.log('[PATTER] Pipeline mode (Telnyx): STT + TTS connected');
          } catch (e) {
            console.error('[PATTER] Pipeline connect FAILED (Telnyx):', e);
            return;
          }

          if (this.agent.firstMessage && !this.onMessage && tts) {
            try {
              for await (const chunk of tts.synthesizeStream(this.agent.firstMessage)) {
                const encoded = chunk.toString('base64');
                ws.send(JSON.stringify({ event_type: 'media', payload: { audio: { chunk: encoded } } }));
              }
            } catch (e) {
              console.error('[PATTER] First message TTS error (Telnyx):', e);
            }
          }

          if (stt) stt.onTranscript(async (transcript) => {
            if (!transcript.isFinal || !transcript.text) return;

            console.log(`[PATTER] User (Telnyx pipeline): ${sanitizeLogValue(transcript.text)}`);

            pushHistory({ role: 'user', text: transcript.text, timestamp: Date.now() });

            if (this.onTranscript) {
              await this.onTranscript({
                role: 'user',
                text: transcript.text,
                call_id: callControlId,
                history: [...conversationHistory],
              });
            }

            if (!this.onMessage) return;

            let responseText: string;
            try {
              responseText = await this.onMessage({
                text: transcript.text,
                call_id: callControlId,
                caller,
                history: [...conversationHistory],
              });
            } catch (e) {
              console.error('[PATTER] onMessage error (Telnyx):', e);
              return;
            }

            if (!responseText) return;

            pushHistory({ role: 'assistant', text: responseText, timestamp: Date.now() });

            isSpeaking = true;
            try {
              for await (const chunk of tts!.synthesizeStream(responseText)) {
                if (!isSpeaking) break;
                const encoded = chunk.toString('base64');
                ws.send(JSON.stringify({ event_type: 'media', payload: { audio: { chunk: encoded } } }));
              }
            } catch (e) {
              console.error('[PATTER] TTS streaming error (Telnyx):', e);
            } finally {
              isSpeaking = false;
            }
          });

        } else {
          // ---- OpenAI Realtime / ElevenLabs ConvAI mode ----
          adapter = buildAIAdapter(this.config, this.agent, resolvedPromptTelnyx);
          try {
            await adapter.connect();
            console.log('[PATTER] AI adapter connected (Telnyx)');
          } catch (e) {
            console.error('[PATTER] AI adapter connect FAILED (Telnyx):', e);
            return;
          }

          if (this.agent.firstMessage && adapter instanceof OpenAIRealtimeAdapter) {
            await adapter.sendText(this.agent.firstMessage);
          }

          adapter.onEvent(async (type, eventData) => {
            try {
            if (type === 'audio') {
              const encoded = (eventData as Buffer).toString('base64');
              ws.send(
                JSON.stringify({
                  event_type: 'media',
                  payload: { audio: { chunk: encoded } },
                }),
              );
            } else if (type === 'transcript_input') {
              const inputTextTelnyx = eventData as string;
              console.log(`[PATTER] User (Telnyx): ${sanitizeLogValue(inputTextTelnyx)}`);
              pushHistory({ role: 'user', text: inputTextTelnyx, timestamp: Date.now() });
              if (this.onTranscript) {
                await this.onTranscript({
                  role: 'user',
                  text: inputTextTelnyx,
                  call_id: callControlId,
                  history: [...conversationHistory],
                });
              }
            } else if (type === 'transcript_output') {
              const outputTextTelnyx = eventData as string;
              if (outputTextTelnyx) {
                const triggeredTelnyx = checkGuardrails(outputTextTelnyx, this.agent.guardrails);
                if (triggeredTelnyx) {
                  console.log(`[PATTER] Guardrail '${triggeredTelnyx.name}' triggered`);
                  if (adapter instanceof OpenAIRealtimeAdapter) {
                    adapter.cancelResponse();
                    await adapter.sendText(triggeredTelnyx.replacement ?? "I'm sorry, I can't respond to that.");
                  }
                }
                pushHistory({ role: 'assistant', text: outputTextTelnyx, timestamp: Date.now() });
              }
            } else if (type === 'speech_started' || type === 'interruption') {
              ws.send(JSON.stringify({ event_type: 'media_stop' }));
              if (adapter instanceof OpenAIRealtimeAdapter) {
                adapter.cancelResponse();
              }
            } else if (type === 'function_call' && adapter instanceof OpenAIRealtimeAdapter) {
              const fc = eventData as { call_id: string; name: string; arguments: string };
              if (fc.name === 'transfer_call') {
                // System tool — transfer the call (Telnyx)
                let transferArgs: { number?: string };
                try {
                  transferArgs = JSON.parse(fc.arguments || '{}') as { number?: string };
                } catch {
                  transferArgs = {};
                }
                const rawTransferTo = transferArgs.number ?? '';
                if (!isValidE164(rawTransferTo)) {
                  console.warn(`[PATTER] transfer_call rejected (Telnyx): invalid number ${JSON.stringify(rawTransferTo)}`);
                  await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ error: 'Invalid phone number format', status: 'rejected' }));
                  return;
                }
                const transferTo = xmlEscape(rawTransferTo);
                console.log(`[PATTER] Transferring Telnyx call to ${transferTo}`);
                await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ status: 'transferring' }));
                const telnyxKey = this.config.telnyxKey ?? '';
                await fetch(`https://api.telnyx.com/v2/calls/${callControlId}/actions/transfer`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
                  body: JSON.stringify({ to: rawTransferTo }),
                });
                if (this.onTranscript) {
                  await this.onTranscript({ role: 'system', text: `Call transferred to ${transferTo}`, call_id: callControlId });
                }
                return;
              } else if (fc.name === 'end_call') {
                // System tool — end the call (Telnyx)
                let endArgs: { reason?: string };
                try {
                  endArgs = JSON.parse(fc.arguments || '{}') as { reason?: string };
                } catch {
                  endArgs = {};
                }
                const reason = endArgs.reason ?? 'conversation_complete';
                console.log(`[PATTER] Ending call (Telnyx): ${reason}`);
                await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ status: 'ending', reason }));
                if (this.onTranscript) {
                  await this.onTranscript({ role: 'system', text: `Call ended: ${reason}`, call_id: callControlId });
                }
                ws.close();
                return;
              }
              const toolDef = this.agent.tools?.find((t) => t.name === fc.name);
              if (toolDef?.webhookUrl) {
                let parsedArgsTelnyx: unknown;
                try {
                  parsedArgsTelnyx = JSON.parse(fc.arguments || '{}');
                } catch {
                  parsedArgsTelnyx = {};
                }
                let result = '';
                try {
                  validateWebhookUrl(toolDef.webhookUrl);
                } catch (e) {
                  console.error(`[PATTER] Tool webhook URL rejected (Telnyx): ${String(e)}`);
                  await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ error: String(e), fallback: true }));
                  return;
                }
                for (let attempt = 0; attempt < 3; attempt++) {
                  try {
                    const resp = await fetch(toolDef.webhookUrl, {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        tool: fc.name,
                        arguments: parsedArgsTelnyx,
                        call_id: callControlId,
                        caller,
                        attempt: attempt + 1,
                      }),
                      signal: AbortSignal.timeout(10_000),
                    });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    result = JSON.stringify(await resp.json() as unknown);
                    // Cap response body at 64 KB to prevent oversized payloads from malicious webhooks
                    if (result.length > 65_536) {
                      console.warn(`[PATTER] Tool webhook response truncated from ${result.length} bytes to 64KB (Telnyx)`);
                      result = result.slice(0, 65_536);
                    }
                    break;
                  } catch (e) {
                    if (attempt < 2) {
                      console.log(`[PATTER] Tool webhook retry ${attempt + 1} (Telnyx): ${String(e)}`);
                      await new Promise<void>((r) => setTimeout(r, 500));
                    } else {
                      result = JSON.stringify({ error: `Tool failed after 3 attempts: ${String(e)}`, fallback: true });
                    }
                  }
                }
                await adapter.sendFunctionResult(fc.call_id, result);
              }
            }
            } catch (err) {
              console.error('[PATTER] Adapter event handler error (Telnyx):', err);
            }
          });
        }

      } else if (eventType === 'media') {
        const audioChunk = data.payload?.audio?.chunk ?? '';
        if (!audioChunk) return;

        const provider = this.agent.provider ?? 'openai_realtime';
        if (provider === 'pipeline' && stt && !isSpeaking) {
          // Telnyx sends 16 kHz PCM — send directly to Deepgram
          stt.sendAudio(Buffer.from(audioChunk, 'base64'));
        } else if (adapter) {
          adapter.sendAudio(Buffer.from(audioChunk, 'base64'));
        }

      } else if (eventType === 'stream_stopped') {
        stt?.close();
        adapter?.close();
        if (!callEndFired && this.onCallEnd) {
          callEndFired = true;
          await this.onCallEnd({ call_id: callControlId, transcript: [...conversationHistory] });
        }
      }
      } catch (err) {
        console.error('[PATTER] Stream handler error (Telnyx):', err);
      }
    });

    ws.on('close', () => {
      if (!callEndFired && this.onCallEnd) {
        callEndFired = true;
        this.onCallEnd({ call_id: callControlId, transcript: [...conversationHistory] }).catch(() => {});
      }
      stt?.close();
      adapter?.close();
    });
  }

  async stop(): Promise<void> {
    if (!this.server) return;
    return new Promise((resolve) => {
      this.server!.close(() => resolve());
    });
  }
}

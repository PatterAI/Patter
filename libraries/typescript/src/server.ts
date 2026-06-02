/**
 * Embedded HTTP/WebSocket server — wires Express webhooks for the configured
 * carrier (Twilio or Telnyx) into the per-call `StreamHandler` and dashboard.
 */

import crypto from 'node:crypto';
import express from 'express';
import { createServer, Server as HTTPServer } from 'http';
import { WebSocketServer, WebSocket as WSWebSocket } from 'ws';
import { OpenAIRealtimeAdapter } from './providers/openai-realtime';
import { OpenAIRealtime2Adapter } from './providers/openai-realtime-2';
import { ElevenLabsConvAIAdapter } from './providers/elevenlabs-convai';
import { PlivoAdapter, dropPlivoVoicemail } from './providers/plivo-adapter';
import { PlivoBridge, classifyPlivoAmd, validatePlivoSignature } from './telephony/plivo';
// Re-export so existing imports from './server' keep working after the
// extraction of PlivoBridge into ./telephony/plivo.
export { PlivoBridge } from './telephony/plivo';
import { createSTT } from './provider-factory';
import type { STTAdapter } from './provider-factory';
import { CallMetricsAccumulator } from './metrics';
import { mergePricing } from './pricing';
import { MetricsStore } from './dashboard/store';
import { mountDashboard, mountApi } from './dashboard/routes';
import { RemoteMessageHandler } from './remote-message';
import { StreamHandler, sanitizeLogValue } from './stream-handler';
import { getLogger } from './logger';
import type { TelephonyBridge } from './stream-handler';
import type {
  AgentOptions,
  ToolDefinition,
  PipelineMessageHandler,
  MachineDetectionResult,
  CarrierKind,
  CallOutcome,
  CallResult,
} from './types';
import type { CallMetrics, CostBreakdown } from './metrics';
import { CallLogger, resolveLogRoot } from './services/call-log';

/** Resolved configuration consumed by `EmbeddedServer` (carrier credentials, webhook URL, etc.). */
export interface LocalConfig {
  twilioSid?: string;
  twilioToken?: string;
  openaiKey?: string;
  phoneNumber: string;
  webhookUrl: string;
  telephonyProvider?: CarrierKind;
  telnyxKey?: string;
  telnyxConnectionId?: string;
  /** Plivo Auth ID — HTTP Basic username for the Plivo REST API. */
  plivoAuthId?: string;
  /** Plivo Auth Token — Basic password AND the V3 webhook signature key. */
  plivoAuthToken?: string;
  /**
   * Telnyx Ed25519 public key (base64-encoded, DER/SPKI format) used to verify
   * incoming webhook signatures. Obtain from the Telnyx portal under
   * API Keys → Webhook Keys. When provided, unauthenticated webhook requests
   * are rejected with HTTP 403.
   */
  telnyxPublicKey?: string;
  /**
   * SECURITY: require valid webhook signatures on both Twilio and Telnyx
   * inbound webhooks. When True (the default), a missing credential
   * (twilioToken / telnyxPublicKey) causes the webhook to return
   * 503 Service Unavailable instead of silently accepting the request.
   * Set to false only for local development against mock providers.
   */
  requireSignature?: boolean;
  /**
   * Resolved on-disk persistence root for the dashboard's call history,
   * or ``null`` to disable. Computed by ``client.ts`` from the public
   * ``LocalOptions.persist`` option (with ``PATTER_LOG_DIR`` env-var
   * fallback). When ``null``, `CallLogger` is a no-op and the dashboard
   * is in-memory-only — restarts wipe history.
   */
  persistRoot?: string | null;
}

type AIAdapter = OpenAIRealtimeAdapter | ElevenLabsConvAIAdapter;

export const TRANSFER_CALL_TOOL = {
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

export const END_CALL_TOOL = {
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
 * Map a Twilio ``AnsweredBy`` value to the carrier-agnostic
 * {@link MachineDetectionResult.classification}. Anything unrecognised
 * collapses to ``unknown`` rather than throwing — Twilio occasionally
 * adds new AMD outcomes (e.g. fax variants) and we don't want a webhook
 * to 500 because of an unknown enum value.
 */
function classifyTwilioAmd(answeredBy: string): MachineDetectionResult['classification'] {
  if (answeredBy === 'human') return 'human';
  if (answeredBy.startsWith('machine_')) return 'machine';
  if (answeredBy === 'fax') return 'fax';
  return 'unknown';
}

/**
 * Map a Telnyx ``call.machine.detection.ended.result`` value to the
 * carrier-agnostic classification. Telnyx uses ``human`` / ``machine``
 * (and historically ``machine_detected``) / ``not_sure`` / ``fax``.
 */
function classifyTelnyxAmd(result: string): MachineDetectionResult['classification'] {
  if (result === 'human') return 'human';
  if (result === 'machine' || result === 'machine_detected') return 'machine';
  if (result === 'fax') return 'fax';
  return 'unknown';
}

/**
 * Map a no-media Twilio terminal ``CallStatus`` to a {@link CallResult}
 * outcome. Only called for statuses that imply the call never reached the
 * media stream (``no-answer`` / ``busy`` / ``failed`` / ``canceled``);
 * connected calls resolve via ``onCallEnd`` instead. Mirrors Python's
 * ``_twilio_status_to_outcome``.
 */
export function twilioStatusToOutcome(callStatus: string): CallOutcome {
  const s = (callStatus || '').toLowerCase();
  if (s === 'no-answer') return 'no_answer';
  if (s === 'busy') return 'busy';
  return 'failed'; // failed / canceled / any other terminal no-media status
}

/**
 * Map a Telnyx ``hangup_cause`` to a no-media {@link CallResult} outcome, or
 * ``null`` when the cause implies the call connected (``normal_clearing``).
 *
 * Connected calls return ``null`` here so they resolve via ``onCallEnd`` with
 * the full transcript + metrics rather than being prematurely closed as a
 * no-media outcome. Mirrors Python's ``_telnyx_hangup_outcome``.
 */
export function telnyxHangupOutcome(cause: string): CallOutcome | null {
  const c = (cause || '').toLowerCase();
  if (c === 'no_answer' || c === 'timeout' || c === 'no_user_response') return 'no_answer';
  if (c === 'user_busy' || c === 'busy') return 'busy';
  if (c === 'call_rejected' || c === 'rejected' || c === 'destination_out_of_order') return 'failed';
  return null;
}

/**
 * Validate that a webhook URL is safe to fetch (SSRF protection).
 *
 * Blocks:
 *   - Non-HTTP(S) schemes (``file:``, ``javascript:``, etc.)
 *   - IPv4 private, loopback, link-local, reserved ranges
 *     (127/8, 10/8, 172.16/12, 192.168/16, 169.254/16, 0/8)
 *   - IPv6 loopback and aliases (``::1``, ``::``, ``ip6-localhost``,
 *     ``ip6-loopback``), unique-local (``fc00::/7``) and link-local
 *     (``fe80::/10``) ranges
 *   - Localhost hostnames (``localhost``) and cloud-metadata hostnames
 *     (``metadata``, ``metadata.google.internal``, ``metadata.azure.com``)
 *
 * Mirrors Python's ``ipaddress.ip_address(...).is_private /
 * .is_loopback / .is_link_local / .is_reserved`` behaviour.
 *
 * URLs validated here are SDK-user config, not caller-derived input. When
 * *allowLoopback* is ``true`` (opt-in, consult tool only) the loopback /
 * private / link-local rejections AND the cloud-metadata hostname block are
 * skipped, letting a developer point at a trusted local agent. The scheme
 * check is NEVER relaxed — non-HTTP(S) URLs are always rejected. Every other
 * caller relies on the strict default (``allowLoopback = false``).
 *
 * @param url            The webhook URL to validate.
 * @param allowLoopback  Opt-in: permit loopback/private/link-local hosts
 *                       (default ``false`` — strict SSRF guard).
 */
export function validateWebhookUrl(url: string, allowLoopback = false): void {
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error(`Invalid webhook URL scheme: ${parsed.protocol}`);
  }
  // Node's URL parser preserves IPv6 brackets on ``hostname`` — strip them so
  // raw IPv6 literal checks can match. Lowercase for case-insensitive
  // hostname/IP comparisons (hex digits are case-insensitive in IPv6).
  const rawHost = parsed.hostname;
  const host = rawHost.replace(/^\[/, '').replace(/\]$/, '').toLowerCase();

  // ``allowLoopback`` is an opt-in escape hatch for trusted, developer-
  // configured local agents (the consult tool). It relaxes the loopback /
  // private / link-local rejections below but NEVER the scheme check above —
  // a developer-specified URL is still not allowed to be ``file:`` etc. Every
  // other caller passes the strict default (``false``).
  if (allowLoopback) {
    return;
  }

  // --- Blocked hostnames (case-insensitive, exact match) ------------------
  const BLOCKED_HOSTNAMES = new Set([
    'localhost',
    'ip6-localhost',
    'ip6-loopback',
    'metadata',
    'metadata.google.internal',
    'metadata.azure.com',
  ]);
  if (BLOCKED_HOSTNAMES.has(host)) {
    throw new Error(`Webhook URL blocked: ${rawHost} is a private/internal address`);
  }

  // --- IPv4 literal checks ------------------------------------------------
  const IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
  const v4 = IPV4_RE.exec(host);
  if (v4) {
    const oct = v4.slice(1, 5).map((s) => parseInt(s, 10));
    if (oct.some((n) => n < 0 || n > 255)) {
      throw new Error(`Webhook URL blocked: ${rawHost} is not a valid IPv4 address`);
    }
    const [a, b] = oct;
    if (
      a === 0 ||                              // 0.0.0.0/8 (any 0.x)
      a === 10 ||                             // 10.0.0.0/8
      a === 127 ||                            // 127.0.0.0/8 loopback
      (a === 169 && b === 254) ||             // 169.254.0.0/16 link-local
      (a === 172 && b >= 16 && b <= 31) ||    // 172.16.0.0/12
      (a === 192 && b === 168)                // 192.168.0.0/16
    ) {
      throw new Error(`Webhook URL blocked: ${rawHost} is a private/internal address`);
    }
    return;
  }

  // --- IPv6 literal checks (after bracket strip) --------------------------
  // Heuristic detection: IPv6 literals contain ':'.
  if (host.includes(':')) {
    // Loopback / unspecified
    if (host === '::1' || host === '::') {
      throw new Error(`Webhook URL blocked: ${rawHost} is a private/internal address`);
    }
    // Unique local fc00::/7 — first hex group starts with "fc" or "fd"
    if (/^fc[0-9a-f]{0,2}:/.test(host) || /^fd[0-9a-f]{0,2}:/.test(host)) {
      throw new Error(`Webhook URL blocked: ${rawHost} is a private/internal address`);
    }
    // Link-local fe80::/10 — first hex group in [fe80, febf]
    if (/^fe[89ab][0-9a-f]?:/.test(host)) {
      throw new Error(`Webhook URL blocked: ${rawHost} is a private/internal address`);
    }
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
    // Reject if timestamp is missing or too old (replay attack protection).
    // Telnyx sends ``telnyx-timestamp`` as seconds since epoch (per docs:
    // https://developers.telnyx.com/docs/messaging/webhooks#webhook-signing).
    // Heuristic: any value below 1e12 is seconds (a 2026 epoch in seconds is
    // ~1.77e9, while milliseconds is ~1.77e12), so we promote to ms before
    // comparing. This stays correct if Telnyx ever switches the unit.
    const ts = parseInt(timestamp, 10);
    if (!Number.isFinite(ts)) return false;
    const tsMs = ts < 1e12 ? ts * 1000 : ts;
    const ageMs = Date.now() - tsMs;
    if (ageMs < 0 || ageMs > toleranceSec * 1000) return false;

    const payload = `${timestamp}|${rawBody}`;
    const keyBuffer = Buffer.from(publicKey, 'base64');

    // Node 15+ supports Ed25519 natively via createPublicKey / verify
    const keyObject = crypto.createPublicKey({
      key: keyBuffer,
      format: 'der',
      type: 'spki',
    });

    // The telnyx-signature-ed25519 header may contain multiple comma-separated
    // signatures during key rotation. Accept the webhook if any one of them
    // verifies; fail-closed when none match (mirrors Python server.py:69-81).
    for (const rawSig of signature.split(',')) {
      const trimmed = rawSig.trim();
      if (!trimmed) continue;
      try {
        const sigBuffer = Buffer.from(trimmed, 'base64');
        if (crypto.verify(null, Buffer.from(payload), keyObject, sigBuffer)) {
          return true;
        }
      } catch {
        // Malformed signature entry — try the next one.
        continue;
      }
    }
    return false;
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
    const sigBuf = Buffer.from(signature);
    const expBuf = Buffer.from(expected);
    // timingSafeEqual throws when buffer lengths differ. Compare lengths
    // explicitly first — buffer length is not a secret, so an early return
    // on mismatch does not leak timing information about the secret itself.
    if (sigBuf.length !== expBuf.length) return false;
    return crypto.timingSafeEqual(sigBuf, expBuf);
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
 * Credentials come from the engine instance attached to ``agent.engine``
 * (v0.5.0+). OpenAI falls back to ``config.openaiKey`` when no engine is set.
 */
export function buildAIAdapter(config: LocalConfig, agent: AgentOptions, resolvedPrompt?: string, toolsOverride?: readonly ToolDefinition[]): AIAdapter {
  const engine = agent.engine;
  if (agent.provider === 'elevenlabs_convai') {
    if (!engine || engine.kind !== 'elevenlabs_convai') {
      throw new Error(
        "ElevenLabs ConvAI mode requires `agent.engine = new ElevenLabsConvAI({...})`.",
      );
    }
    return new ElevenLabsConvAIAdapter(
      engine.apiKey,
      engine.agentId,
      agent.voice ?? 'EXAVITQu4vr4xnSDxMaL',
      agent.firstMessage ?? '',
    );
  }
  // Always inject transfer_call and end_call system tools alongside agent-defined tools.
  // ``strict`` is propagated when the user opts in — Patter does not flip it on
  // by default because OpenAI strict mode requires every property in ``required``
  // and ``additionalProperties: false`` everywhere, which would break tools with
  // optional fields. The user's tool schemas are validated at agent() build time
  // (see tools/schema-validation.ts) so any strict-mode violation surfaces early.
  // ``toolsOverride`` carries the per-call resolved tool list (MCP + consult
  // merges from the stream handler) so those tools are advertised to the
  // Realtime model; falls back to the static ``agent.tools``.
  const agentTools = (toolsOverride ?? agent.tools)?.map((t) => ({
    name: t.name,
    description: t.description,
    parameters: t.parameters,
    strict: (t as { strict?: boolean }).strict,
  })) ?? [];
  const tools = [...agentTools, TRANSFER_CALL_TOOL, END_CALL_TOOL];
  const isOpenAIEngine = engine && (engine.kind === 'openai_realtime' || engine.kind === 'openai_realtime_2');
  const openaiKey = isOpenAIEngine ? engine.apiKey : (config.openaiKey ?? '');
  // Forward optional engine-level Realtime knobs so the high-level
  // ``OpenAIRealtime`` / ``OpenAIRealtime2`` engine wrappers have the same
  // expressivity as the underlying adapters. Omitting the option keeps the
  // adapter's own defaults — backward compat with users on the prior shape.
  const adapterOptions: import('./providers/openai-realtime').OpenAIRealtimeOptions = {};
  if (isOpenAIEngine) {
    if (engine.reasoningEffort !== undefined) {
      adapterOptions.reasoningEffort = engine.reasoningEffort;
    }
    if (engine.inputAudioTranscriptionModel !== undefined) {
      adapterOptions.inputAudioTranscriptionModel = engine.inputAudioTranscriptionModel;
    }
  }
  // Dispatch to the GA-API adapter when the caller passed the
  // ``OpenAIRealtime2`` engine marker. Falls through to the v1-beta adapter
  // for ``OpenAIRealtime`` and the legacy no-engine code path.
  const AdapterCtor = engine && engine.kind === 'openai_realtime_2'
    ? OpenAIRealtime2Adapter
    : OpenAIRealtimeAdapter;
  return new AdapterCtor(
    openaiKey,
    agent.model,
    agent.voice,
    resolvedPrompt ?? agent.systemPrompt,
    tools,
    undefined,
    adapterOptions,
  );
}

// ---------------------------------------------------------------------------
// Telephony bridge implementations
// ---------------------------------------------------------------------------

/** Twilio-specific telephony bridge. */
class TwilioBridge implements TelephonyBridge {
  readonly label = 'Twilio';
  readonly telephonyProvider = 'twilio' as const;
  readonly inputWireFormat = 'ulaw_8000' as const;

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
      const E164_RE = /^\+[1-9]\d{6,14}$/;
      if (!E164_RE.test(toNumber)) {
        getLogger().warn(`TwilioBridge.transferCall rejected: invalid target ${JSON.stringify(toNumber)}`);
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

  createStt(agent: AgentOptions): Promise<STTAdapter | null> {
    // In v0.5.0+ the adapter is pre-instantiated and already configured for
    // the transcoded pipeline stream (PCM16 16 kHz). Transcoding happens in
    // ``StreamHandler.handleAudio``.
    return createSTT(agent);
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
      } catch (err) {
        // Fallback to estimated cost. Mirrors Py handlers/twilio_handler.py:538-539.
        getLogger().debug(
          `queryTelephonyCost(twilio) failed: ${(err as Error)?.message ?? err}`,
        );
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

/**
 * DTMF digits accepted by the Telnyx `send_dtmf` command.
 *
 * ``w`` / ``W`` are Telnyx-specific pause characters (each inserts a 500 ms
 * wait before the next digit). They are sent as-is in the ``digits`` payload
 * — Telnyx interprets them server-side. Mirrors the Python ``_DTMF_ALLOWED``
 * set in ``libraries/python/getpatter/handlers/telnyx_handler.py``.
 */
const TELNYX_DTMF_ALLOWED = new Set('0123456789*#ABCDabcdwW');
const TELNYX_DTMF_DURATION_MS = 250;

async function sleep(ms: number): Promise<void> {
  if (ms <= 0) return;
  await new Promise((resolve) => setTimeout(resolve, ms));
}

/** Telnyx-specific telephony bridge. */
export class TelnyxBridge implements TelephonyBridge {
  readonly label = 'Telnyx';
  readonly telephonyProvider = 'telnyx' as const;
  // ``streaming_start`` negotiates PCMU bidirectional by default — keeping
  // ``ulaw_8000`` here matches what TwilioBridge does and keeps the stream
  // handler's input-transcode branch in the right shape. If a deployment
  // overrides the negotiation to L16, this should flip to ``pcm_16000``.
  readonly inputWireFormat = 'ulaw_8000' as const;

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
    await fetch(`https://api.telnyx.com/v2/calls/${encodeURIComponent(callId)}/actions/transfer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
      body: JSON.stringify({ to: toNumber }),
    });
    getLogger().info(`Telnyx call transferred to ${toNumber}`);
  }

  async sendDtmf(_ws: WSWebSocket, callId: string, digits: string, delayMs: number): Promise<void> {
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
      await fetch(`https://api.telnyx.com/v2/calls/${encodeURIComponent(callId)}/actions/send_dtmf`, {
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
      const resp = await fetch(`https://api.telnyx.com/v2/calls/${encodeURIComponent(callId)}/actions/record_start`, {
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
      const resp = await fetch(`https://api.telnyx.com/v2/calls/${encodeURIComponent(callId)}/actions/record_stop`, {
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

  async endCall(callId: string, _ws: WSWebSocket): Promise<void> {
    // Hang up via Telnyx Call Control API. We intentionally do NOT close the
    // media WebSocket here — Telnyx will emit a ``stop`` frame in response
    // to the hangup, and the stream handler's ``stop`` processing drives the
    // WebSocket close (matches the Python ``_telnyx_hangup`` helper which
    // never touches the WS). Closing it here races with the carrier's stop
    // frame and truncates in-flight media.
    const telnyxKey = this.config.telnyxKey ?? '';
    if (callId && telnyxKey) {
      try {
        await fetch(`https://api.telnyx.com/v2/calls/${encodeURIComponent(callId)}/actions/hangup`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${telnyxKey}` },
          body: JSON.stringify({}),
        });
      } catch { /* best effort — call may already be ended */ }
    }
  }

  createStt(agent: AgentOptions): Promise<STTAdapter | null> {
    return createSTT(agent);
  }

  async queryTelephonyCost(metricsAcc: CallMetricsAccumulator, callId: string): Promise<void> {
    if (this.config.telnyxKey && callId) {
      try {
        const resp = await fetch(
          `https://api.telnyx.com/v2/calls/${encodeURIComponent(callId)}`,
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
      } catch (err) {
        // Fallback to estimated cost. Mirrors Py handlers/twilio_handler.py:538-539.
        getLogger().debug(
          `queryTelephonyCost(telnyx) failed: ${(err as Error)?.message ?? err}`,
        );
      }
    }
  }
}

// ---------------------------------------------------------------------------
// EmbeddedServer
// ---------------------------------------------------------------------------

/** Maximum seconds to wait for active calls to finish during graceful shutdown. */
const GRACEFUL_SHUTDOWN_TIMEOUT_MS = 10_000;

/** HTTP+WebSocket server that hosts the carrier webhook surface and per-call media streams. */
export class EmbeddedServer {
  private server: HTTPServer | null = null;
  private wss: WebSocketServer | null = null;
  private twilioTokenWarningLogged = false;
  private telnyxSigWarningLogged = false;
  readonly metricsStore: MetricsStore;
  private readonly pricing: ReturnType<typeof mergePricing>;
  private readonly remoteHandler = new RemoteMessageHandler();
  /**
   * Opt-in per-call filesystem logger. Path is resolved by ``client.ts``
   * from the public ``LocalOptions.persist`` option (with the legacy
   * ``PATTER_LOG_DIR`` env var as fallback). Initialised in the ctor
   * because ``resolveLogRoot`` cannot see ``this.config`` from a field
   * default expression.
   */
  private readonly callLogger: CallLogger;

  /** Active WebSocket connections tracked for graceful shutdown. */
  private readonly activeConnections = new Set<WSWebSocket>();
  private readonly activeCallIds = new Map<WSWebSocket, string>();

  /**
   * Per-call AMD result callbacks keyed by CallSid / call_control_id.
   * Public so ``client.ts`` can register a callback per outbound call.
   * The Map slot is deleted after the callback fires once — preventing
   * cross-call misfires when multiple concurrent outbound calls are in
   * flight (single-slot was a race condition: the last registered callback
   * would win for every in-flight AMD result).
   */
  public onMachineDetectionByCallSid: Map<
    string,
    (result: MachineDetectionResult) => void | Promise<void>
  > = new Map();

  /**
   * Pre-warm first-message audio accessor wired by ``Patter.serve()``.
   * The per-call StreamHandler invokes this with its ``callId`` at the
   * start of the firstMessage emit; a defined return is sent verbatim
   * in place of running TTS again. ``undefined`` means "no prewarm
   * cache for this call — fall back to live synthesis". Default is a
   * no-op so callers that instantiate ``EmbeddedServer`` directly
   * (tests) work without further setup.
   */
  public popPrewarmAudio: (callId: string) => Buffer | undefined = () => undefined;

  /**
   * Pre-warmed provider WebSocket accessor wired by ``Patter.serve()``.
   * The per-call StreamHandler invokes this with its ``callId`` at
   * pipeline init; defined returns hand off pre-opened STT / TTS /
   * Realtime sockets so the live first turn skips the cold-handshake.
   * Default is a no-op for direct ``EmbeddedServer`` callers.
   */
  public popPrewarmedConnections: (
    callId: string,
  ) => import('./client').ParkedProviderConnections | undefined = () => undefined;

  /**
   * Prewarm waste recorder wired by ``Patter.serve()``. Invoked from
   * the Twilio status callback (no-answer / busy / failed / canceled)
   * and the Telnyx call.hangup / AMD-machine handlers so the cache
   * entry is evicted when the call terminates before the media stream
   * starts. Default is a no-op so direct ``EmbeddedServer`` callers
   * (tests) work without further setup. See FIX #91.
   */
  public recordPrewarmWaste: (callId: string) => void = () => undefined;

  /**
   * Per-callId completion deferreds for ``Patter.call({ wait: true })``.
   * Resolved by the FIRST terminal signal: the Twilio/Telnyx status callback
   * for no-media outcomes (no-answer / busy / failed), or ``onCallEnd`` for a
   * connected call (answered / voicemail). The AMD classification is recorded
   * per callId so the connected-call path can distinguish ``answered`` from
   * ``voicemail``. This is what lets ``call({ wait: true })`` resolve to a
   * structured {@link CallResult} without the caller hand-wiring ``onCallEnd``
   * to a promise. Public so ``client.ts`` can register/await + fail in-flight
   * waiters on ``disconnect()``. Mirrors Python's ``EmbeddedServer._completions``.
   */
  public readonly completions = new Map<
    string,
    {
      readonly promise: Promise<CallResult>;
      readonly resolve: (r: CallResult) => void;
      readonly reject: (e: Error) => void;
      done: boolean;
    }
  >();
  /** AMD classification recorded per callId, used by the connected-call path. */
  private readonly amdClass = new Map<string, MachineDetectionResult['classification']>();

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

    // Resolve the persistence root. Prefer the explicit value passed by
    // ``client.ts`` (already resolved from the public ``persist`` option +
    // env-var fallback). When ``persistRoot`` is ``undefined`` (callers
    // that bypass ``client.ts`` and instantiate ``EmbeddedServer``
    // directly, e.g. tests) fall back to the env var. ``null`` is the
    // explicit "off" signal — keep it as null.
    const logRoot = config.persistRoot === undefined
      ? resolveLogRoot()
      : config.persistRoot;
    this.callLogger = new CallLogger(logRoot);

    // Hydrate the dashboard from disk so /api/dashboard/calls survives a
    // restart. CallLogger persists call metadata as JSONL/JSON under
    // ``logRoot``; replay those files into the in-memory ring buffer.
    // No-op when logging is disabled (``logRoot`` is ``null``).
    if (logRoot) {
      try {
        const restored = this.metricsStore.hydrate(logRoot);
        if (restored > 0) {
          getLogger().info(`Dashboard hydrated ${restored} call(s) from ${logRoot}`);
        }
      } catch (err) {
        getLogger().warn(`Dashboard hydration failed: ${String(err)}`);
      }
    }
  }

  // === Outbound completion registry (call({ wait: true })) ===

  /**
   * Register (or return) a completion promise for an outbound call.
   *
   * Called by ``Patter.call({ wait: true })`` immediately after the carrier
   * accepts the dial — the promise resolves to a {@link CallResult} once a
   * terminal signal arrives. Idempotent: returns the existing pending promise
   * if one is already registered for ``callId``. Mirrors Python's
   * ``register_completion``.
   */
  registerCompletion(callId: string): Promise<CallResult> {
    const existing = this.completions.get(callId);
    if (existing && !existing.done) {
      return existing.promise;
    }
    let resolve!: (r: CallResult) => void;
    let reject!: (e: Error) => void;
    const promise = new Promise<CallResult>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    this.completions.set(callId, { promise, resolve, reject, done: false });
    return promise;
  }

  /** Drop a registered completion (e.g. on a backstop timeout) without resolving it. */
  deleteCompletion(callId: string): void {
    this.completions.delete(callId);
    this.amdClass.delete(callId);
  }

  /**
   * Resolve a pending completion with a {@link CallResult}.
   *
   * No-op when no completion is registered for ``callId`` (the common case —
   * most calls are placed without ``wait: true``) or it is already done.
   * Builds the result from the ``onCallEnd`` payload when ``data`` is provided
   * (connected calls carry transcript + {@link CallMetrics}); no-media
   * outcomes pass ``data`` undefined and yield an empty transcript / no cost.
   * Mirrors Python's ``_resolve_completion``.
   */
  resolveCompletion(
    callId: string,
    args: { outcome: CallOutcome; status: string; data?: Record<string, unknown> },
  ): void {
    const entry = this.completions.get(callId);
    if (!entry || entry.done) return;

    const data = args.data;
    const metrics = (data?.metrics ?? null) as CallMetrics | null;
    const cost = (metrics?.cost ?? null) as CostBreakdown | null;
    const durationRaw = metrics?.duration_seconds;
    const duration = typeof durationRaw === 'number' ? durationRaw : 0;
    const transcriptRaw = data?.transcript;
    const transcript = Array.isArray(transcriptRaw)
      ? (transcriptRaw as CallResult['transcript'])
      : [];

    const result: CallResult = {
      callId,
      outcome: args.outcome,
      status: args.status,
      durationSeconds: duration,
      transcript,
      cost,
      metrics,
    };
    entry.done = true;
    entry.resolve(result);
    this.completions.delete(callId);
    this.amdClass.delete(callId);
  }

  /**
   * Fail every in-flight completion with ``error``. Called by
   * ``Patter.disconnect()`` so a ``call({ wait: true })`` awaiter does not
   * hang until its backstop timeout once the server is gone. Mirrors the
   * Python ``disconnect()`` change that fails in-flight ``wait=True`` awaiters.
   */
  failPendingCompletions(error: Error): void {
    for (const entry of this.completions.values()) {
      if (!entry.done) {
        entry.done = true;
        entry.reject(error);
      }
    }
    this.completions.clear();
    this.amdClass.clear();
  }

  /** Bind HTTP + WebSocket listeners on `port`, mount carrier webhooks and dashboard routes. */
  async start(port: number = 8000): Promise<void> {
    const webhookUrlPattern = /^[a-zA-Z0-9][a-zA-Z0-9.\-]+[a-zA-Z0-9]$/;
    if (!webhookUrlPattern.test(this.config.webhookUrl)) {
      throw new Error(`Invalid webhookUrl: must be a hostname with no protocol prefix or path (got: '${this.config.webhookUrl}')`);
    }

    // Startup-time warning when webhook signature enforcement is active but
    // the verifying credential is missing. Surfacing this at startup prevents
    // deployers from discovering the misconfiguration only via a first 503.
    if (this.config.requireSignature !== false) {
      if (this.config.telephonyProvider === 'twilio' && !this.config.twilioToken) {
        getLogger().warn(
          'Twilio webhook enforcement ACTIVE but twilioToken is empty — webhooks will 503. ' +
            'Set requireSignature=false for local dev.',
        );
      }
      if (this.config.telephonyProvider === 'telnyx' && !this.config.telnyxPublicKey) {
        getLogger().warn(
          'Telnyx webhook enforcement ACTIVE but telnyxPublicKey is empty — webhooks will 503. ' +
            'Set requireSignature=false for local dev.',
        );
      }
    }

    // (Earlier versions of this file emitted a "Pipeline mode without VAD"
    // warning here when neither `agent.engine` nor `agent.vad` was set.
    // The warning is now stale: since the auto-VAD work landed in
    // stream-handler.ts (`this.autoVad = await SileroVAD.forPhoneCall()`
    // when `onnxruntime-node` is installed), the SDK silently provides a
    // working VAD per call. The stream handler still logs a single,
    // accurate message in the rare case the auto-load fails — emitting
    // both warnings created false-positive alarm fatigue for operators.)

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
        req.on('error', (err) => {
          next(err);
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
      mountDashboard(app, this.metricsStore, this.dashboardToken);
      mountApi(app, this.metricsStore, this.dashboardToken);
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
      } else if (this.config.requireSignature !== false) {
        getLogger().error('Twilio webhook rejected: twilioToken not configured and requireSignature is not false');
        res.status(503).send('Webhook signature required');
        return;
      }
      const body = req.body as Record<string, string>;
      // Raw carrier values — the completion registry is keyed by the raw
      // Twilio Call SID assigned at dial time, and the status string drives
      // the carrier-agnostic outcome mapping. ``callSid`` / ``callStatus``
      // below are sanitized for logging + the metrics store only.
      const rawCallSid = body['CallSid'] ?? '';
      const rawCallStatus = body['CallStatus'] ?? '';
      const callSid = sanitizeLogValue(rawCallSid);
      const callStatus = sanitizeLogValue(rawCallStatus);
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
      // FIX #91 — when the call terminates before the media stream
      // starts (no-answer / busy / failed / canceled), the prewarm
      // cache entry would otherwise leak until ``endCall`` runs. Evict
      // it here so the WARN fires once and the bytes are released
      // regardless of whether the user calls ``endCall``.
      if (
        callSid &&
        (callStatus === 'no-answer' ||
          callStatus === 'busy' ||
          callStatus === 'failed' ||
          callStatus === 'canceled')
      ) {
        try {
          this.recordPrewarmWaste(callSid);
        } catch (err) {
          getLogger().debug(`recordPrewarmWaste threw: ${String(err)}`);
        }
        // Resolve any pending call({ wait: true }) for a call that never
        // reached media — no onCallEnd will fire for these. Keyed by the raw
        // Call SID so it matches the id registered at dial time.
        this.resolveCompletion(rawCallSid, {
          outcome: twilioStatusToOutcome(rawCallStatus),
          status: rawCallStatus,
        });
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
      } else if (this.config.requireSignature !== false) {
        getLogger().error('Twilio webhook rejected: twilioToken not configured and requireSignature is not false');
        res.status(503).send('Webhook signature required');
        return;
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
      } else if (this.config.requireSignature !== false) {
        getLogger().error('Twilio webhook rejected: twilioToken not configured and requireSignature is not false');
        res.status(503).send('Webhook signature required');
        return;
      }
      const body = req.body as Record<string, string>;
      const answeredBy = body['AnsweredBy'] ?? '';
      const callSid = body['CallSid'] ?? '';
      getLogger().info(`AMD result for ${sanitizeLogValue(callSid)}: ${sanitizeLogValue(answeredBy)}`);

      // Record the AMD classification so a later onCallEnd can resolve
      // call({ wait: true }) as ``voicemail`` vs ``answered``.
      if (callSid) {
        this.amdClass.set(callSid, classifyTwilioAmd(answeredBy));
      }

      // Fire the per-call onMachineDetection callback (if set by Patter.call())
      // BEFORE the voicemail-drop logic so callers see the result regardless
      // of whether a voicemail message was configured. Errors in user code
      // must not break webhook delivery — Twilio retries on non-2xx.
      // Looked up by callSid so concurrent outbound calls each get their
      // own callback (Map replaces the old single-slot field).
      const cb = callSid ? this.onMachineDetectionByCallSid.get(callSid) : undefined;
      if (cb && callSid) {
        this.onMachineDetectionByCallSid.delete(callSid);
        try {
          await cb({
            call_id: callSid,
            carrier: 'twilio',
            classification: classifyTwilioAmd(answeredBy),
            raw: answeredBy,
            detected_at: Date.now() / 1000,
          });
        } catch (err) {
          getLogger().warn(`onMachineDetection callback threw: ${sanitizeLogValue(String(err))}`);
        }
      }

      // FIX #91 — when AMD classifies as machine, the agent's first
      // message will not be played (we drop voicemail or hang up), so
      // the prewarmed greeting is never consumed. Evict the cache entry
      // once so the WARN fires regardless of whether ``voicemailMessage``
      // is configured.
      if (
        (answeredBy === 'machine_end_beep' || answeredBy === 'machine_end_silence') &&
        callSid
      ) {
        try {
          this.recordPrewarmWaste(callSid);
        } catch (err) {
          getLogger().debug(`recordPrewarmWaste threw: ${String(err)}`);
        }
      }

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
          // Voicemail-drop is best-effort — degrade gracefully on slow/unreachable
          // Twilio API rather than blocking call-flow indefinitely (mirrors
          // Python server.py voicemail-drop httpx timeout=10.0).
          const vmResp = await fetch(vmUrl, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/x-www-form-urlencoded',
              'Authorization': `Basic ${Buffer.from(`${this.config.twilioSid}:${this.config.twilioToken}`).toString('base64')}`,
            },
            body: new URLSearchParams({ Twiml: twiml }).toString(),
            signal: AbortSignal.timeout(10_000),
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
      } else if (this.config.requireSignature !== false) {
        getLogger().error('Twilio webhook rejected: twilioToken not configured and requireSignature is not false');
        res.status(503).send('Webhook signature required');
        return;
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
      } else if (this.config.requireSignature !== false) {
        getLogger().error('Telnyx webhook rejected: telnyxPublicKey not configured and requireSignature is not false');
        return res.status(503).send('Webhook signature required');
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
            result?: string;
            hangup_cause?: string;
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

      // AMD result — mirrors Twilio's ``AnsweredBy == machine_end_*``
      // voicemail-drop flow. When Telnyx classifies the call as answered
      // by machine we speak the configured ``voicemailMessage`` via
      // ``actions/speak`` and then hang up via ``actions/hangup``.
      // Matches ``libraries/python/getpatter/handlers/telnyx_handler.py::handle_amd_result``.
      if (eventType === 'call.machine.detection.ended') {
        const amdCallId = payload.call_control_id ?? '';
        const amdResult = String(payload.result ?? '');
        getLogger().info(
          `Telnyx AMD result for ${sanitizeLogValue(amdCallId)}: ${sanitizeLogValue(amdResult)}`,
        );
        // Record the AMD classification so a later onCallEnd can resolve
        // call({ wait: true }) as ``voicemail`` vs ``answered``.
        if (amdCallId) {
          this.amdClass.set(amdCallId, classifyTelnyxAmd(amdResult));
        }
        // Fire the per-call onMachineDetection callback. Same rationale as
        // the Twilio path above — caller sees the result even when no
        // voicemailMessage is configured, and errors in user code don't
        // break webhook delivery.
        // Looked up by amdCallId (call_control_id) so concurrent outbound
        // calls each get their own callback.
        const cbTx = amdCallId ? this.onMachineDetectionByCallSid.get(amdCallId) : undefined;
        if (cbTx && amdCallId) {
          this.onMachineDetectionByCallSid.delete(amdCallId);
          try {
            await cbTx({
              call_id: amdCallId,
              carrier: 'telnyx',
              classification: classifyTelnyxAmd(amdResult),
              raw: amdResult,
              detected_at: Date.now() / 1000,
            });
          } catch (err) {
            getLogger().warn(`onMachineDetection callback threw: ${sanitizeLogValue(String(err))}`);
          }
        }
        if (amdCallId && (amdResult === 'machine' || amdResult === 'machine_detected')) {
          await this.handleTelnyxAmdVoicemail(amdCallId);
          // FIX #91 — when AMD classifies as machine the agent's first
          // message is replaced by ``voicemailMessage`` (or the call
          // simply ends), so the prewarmed greeting is never consumed.
          // Evict it so the WARN fires once.
          try {
            this.recordPrewarmWaste(amdCallId);
          } catch (err) {
            getLogger().debug(`recordPrewarmWaste threw: ${String(err)}`);
          }
        }
        return res.status(200).send();
      }

      // FIX #91 — Telnyx fires ``call.hangup`` as the final status
      // notification. ``hangup_cause`` distinguishes carrier outcomes
      // (``call_rejected`` / ``busy`` / ``no_answer`` / ``timeout`` /
      // ``normal_clearing`` / …). When the call never reached the
      // media stream the prewarm cache leaks unless we evict it here.
      if (eventType === 'call.hangup') {
        const hangupCallId = payload.call_control_id ?? '';
        const hangupCause = String(payload.hangup_cause ?? '');
        getLogger().info(
          `Telnyx call.hangup for ${sanitizeLogValue(hangupCallId)} ` +
            `(cause=${sanitizeLogValue(hangupCause)})`,
        );
        if (hangupCallId) {
          try {
            this.recordPrewarmWaste(hangupCallId);
          } catch (err) {
            getLogger().debug(`recordPrewarmWaste threw: ${String(err)}`);
          }
          // Resolve a pending call({ wait: true }) only for no-media hangup
          // causes (no-answer / busy / rejected). ``normal_clearing`` implies
          // the call connected → ``null`` here so onCallEnd resolves it with
          // the full transcript instead.
          const noMediaOutcome = telnyxHangupOutcome(hangupCause);
          if (noMediaOutcome !== null) {
            this.resolveCompletion(hangupCallId, {
              outcome: noMediaOutcome,
              status: hangupCause,
            });
          }
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
          // PERF — Telnyx accepts the streaming params inline on
          // ``actions/answer`` and auto-starts the stream the moment the
          // leg picks up. Folding ``streaming_start`` into the answer body
          // removes the ``call.answered`` webhook round-trip and a second
          // POST (~100-200 ms saved per inbound call).
          const caller = payload.from ?? '';
          const callee = payload.to ?? '';
          const streamUrl =
            `wss://${this.config.webhookUrl}/ws/stream/${encodeURIComponent(callControlId)}` +
            `?caller=${encodeURIComponent(caller)}&callee=${encodeURIComponent(callee)}`;
          getLogger().info(`Telnyx call.initiated ${callControlId} — answering with inline stream`);
          const resp = await fetch(`${apiBase}/calls/${encodeURIComponent(callControlId)}/actions/answer`, {
            method: 'POST',
            headers: authHeaders,
            body: JSON.stringify({
              stream_url: streamUrl,
              // ``inbound_track`` halves WS upstream bandwidth — outbound
              // echo was always filtered downstream anyway.
              stream_track: 'inbound_track',
              stream_bidirectional_mode: 'rtp',
              stream_bidirectional_codec: 'PCMU',
              stream_bidirectional_sampling_rate: 8000,
              stream_bidirectional_target_legs: 'self',
            }),
            signal: AbortSignal.timeout(10_000),
          });
          if (!resp.ok) {
            getLogger().warn(`Telnyx answer failed: ${resp.status} ${(await resp.text()).slice(0, 200)}`);
          }
        } else if (eventType === 'call.answered') {
          // No-op: ``call.initiated`` already submitted answer + streaming
          // in a single call. Telnyx still emits ``call.answered`` as an
          // informational event; acknowledge it without a redundant POST.
          getLogger().debug(`Telnyx call.answered ${callControlId} — stream already active (inline)`);
        } else {
          getLogger().debug(`Telnyx event ignored: ${eventType}`);
        }
      } catch (e) {
        getLogger().error(`Telnyx webhook handler error: ${String(e)}`);
      }

      // Telnyx ignores the response body. Acknowledge with 200 OK.
      return res.status(200).send();
    });

    // --- Plivo ---

    // Verify the X-Plivo-Signature-V3 header. V3 signs ``url + sorted_post_params
    // + "." + nonce`` for POST and ``url + "." + nonce`` for GET — so the form
    // body (already parsed by express.urlencoded) has to feed into the
    // signature calculation. Returns false (and writes the error response) to
    // short-circuit the route.
    const validatePlivoRequest = (req: express.Request, res: express.Response): boolean => {
      const authToken = this.config.plivoAuthToken;
      if (!authToken) {
        if (this.config.requireSignature !== false) {
          getLogger().error(
            'Plivo webhook rejected: plivoAuthToken not configured and requireSignature is not false',
          );
          res.status(503).send('Webhook signature required');
          return false;
        }
        return true;
      }
      const method = req.method.toUpperCase() as 'GET' | 'POST';
      const params: Record<string, string> =
        method === 'POST' && req.body && typeof req.body === 'object'
          ? Object.fromEntries(
              Object.entries(req.body as Record<string, unknown>).map(([k, v]) => [k, String(v)]),
            )
          : {};
      const signature = (req.headers['x-plivo-signature-v3'] as string) || '';
      const nonce = (req.headers['x-plivo-signature-v3-nonce'] as string) || '';
      const url = `https://${this.config.webhookUrl}${req.originalUrl}`;
      if (!validatePlivoSignature(url, nonce, signature, authToken, params, method)) {
        getLogger().warn('Plivo webhook rejected: invalid or missing V3 signature');
        res.status(403).send('Invalid signature');
        return false;
      }
      return true;
    };

    app.post('/webhooks/plivo/voice', (req, res) => {
      if (!validatePlivoRequest(req, res)) return;
      const body = (req.body ?? {}) as Record<string, string>;
      // Plivo posts CallUUID + From/To on the answer_url for inbound AND
      // answered-outbound calls — the same route serves both.
      const callUuid = body['CallUUID'] ?? '';
      const caller = body['From'] ?? '';
      const callee = body['To'] ?? '';
      const qs = `?caller=${encodeURIComponent(caller)}&callee=${encodeURIComponent(callee)}`;
      const streamUrl = `wss://${this.config.webhookUrl}/ws/plivo/stream/${callUuid || 'outbound'}${qs}`;
      const xml = PlivoAdapter.generateStreamXml(streamUrl, 'audio/x-mulaw;rate=8000', {
        'X-PH-caller': caller,
        'X-PH-callee': callee,
      });
      res.type('text/xml').send(xml);
    });

    app.post('/webhooks/plivo/status', (req, res) => {
      if (!validatePlivoRequest(req, res)) return;
      const body = (req.body ?? {}) as Record<string, string>;
      const callUuid = body['CallUUID'] ?? '';
      const callStatus = body['CallStatus'] ?? body['Status'] ?? '';
      const duration = body['Duration'] ?? body['BillDuration'] ?? '';
      getLogger().info(
        `Plivo status ${sanitizeLogValue(callStatus)} for call ${sanitizeLogValue(callUuid)} (duration=${duration})`,
      );
      if (callUuid && callStatus) {
        const extra: Record<string, unknown> = {};
        const parsed = parseFloat(duration);
        if (!Number.isNaN(parsed)) extra.duration_seconds = parsed;
        this.metricsStore.updateCallStatus(callUuid, callStatus, extra);
      }
      if (
        callUuid &&
        ['no-answer', 'busy', 'failed', 'timeout', 'cancel'].includes(callStatus)
      ) {
        try {
          this.recordPrewarmWaste(callUuid);
        } catch (err) {
          getLogger().debug(`recordPrewarmWaste threw: ${String(err)}`);
        }
        // Resolve a pending call({ wait: true }) for a call that never reached
        // media — no onCallEnd will fire for these.
        const outcome: CallOutcome =
          callStatus === 'no-answer' || callStatus === 'timeout'
            ? 'no_answer'
            : callStatus === 'busy'
              ? 'busy'
              : 'failed';
        this.resolveCompletion(callUuid, { outcome, status: callStatus });
      }
      res.status(200).send();
    });

    app.post('/webhooks/plivo/amd', async (req, res) => {
      if (!validatePlivoRequest(req, res)) return;
      const body = (req.body ?? {}) as Record<string, string>;
      const callUuid = body['CallUUID'] ?? '';
      // Plivo's async AMD result field name varies by API version — accept the
      // common spellings; classifyPlivoAmd normalises them.
      const amdRaw =
        body['Machine'] || body['MachineDetection'] || body['AnsweredBy'] || body['CallStatus'] || '';
      getLogger().info(`AMD result for ${sanitizeLogValue(callUuid)}: ${sanitizeLogValue(amdRaw)}`);
      const classification = classifyPlivoAmd(amdRaw);
      // Record the AMD classification so a later onCallEnd can resolve a
      // pending call({ wait: true }) as ``voicemail`` vs ``answered``.
      if (callUuid) this.amdClass.set(callUuid, classification);

      // Fire the per-call onMachineDetection callback. Plivo registers under
      // its dial-time ``request_uuid``, but this webhook only carries the live
      // ``CallUUID`` — the two identifiers differ. Try a keyed lookup first
      // (works if a future Plivo change ever aligns them), then fall back to
      // the single pending callback when exactly one is registered. The
      // fallback preserves the single-slot semantics Python uses for Plivo
      // while still benefiting from the per-callSid Map for Twilio / Telnyx.
      let cbKey = callUuid && this.onMachineDetectionByCallSid.has(callUuid) ? callUuid : undefined;
      if (cbKey === undefined && this.onMachineDetectionByCallSid.size === 1) {
        cbKey = this.onMachineDetectionByCallSid.keys().next().value;
      }
      const cb = cbKey !== undefined ? this.onMachineDetectionByCallSid.get(cbKey) : undefined;
      if (cb && callUuid) {
        if (cbKey !== undefined) this.onMachineDetectionByCallSid.delete(cbKey);
        try {
          await cb({
            call_id: callUuid,
            carrier: 'plivo',
            classification,
            raw: amdRaw,
            detected_at: Date.now() / 1000,
          });
        } catch (err) {
          getLogger().warn(`onMachineDetection callback threw: ${sanitizeLogValue(String(err))}`);
        }
      }

      if (classification === 'machine' && callUuid) {
        try {
          this.recordPrewarmWaste(callUuid);
        } catch (err) {
          getLogger().debug(`recordPrewarmWaste threw: ${String(err)}`);
        }
        if (this.voicemailMessage && this.config.plivoAuthId && this.config.plivoAuthToken) {
          await dropPlivoVoicemail(
            callUuid,
            this.voicemailMessage,
            this.config.plivoAuthId,
            this.config.plivoAuthToken,
          );
        }
      }
      res.status(200).send();
    });

    // Blind-transfer target XML: the ``aleg_url`` PlivoBridge.transferCall
    // redirects the A-leg to. Served for GET and POST (Plivo may use either).
    app.all('/webhooks/plivo/transfer', (req, res) => {
      if (!validatePlivoRequest(req, res)) return;
      const to = String((req.query.to as string) ?? '');
      if (!to || !/^\+[1-9]\d{6,14}$/.test(to)) {
        getLogger().warn(`Plivo transfer XML: invalid target ${JSON.stringify(to)}`);
        res.type('text/xml').send('<Response><Hangup/></Response>');
        return;
      }
      res.type('text/xml').send(`<Response><Dial><Number>${xmlEscape(to)}</Number></Dial></Response>`);
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

      // Track active connections for graceful shutdown
      this.activeConnections.add(ws);
      ws.once('close', () => {
        this.activeConnections.delete(ws);
      });

      const provider = this.config.telephonyProvider;
      if (provider === 'telnyx') {
        this.handleTelnyxStream(ws, url);
      } else if (provider === 'plivo') {
        this.handlePlivoStream(ws, url);
      } else {
        this.handleTwilioStream(ws, url);
      }
    });

    await new Promise<void>((resolve, reject) => {
      // Default bind = 127.0.0.1 (loopback, safest). Set
      // ``PATTER_BIND_HOST=0.0.0.0`` when the SDK runs inside a container
      // whose port must be reachable from the host (e.g. ``docker run -p
      // 8000:8000`` with a tunnel pointing at the host port — Docker's
      // port-mapping cannot forward to a 127.0.0.1 listener inside the
      // container because that's the container's own loopback).
      const bindHost = process.env.PATTER_BIND_HOST ?? '127.0.0.1';
      this.server!.once('error', reject);
      this.server!.listen(port, bindHost, () => {
        this.server!.off('error', reject);
        getLogger().info(`Server on port ${port}`);
        getLogger().info(`Webhook: https://${this.config.webhookUrl}`);
        getLogger().info(`Phone:   ${this.config.phoneNumber}`);
        // Warn if the agent runs a non-default Realtime model — DEFAULT_PRICING
        // is calibrated for the default Realtime models (gpt-realtime-mini /
        // gpt-4o-mini-realtime-preview, which share the same rates). Other
        // models differ by 3-10x so cost display would under-report.
        const model = this.agent.model ?? '';
        const calibrated = ['gpt-realtime-mini', 'gpt-4o-mini-realtime-preview'];
        if (model && !calibrated.includes(model) && model.includes('realtime')) {
          // Dev-supplied string — sanitize to avoid ANSI/log-injection in
          // aggregators.
          getLogger().warn(
            `Agent uses "${sanitizeLogValue(model)}" but DEFAULT_PRICING.openai_realtime is ` +
            'calibrated for the default Realtime models (gpt-realtime-mini / ' +
            'gpt-4o-mini-realtime-preview). Pass ' +
            'Patter({ pricing: { openai_realtime: {...} } }) to set rates for ' +
            'this model, otherwise the dashboard cost display will under-report.'
          );
        }
        if (this.dashboard) {
          console.log('\n──── Dashboard ─────────────────────────────────────');
          getLogger().info(`URL: http://127.0.0.1:${port}/`);
          if (!this.dashboardToken) {
            getLogger().warn(
              'Dashboard is enabled without authentication. ' +
              'Set dashboardToken to protect call data. ' +
              'This is safe for local development but should not be exposed on a public network.'
            );
          }
          console.log('────────────────────────────────────────────────────\n');
        }
        resolve();
      });
    });
  }

  /**
   * Handle a Telnyx ``call.machine.detection.ended`` event when AMD returns
   * ``machine``: speak the configured voicemail message via ``actions/speak``
   * then hang up via ``actions/hangup``. Mirrors the Python
   * ``handle_amd_result`` helper.
   */
  private async handleTelnyxAmdVoicemail(callControlId: string): Promise<void> {
    const telnyxKey = this.config.telnyxKey ?? '';
    if (!callControlId || !telnyxKey || !this.voicemailMessage) {
      return;
    }
    const encoded = encodeURIComponent(callControlId);
    const headers = {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${telnyxKey}`,
    } as const;
    // Heuristic playback-duration estimate — ~150 ms per character
    // (≈14 chars/sec English speech) plus a 1500 ms buffer, capped at
    // 30 s. Avoids cutting the voicemail mid-sentence on hangup. The
    // proper fix is to subscribe to Telnyx ``call.speak.ended`` and hang
    // up there; kept as a heuristic since the webhook plumbing change
    // is broader than this handler. Mirrors
    // ``libraries/python/getpatter/handlers/telnyx_handler.py::handle_amd_result``.
    const estimatedMs = Math.min(
      30_000,
      Math.ceil((this.voicemailMessage.length / 14) * 1000) + 1500,
    );
    try {
      const speakResp = await fetch(
        `https://api.telnyx.com/v2/calls/${encoded}/actions/speak`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            payload: this.voicemailMessage,
            voice: 'female',
            language: 'en-US',
          }),
          signal: AbortSignal.timeout(10_000),
        },
      );
      if (!speakResp.ok) {
        getLogger().warn(
          `Telnyx voicemail speak failed: ${speakResp.status} ${(await speakResp.text()).slice(0, 200)}`,
        );
      }
      await new Promise((resolve) => setTimeout(resolve, estimatedMs));
      await fetch(`https://api.telnyx.com/v2/calls/${encoded}/actions/hangup`, {
        method: 'POST',
        headers,
        body: JSON.stringify({}),
        signal: AbortSignal.timeout(10_000),
      });
      getLogger().info(`Voicemail dropped for Telnyx call ${sanitizeLogValue(callControlId)}`);
    } catch (e) {
      getLogger().warn(`Could not drop voicemail (Telnyx): ${String(e)}`);
    }
  }

  // ---------------------------------------------------------------------------
  // Stream handler helpers
  // ---------------------------------------------------------------------------

  /** Build the shared StreamHandlerDeps for the current server configuration. */
  private buildStreamHandlerDeps(bridge: TelephonyBridge): import('./stream-handler').StreamHandlerDeps {
    const [wrappedStart, wrappedMetrics, wrappedEnd] = this.wrapLoggingCallbacks(bridge);
    return {
      config: this.config,
      agent: this.agent,
      bridge,
      metricsStore: this.metricsStore,
      pricing: this.pricing,
      remoteHandler: this.remoteHandler,
      onCallStart: wrappedStart,
      onCallEnd: wrappedEnd,
      onTranscript: this.onTranscript,
      onMessage: this.onMessage,
      onMetrics: wrappedMetrics,
      recording: this.recording,
      buildAIAdapter: (resolvedPrompt: string, toolsOverride?: readonly ToolDefinition[]) =>
        buildAIAdapter(this.config, this.agent, resolvedPrompt, toolsOverride),
      sanitizeVariables,
      resolveVariables,
      popPrewarmAudio: this.popPrewarmAudio,
      popPrewarmedConnections: this.popPrewarmedConnections,
    };
  }

  /**
   * Wrap user-supplied call lifecycle callbacks with CallLogger side-effects.
   * When PATTER_LOG_DIR is unset, the logger is disabled and the returned
   * wrappers degrade to just calling the user callbacks (still wrapped so
   * the logger stays consistent with future configuration changes).
   */
  private wrapLoggingCallbacks(
    bridge: TelephonyBridge,
  ): [
    typeof this.onCallStart,
    typeof this.onMetrics,
    typeof this.onCallEnd,
  ] {
    const logger = this.callLogger;
    const agent = this.agent;
    const userStart = this.onCallStart;
    const userMetrics = this.onMetrics;
    const userEnd = this.onCallEnd;

    const agentSnapshot = (): Record<string, unknown> => {
      const snap: Record<string, unknown> = {
        provider: agent.provider,
        model: (agent as { model?: string }).model,
        voice: (agent as { voice?: string }).voice,
        language: (agent as { language?: string }).language,
      };
      if (agent.stt && agent.tts && !('engine' in agent && (agent as { engine?: unknown }).engine)) {
        snap.mode = 'pipeline';
      }
      return Object.fromEntries(Object.entries(snap).filter(([, v]) => v !== undefined));
    };

    const store = this.metricsStore;
    const wrappedStart = async (data: Record<string, unknown>): Promise<void> => {
      if (logger.enabled) {
        const callId = typeof data.call_id === 'string' ? data.call_id : '';
        // For outbound calls the bridge has no caller/callee in the WS query
        // string (TwiML for outbound is inline ``<Stream url="…/outbound"/>``
        // with no <Parameter> tags), so ``data.caller`` / ``data.callee`` are
        // empty here. The active record in the store was populated by
        // ``recordCallInitiated`` at dial time and holds the correct numbers
        // — pull them from there before persisting metadata.json. Without
        // this fallback every outbound call's metadata.json on disk has
        // ``caller=""`` / ``callee=""``.
        const dataCaller = typeof data.caller === 'string' ? data.caller : '';
        const dataCallee = typeof data.callee === 'string' ? data.callee : '';
        const active = callId ? store.getActive(callId) : undefined;
        const resolvedCaller = dataCaller || active?.caller || '';
        const resolvedCallee = dataCallee || active?.callee || '';
        // Fire-and-forget: call logging must never block the voice flow.
        const resolvedDirection =
          (typeof data.direction === 'string' ? data.direction : '') ||
          active?.direction ||
          'inbound';
        void logger
          .logCallStart(callId, {
            caller: resolvedCaller,
            callee: resolvedCallee,
            direction: resolvedDirection,
            telephonyProvider: bridge.telephonyProvider,
            providerMode: agent.provider ?? '',
            agent: agentSnapshot(),
          })
          .catch((err) => getLogger().error(`call_log start error: ${String(err)}`));
      }
      if (userStart) await userStart(data);
    };

    const wrappedMetrics = async (data: Record<string, unknown>): Promise<void> => {
      if (logger.enabled) {
        const callId = typeof data.call_id === 'string' ? data.call_id : '';
        const turn = data.turn;
        if (turn && typeof turn === 'object') {
          // Fire-and-forget: call logging must never block the voice flow.
          void logger
            .logTurn(callId, turn as Record<string, unknown>)
            .catch((err) => getLogger().error(`call_log turn error: ${String(err)}`));
        }
      }
      if (userMetrics) await userMetrics(data);
    };

    const wrappedEnd = async (data: Record<string, unknown>): Promise<void> => {
      if (logger.enabled) {
        const callId = typeof data.call_id === 'string' ? data.call_id : '';
        const metricsObj = (data.metrics ?? null) as
          | (Record<string, unknown> & {
              duration_seconds?: number;
              turns?: unknown[];
              cost?: Record<string, unknown>;
              latency_avg?: Record<string, number>;
              latency_p50?: Record<string, number>;
              latency_p95?: Record<string, number>;
              latency_p99?: Record<string, number>;
            })
          | null;
        // Persist full LatencyBreakdown per percentile so the dashboard
        // hydrate path can render stt/llm/tts breakdown for historical
        // calls. Keep flat ``p50_ms/p95_ms/p99_ms`` for backward compat.
        const latency = metricsObj
          ? {
              p50_ms: metricsObj.latency_p50?.total_ms ?? null,
              p95_ms: metricsObj.latency_p95?.total_ms ?? null,
              p99_ms: metricsObj.latency_p99?.total_ms ?? null,
              avg: metricsObj.latency_avg ?? null,
              p50: metricsObj.latency_p50 ?? null,
              p95: metricsObj.latency_p95 ?? null,
              p99: metricsObj.latency_p99 ?? null,
            }
          : null;
        // Fire-and-forget: call logging must never block the voice flow.
        void logger
          .logCallEnd(callId, {
            durationSeconds: metricsObj?.duration_seconds,
            turns: metricsObj?.turns?.length,
            cost: metricsObj?.cost ?? null,
            latency,
          })
          .catch((err) => getLogger().error(`call_log end error: ${String(err)}`));
      }
      if (userEnd) await userEnd(data);
      // Resolve any pending call({ wait: true }) for this call. A media-stream
      // end means the call connected: classify ``voicemail`` when AMD tagged
      // the callee as a machine, else ``answered``. Fan-out — this runs
      // regardless of (and after) the user's own onCallEnd callback, so
      // wiring a callback no longer monopolises completion signalling.
      // Mirrors the Python ``_on_call_end`` wrapper.
      const cid = typeof data.call_id === 'string' ? data.call_id : '';
      if (cid) {
        const cls = this.amdClass.get(cid);
        const outcome: CallOutcome = cls === 'machine' ? 'voicemail' : 'answered';
        this.resolveCompletion(cid, { outcome, status: 'completed', data });
      }
    };

    return [wrappedStart, wrappedMetrics, wrappedEnd];
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
          // Twilio confirms playback of a previously sent audio chunk.
          // Forward the mark name so barge-in heuristics can compare it
          // against the latest sent mark. Mirrors Python's
          // ``twilio_handler.on_mark`` propagation.
          const markName = String(data.mark?.name ?? '');
          if (markName) await handler.onMark(markName);
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
  // Plivo WebSocket message parser (thin layer)
  // ---------------------------------------------------------------------------

  private handlePlivoStream(ws: WSWebSocket, url: URL): void {
    const caller = url.searchParams.get('caller') ?? '';
    const callee = url.searchParams.get('callee') ?? '';
    const bridge = new PlivoBridge(this.config);
    const handler = new StreamHandler(this.buildStreamHandlerDeps(bridge), ws, caller, callee);

    ws.on('message', async (raw) => {
      try {
        // Plivo media-stream frames: ``start`` (callId/streamId/mediaFormat),
        // ``media``, ``playedStream`` (checkpoint ack ≈ Twilio mark), ``dtmf``,
        // ``clearedAudio`` / ``playFailed`` / ``error``, ``stop``. Mirror of
        // the Python ``plivo_stream_bridge``.
        let data: {
          event?: string;
          start?: { callId?: string; streamId?: string; mediaFormat?: { encoding?: string; sampleRate?: number } };
          media?: { payload?: string };
          dtmf?: { digit?: string };
          name?: string;
          reason?: string;
        };
        try {
          data = JSON.parse(raw.toString()) as typeof data;
        } catch (e) {
          getLogger().error('Failed to parse Plivo WS message:', e);
          return;
        }
        const event = data.event ?? '';

        if (event === 'start') {
          // Plivo's CallUUID arrives here as ``callId`` and is the id used for
          // hangup / transfer / recording / cost REST calls.
          handler.setStreamSid(data.start?.streamId ?? '');
          const callId = data.start?.callId ?? '';
          if (callId) this.activeCallIds.set(ws, callId);
          await handler.handleCallStart(callId);
        } else if (event === 'media') {
          const payload = data.media?.payload ?? '';
          if (payload) handler.handleAudio(Buffer.from(payload, 'base64'));
        } else if (event === 'playedStream') {
          // Checkpoint acknowledgement — the analogue of a Twilio mark.
          const markName = String(data.name ?? '');
          if (markName) await handler.onMark(markName);
        } else if (event === 'dtmf') {
          const digit = String(data.dtmf?.digit ?? '').trim();
          if (digit) await handler.handleDtmf(digit);
        } else if (event === 'playFailed' || event === 'error') {
          getLogger().warn(`Plivo ${event}: ${data.reason ?? 'unknown'}`);
        } else if (event === 'stop') {
          await handler.handleStop();
        }
      } catch (err) {
        getLogger().error('Stream handler error (Plivo):', err);
      }
    });

    ws.on('close', async () => {
      this.activeCallIds.delete(ws);
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
    const provider = this.config.telephonyProvider;
    for (const [ws, callId] of this.activeCallIds) {
      try {
        const bridge =
          provider === 'telnyx'
            ? new TelnyxBridge(this.config)
            : provider === 'plivo'
              ? new PlivoBridge(this.config)
              : new TwilioBridge(this.config);
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
      let checkInterval: ReturnType<typeof setInterval> | undefined;
      const drainPromise = new Promise<void>((resolve) => {
        checkInterval = setInterval(() => {
          if (this.activeConnections.size === 0) {
            clearInterval(checkInterval!);
            resolve();
          }
        }, 100);
      });
      const timeoutPromise = new Promise<void>((resolve) => setTimeout(resolve, GRACEFUL_SHUTDOWN_TIMEOUT_MS));
      await Promise.race([drainPromise, timeoutPromise]);
      clearInterval(checkInterval!);
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

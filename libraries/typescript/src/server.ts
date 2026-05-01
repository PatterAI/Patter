/**
 * Embedded HTTP/WebSocket server — wires Express webhooks for the configured
 * carrier (Twilio or Telnyx) into the per-call `StreamHandler` and dashboard.
 */

import crypto from 'node:crypto';
import express from 'express';
import { createServer, Server as HTTPServer } from 'http';
import { WebSocketServer, WebSocket as WSWebSocket } from 'ws';
import { OpenAIRealtimeAdapter } from './providers/openai-realtime';
import { ElevenLabsConvAIAdapter } from './providers/elevenlabs-convai';
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
import type { AgentOptions, PipelineMessageHandler } from './types';
import { CallLogger, resolveLogRoot } from './services/call-log';

/** Resolved configuration consumed by `EmbeddedServer` (carrier credentials, webhook URL, etc.). */
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
  /**
   * SECURITY: require valid webhook signatures on both Twilio and Telnyx
   * inbound webhooks. When True (the default), a missing credential
   * (twilioToken / telnyxPublicKey) causes the webhook to return
   * 503 Service Unavailable instead of silently accepting the request.
   * Set to false only for local development against mock providers.
   */
  requireSignature?: boolean;
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
 */
export function validateWebhookUrl(url: string): void {
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error(`Invalid webhook URL scheme: ${parsed.protocol}`);
  }
  // Node's URL parser preserves IPv6 brackets on ``hostname`` — strip them so
  // raw IPv6 literal checks can match. Lowercase for case-insensitive
  // hostname/IP comparisons (hex digits are case-insensitive in IPv6).
  const rawHost = parsed.hostname;
  const host = rawHost.replace(/^\[/, '').replace(/\]$/, '').toLowerCase();

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
export function buildAIAdapter(config: LocalConfig, agent: AgentOptions, resolvedPrompt?: string): AIAdapter {
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
  // Always inject transfer_call and end_call system tools alongside agent-defined tools
  const agentTools = agent.tools?.map((t) => ({
    name: t.name,
    description: t.description,
    parameters: t.parameters,
  })) ?? [];
  const tools = [...agentTools, TRANSFER_CALL_TOOL, END_CALL_TOOL];
  const openaiKey = engine && engine.kind === 'openai_realtime' ? engine.apiKey : (config.openaiKey ?? '');
  return new OpenAIRealtimeAdapter(
    openaiKey,
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
 * set in ``sdk-py/getpatter/handlers/telnyx_handler.py``.
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
  /** Opt-in per-call filesystem logger (set via PATTER_LOG_DIR). */
  private readonly callLogger: CallLogger = new CallLogger(resolveLogRoot());

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

    // Hydrate the dashboard from disk so /api/dashboard/calls survives a
    // restart. CallLogger persists call metadata as JSONL/JSON under
    // PATTER_LOG_DIR; if it's set, replay those files into the store.
    // No-op when logging is disabled (PATTER_LOG_DIR unset).
    const logRoot = resolveLogRoot();
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

    // Pipeline-mode barge-in fragility warning: without an external VAD
    // adapter (Silero, Krisp, custom), the SDK falls back to the legacy
    // "always forward + bargeInThresholdMs" path which is fragile on
    // tunnel + speakerphone setups — the agent's own TTS leaks back into
    // the inbound mic feed, STT transcribes it, and the heuristic gets
    // confused so the cancel never fires. Only relevant in pipeline mode
    // (no engine) with barge-in enabled.
    if (
      !this.agent.engine &&
      !this.agent.vad &&
      (this.agent.bargeInThresholdMs ?? 300) > 0
    ) {
      const ms = this.agent.bargeInThresholdMs ?? 300;
      getLogger().warn(
        `Pipeline mode without VAD: barge-in falls back to a fragile heuristic ` +
          `(bargeInThresholdMs=${ms}) that misfires on tunnel/speakerphone ` +
          `setups when the agent's own TTS leaks into the mic feed. ` +
          `Recommended: pass \`vad: await SileroVAD.load({ sampleRate: 16000 })\` ` +
          `on AgentOptions, or set bargeInThresholdMs: 0 to disable barge-in entirely.`,
      );
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
      // Matches ``sdk-py/getpatter/handlers/telnyx_handler.py::handle_amd_result``.
      if (eventType === 'call.machine.detection.ended') {
        const amdCallId = payload.call_control_id ?? '';
        const amdResult = String(payload.result ?? '');
        getLogger().info(
          `Telnyx AMD result for ${sanitizeLogValue(amdCallId)}: ${sanitizeLogValue(amdResult)}`,
        );
        if (amdCallId && (amdResult === 'machine' || amdResult === 'machine_detected')) {
          await this.handleTelnyxAmdVoicemail(amdCallId);
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
        getLogger().info(`Server on port ${port}`);
        getLogger().info(`Webhook: https://${this.config.webhookUrl}`);
        getLogger().info(`Phone:   ${this.config.phoneNumber}`);
        // Warn if the agent runs a non-default Realtime model — DEFAULT_PRICING
        // is calibrated for gpt-4o-mini-realtime-preview. Other models differ
        // by 3-10x so cost display would under-report without an override.
        const model = this.agent.model ?? '';
        if (model && model !== 'gpt-4o-mini-realtime-preview' && model.includes('realtime')) {
          // Dev-supplied string — sanitize to avoid ANSI/log-injection in
          // aggregators.
          getLogger().warn(
            `Agent uses "${sanitizeLogValue(model)}" but DEFAULT_PRICING.openai_realtime is ` +
            'calibrated for "gpt-4o-mini-realtime-preview". Pass ' +
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
    // ``sdk-py/getpatter/handlers/telnyx_handler.py::handle_amd_result``.
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
      buildAIAdapter: (resolvedPrompt: string) => buildAIAdapter(this.config, this.agent, resolvedPrompt),
      sanitizeVariables,
      resolveVariables,
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

    const wrappedStart = async (data: Record<string, unknown>): Promise<void> => {
      if (logger.enabled) {
        const callId = typeof data.call_id === 'string' ? data.call_id : '';
        // Fire-and-forget: call logging must never block the voice flow.
        void logger
          .logCallStart(callId, {
            caller: typeof data.caller === 'string' ? data.caller : '',
            callee: typeof data.callee === 'string' ? data.callee : '',
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
              latency_p50?: { total_ms?: number };
              latency_p95?: { total_ms?: number };
              latency_p99?: { total_ms?: number };
            })
          | null;
        const latency = metricsObj
          ? {
              p50_ms: metricsObj.latency_p50?.total_ms ?? null,
              p95_ms: metricsObj.latency_p95?.total_ms ?? null,
              p99_ms: metricsObj.latency_p99?.total_ms ?? null,
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

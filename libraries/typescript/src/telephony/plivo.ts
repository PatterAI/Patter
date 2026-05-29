/** Plivo carrier credentials holder + telephony bridge for Patter.
 *
 * Plivo's wire-protocol oddities live entirely in this file:
 *   - the answer XML puts the WSS URL in the ``<Stream>`` element's text
 *     content (not a ``url=`` attribute, like Twilio)
 *   - outbound audio is a ``playAudio`` command (vs Twilio's ``media``)
 *   - barge-in flush is ``clearAudio`` (vs ``clear``)
 *   - playback markers use ``checkpoint`` / ``playedStream`` (vs ``mark``)
 *   - DTMF send is ``sendDTMF`` over the WS (Twilio can't send DTMF at all)
 *   - webhooks sign with V3 (HMAC-SHA256 of url + nonce)
 *
 * The ``PlivoBridge`` and signature/AMD helpers below are imported by
 * ``server.ts`` and ``handlePlivoStream`` — putting them here keeps server.ts
 * from sprawling with carrier-specific code.
 */

import crypto from 'node:crypto';
import { WebSocket as WSWebSocket } from 'ws';

import type { TelephonyBridge } from '../stream-handler';
import type { LocalConfig } from '../server';
import type { AgentOptions, MachineDetectionResult } from '../types';
import type { STTAdapter } from '../provider-factory';
import { createSTT } from '../provider-factory';
import { CallMetricsAccumulator } from '../metrics';
import { getLogger } from '../logger';

/** Constructor options for the Plivo {@link Carrier}. */
export interface PlivoCarrierOptions {
  /** Plivo Auth ID. Falls back to PLIVO_AUTH_ID env var. */
  authId?: string;
  /** Plivo Auth Token. Falls back to PLIVO_AUTH_TOKEN env var. */
  authToken?: string;
}

/**
 * Plivo telephony carrier — holds Auth ID + Auth Token.
 *
 * Plivo authenticates REST calls (outbound dial, hangup, recording) with HTTP
 * Basic ``authId:authToken`` and verifies inbound webhooks with the V3
 * signature scheme (HMAC-SHA256 keyed on ``authToken``) — so unlike Telnyx
 * there is no separate asymmetric public key to carry.
 *
 * @example
 * ```ts
 * import * as plivo from "getpatter/telephony/plivo";
 * const carrier = new plivo.Carrier();                       // reads env
 * const carrier = new plivo.Carrier({ authId: "MA...", authToken: "..." });
 * ```
 */
export class Carrier {
  readonly kind = "plivo" as const;
  readonly authId: string;
  readonly authToken: string;

  constructor(opts: PlivoCarrierOptions = {}) {
    const authId = opts.authId ?? process.env.PLIVO_AUTH_ID;
    const authToken = opts.authToken ?? process.env.PLIVO_AUTH_TOKEN;
    if (!authId) {
      throw new Error(
        "Plivo carrier requires authId. Pass { authId: 'MA...' } or " +
          "set PLIVO_AUTH_ID in the environment.",
      );
    }
    if (!authToken) {
      throw new Error(
        "Plivo carrier requires authToken. Pass { authToken: '...' } or " +
          "set PLIVO_AUTH_TOKEN in the environment.",
      );
    }
    this.authId = authId;
    this.authToken = authToken;
  }
}

// ---------------------------------------------------------------------------
// V3 signature + AMD classifier
// ---------------------------------------------------------------------------

/**
 * Map a Plivo AMD result to the carrier-agnostic classification. Plivo's
 * async machine-detection callback reports the outcome via a result field
 * whose spelling varies by API version, so we match the common shapes
 * defensively. Mirrors Python ``_classify_plivo_amd``.
 */
export function classifyPlivoAmd(result: string): MachineDetectionResult['classification'] {
  const r = (result || '').trim().toLowerCase();
  if (r === 'human' || r === 'person') return 'human';
  if (r.startsWith('machine') || r === 'answering_machine' || r === 'amd' || r === 'true') {
    return 'machine';
  }
  if (r === 'fax') return 'fax';
  return 'unknown';
}

/**
 * Validate a Plivo V3 webhook signature.
 *
 * Mirrors the algorithm in plivo-python's ``signature_v3`` module:
 *
 *   - **POST**: ``signed = url + sortedPostParams + "." + nonce`` where POST
 *     params are sorted alphabetically by key (case-sensitive) and
 *     concatenated as ``key1value1key2value2…`` with no delimiters.
 *   - **GET**:  ``signed = url + "." + nonce`` — query params live in the URL
 *     already so no separate concatenation.
 *
 * HMAC-SHA256 keyed on ``authToken``, base64-encoded. The
 * ``X-Plivo-Signature-V3`` header may carry multiple comma-separated
 * signatures during key rotation; accept if any matches.
 */
export function validatePlivoSignature(
  url: string,
  nonce: string,
  signature: string,
  authToken: string,
  params?: Record<string, string>,
  method: 'GET' | 'POST' = 'POST',
): boolean {
  if (!signature || !nonce || !authToken) return false;
  let base = url;
  if (method === 'POST' && params && Object.keys(params).length > 0) {
    // Plivo SDK ``get_sorted_params_string``: sort keys, concat ``k+v``.
    const keys = Object.keys(params).sort();
    base += keys.map((k) => `${k}${params[k]}`).join('');
  }
  const signed = `${base}.${nonce}`;
  const expected = crypto
    .createHmac('sha256', authToken)
    .update(signed)
    .digest('base64');
  const expBuf = Buffer.from(expected);
  for (const rawSig of signature.split(',')) {
    const trimmed = rawSig.trim();
    if (!trimmed) continue;
    try {
      const sigBuf = Buffer.from(trimmed);
      if (sigBuf.length === expBuf.length && crypto.timingSafeEqual(sigBuf, expBuf)) {
        return true;
      }
    } catch {
      continue;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// PlivoBridge — the per-call carrier shim implementing TelephonyBridge
// ---------------------------------------------------------------------------

/** DTMF digits accepted by Plivo's ``sendDTMF`` command. Parity with the
 *  Telnyx allowlist and Python ``_DTMF_ALLOWED``. */
const PLIVO_DTMF_ALLOWED = new Set('0123456789*#ABCDabcdwW');

/**
 * Plivo-specific telephony bridge.
 *
 * Plivo streams mulaw 8 kHz like Twilio (we pin contentType in the answer
 * XML, so the StreamHandler treats it exactly like Twilio audio-wise), but
 * the outbound envelopes differ: audio is a ``playAudio`` command, barge-in
 * flush is ``clearAudio``, and the playback marker is ``checkpoint`` (acked
 * by a ``playedStream`` frame). Plivo also accepts native ``sendDTMF`` over
 * the WebSocket — a capability Twilio Media Streams lacks. Mirrors Python
 * ``getpatter.telephony.plivo``.
 */
export class PlivoBridge implements TelephonyBridge {
  readonly label = 'Plivo';
  readonly telephonyProvider = 'plivo' as const;
  readonly inputWireFormat = 'ulaw_8000' as const;
  private readonly authHeader: string;
  private readonly apiBase: string;

  constructor(private readonly config: LocalConfig) {
    const authId = config.plivoAuthId ?? '';
    const authToken = config.plivoAuthToken ?? '';
    this.authHeader = `Basic ${Buffer.from(`${authId}:${authToken}`).toString('base64')}`;
    this.apiBase = `https://api.plivo.com/v1/Account/${encodeURIComponent(authId)}`;
  }

  sendAudio(ws: WSWebSocket, audioBase64: string, _streamSid: string): void {
    ws.send(
      JSON.stringify({
        event: 'playAudio',
        media: { contentType: 'audio/x-mulaw', sampleRate: 8000, payload: audioBase64 },
      }),
    );
  }

  sendMark(ws: WSWebSocket, markName: string, streamSid: string): void {
    // Plivo acks with a ``playedStream`` event carrying the same name — the
    // analogue of Twilio's mark protocol.
    ws.send(JSON.stringify({ event: 'checkpoint', streamId: streamSid, name: markName }));
  }

  sendClear(ws: WSWebSocket, streamSid: string): void {
    ws.send(JSON.stringify({ event: 'clearAudio', streamId: streamSid }));
  }

  async transferCall(callId: string, toNumber: string): Promise<void> {
    if (!/^\+[1-9]\d{6,14}$/.test(toNumber)) {
      getLogger().warn(`PlivoBridge.transferCall rejected: invalid target ${JSON.stringify(toNumber)}`);
      return;
    }
    if (!this.config.plivoAuthId || !this.config.plivoAuthToken || !callId) return;
    if (!this.config.webhookUrl) {
      getLogger().warn('PlivoBridge.transferCall skipped: no webhookUrl for aleg_url');
      return;
    }
    // Plivo blind transfer redirects the A-leg to new XML served by
    // ``/webhooks/plivo/transfer``.
    const alegUrl = `https://${this.config.webhookUrl}/webhooks/plivo/transfer?to=${encodeURIComponent(toNumber)}`;
    await fetch(`${this.apiBase}/Call/${encodeURIComponent(callId)}/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: this.authHeader },
      body: JSON.stringify({ legs: 'aleg', aleg_url: alegUrl, aleg_method: 'GET' }),
    });
    getLogger().info(`Call transferred to ${toNumber}`);
  }

  async sendDtmf(ws: WSWebSocket, _callId: string, digits: string, _delayMs: number): Promise<void> {
    // Plivo sends DTMF over the media WebSocket (not a REST action).
    const filtered = Array.from(digits ?? '').filter((d) => PLIVO_DTMF_ALLOWED.has(d)).join('');
    if (!filtered) {
      getLogger().warn(`PlivoBridge.sendDtmf: no valid digits in ${JSON.stringify(digits)}`);
      return;
    }
    ws.send(JSON.stringify({ event: 'sendDTMF', dtmf: filtered }));
  }

  async startRecording(callId: string): Promise<void> {
    if (!this.config.plivoAuthId || !this.config.plivoAuthToken || !callId) return;
    try {
      const resp = await fetch(`${this.apiBase}/Call/${encodeURIComponent(callId)}/Record/`, {
        method: 'POST',
        headers: { Authorization: this.authHeader },
      });
      if (!resp.ok) {
        getLogger().warn(`Plivo record start failed (${resp.status}): ${(await resp.text()).slice(0, 200)}`);
      } else {
        getLogger().info('Plivo recording started');
      }
    } catch (e) {
      getLogger().warn(`Plivo record start error: ${String(e)}`);
    }
  }

  async endCall(callId: string, _ws: WSWebSocket): Promise<void> {
    if (!this.config.plivoAuthId || !this.config.plivoAuthToken || !callId) return;
    try {
      const resp = await fetch(`${this.apiBase}/Call/${encodeURIComponent(callId)}/`, {
        method: 'DELETE',
        headers: { Authorization: this.authHeader },
      });
      if (!resp.ok && resp.status !== 404) {
        getLogger().warn(`Plivo hangup returned ${resp.status}`);
      }
    } catch {
      /* best effort — call may already be ended */
    }
  }

  createStt(agent: AgentOptions): Promise<STTAdapter | null> {
    return createSTT(agent);
  }

  async queryTelephonyCost(metricsAcc: CallMetricsAccumulator, callId: string): Promise<void> {
    if (!this.config.plivoAuthId || !this.config.plivoAuthToken || !callId) return;
    try {
      const resp = await fetch(`${this.apiBase}/Call/${encodeURIComponent(callId)}/`, {
        headers: { Authorization: this.authHeader },
        signal: AbortSignal.timeout(5000),
      });
      if (resp.ok) {
        const data = (await resp.json()) as { total_amount?: string };
        if (data.total_amount != null) {
          metricsAcc.setActualTelephonyCost(Math.abs(parseFloat(data.total_amount)));
          getLogger().info(`Plivo actual cost: $${data.total_amount}`);
        }
      }
    } catch (err) {
      getLogger().debug(`queryTelephonyCost(plivo) failed: ${(err as Error)?.message ?? err}`);
    }
  }
}

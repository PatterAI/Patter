/**
 * Plivo telephony adapter — parity with Python ``PlivoAdapter``.
 *
 * Talks directly to Plivo's REST API via ``fetch`` (HTTP Basic auth). Avoids
 * any dependency on the official ``plivo`` Node SDK so the Patter SDK stays
 * small.
 *
 * Unlike Twilio (``<Stream>`` inline in the call-create TwiML) and Telnyx
 * (``streaming_start`` after answer), Plivo points the outbound call at an
 * ``answerUrl``; the same ``/webhooks/plivo/voice`` route that serves inbound
 * calls returns the ``<Stream>`` XML.
 *
 * See also: ``libraries/python/getpatter/providers/plivo_adapter.py``.
 */
import { getLogger } from '../logger';

const PLIVO_API_BASE = 'https://api.plivo.com/v1';

/**
 * Speak a voicemail message on a machine-answered Plivo call, then hang up.
 *
 * Mirrors Python's ``handle_amd_result`` in ``telephony/plivo.py``. Uses
 * Plivo's live-call Speak API, waits an estimated playout window (~60 ms/char,
 * capped at 30 s) so the message isn't cut off, then hangs up via DELETE.
 * Best-effort — errors are logged and never raised back into the caller.
 */
export async function dropPlivoVoicemail(
  callUuid: string,
  voicemailMessage: string,
  authId: string,
  authToken: string,
): Promise<void> {
  if (!callUuid || !voicemailMessage || !authId || !authToken) return;
  const auth = `Basic ${Buffer.from(`${authId}:${authToken}`).toString('base64')}`;
  const base = `${PLIVO_API_BASE}/Account/${encodeURIComponent(authId)}/Call/${encodeURIComponent(callUuid)}`;
  try {
    // Plivo's Speak API expects form-encoded body per the docs
    // (https://www.plivo.com/docs/voice/api/call/speak-text-on-calls).
    const speak = await fetch(`${base}/Speak/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', Authorization: auth },
      body: new URLSearchParams({ text: voicemailMessage }).toString(),
      signal: AbortSignal.timeout(10_000),
    });
    if (!speak.ok) {
      getLogger().warn(
        `Plivo voicemail Speak failed (${speak.status}): ${(await speak.text()).slice(0, 200)}`,
      );
      return;
    }
    await new Promise<void>((r) =>
      setTimeout(r, Math.min(30_000, voicemailMessage.length * 60)),
    );
    await fetch(`${base}/`, { method: 'DELETE', headers: { Authorization: auth } });
    getLogger().info(`Voicemail dropped for ${callUuid}`);
  } catch (e) {
    getLogger().warn(`Could not drop voicemail: ${String(e)}`);
  }
}

/** XML-escape a string. The WSS URL embedded as ``<Stream>`` text content
 * carries a query string whose ``&`` separators MUST become ``&amp;`` or
 * Plivo's XML parser truncates the URL at the first parameter. */
export function xmlEscapePlivo(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

/** Options accepted by {@link PlivoAdapter.initiateCall}. */
export interface InitiateCallOptions {
  from: string;
  to: string;
  /** Public URL returning the ``<Stream>`` answer XML. */
  answerUrl: string;
  /** Max seconds to ring before no-answer. */
  ringTimeout?: number;
  /** Enable answering-machine detection. */
  machineDetection?: boolean;
  /** Async AMD result callback (no answer-latency penalty on human pickups). */
  machineDetectionUrl?: string;
}

/** Result returned by {@link PlivoAdapter.initiateCall}. */
export interface InitiateCallResult {
  /** Plivo's queued-call handle. The live CallUUID arrives on the answer
   *  webhook / WS ``start`` frame. */
  readonly requestUuid: string;
}

interface PlivoCallPayload {
  request_uuid?: string;
}

interface PlivoNumberSearchPayload {
  objects?: Array<{ number?: string }>;
}

interface PlivoApplicationPayload {
  app_id?: string;
}

/** Direct REST adapter for Plivo Voice & Numbers API. */
export class PlivoAdapter {
  readonly authId: string;
  private readonly baseUrl: string;
  private readonly authHeader: string;

  constructor(authId: string, authToken: string) {
    if (!authId) throw new Error('PlivoAdapter: authId is required');
    if (!authToken) throw new Error('PlivoAdapter: authToken is required');
    this.authId = authId;
    this.baseUrl = `${PLIVO_API_BASE}/Account/${encodeURIComponent(authId)}`;
    this.authHeader = `Basic ${Buffer.from(`${authId}:${authToken}`).toString('base64')}`;
  }

  private async request<T>(
    method: 'GET' | 'POST' | 'DELETE',
    path: string,
    jsonBody?: unknown,
  ): Promise<{ status: number; data: T }> {
    const headers: Record<string, string> = { Authorization: this.authHeader };
    if (jsonBody !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: jsonBody !== undefined ? JSON.stringify(jsonBody) : undefined,
      signal: AbortSignal.timeout(30_000),
    });
    const text = await response.text();
    if (!response.ok && response.status !== 404) {
      throw new Error(`Plivo ${method} ${path} failed: ${response.status} ${text}`);
    }
    let data = {} as T;
    if (text) {
      try {
        data = JSON.parse(text) as T;
      } catch {
        /* Plivo returns empty body on 204 — leave data as {}. */
      }
    }
    return { status: response.status, data };
  }

  /** Search and rent an available Plivo number for the given ISO country. */
  async provisionNumber(countryIso: string): Promise<string> {
    const { data } = await this.request<PlivoNumberSearchPayload>(
      'GET',
      `/PhoneNumber/?country_iso=${encodeURIComponent(countryIso)}&limit=1`,
    );
    const number = data.objects?.[0]?.number;
    if (!number) throw new Error(`PlivoAdapter: no numbers available for ${countryIso}`);
    await this.request('POST', `/PhoneNumber/${encodeURIComponent(number)}/`);
    return number;
  }

  /**
   * Point the inbound answer flow for ``number`` at ``answerUrl`` by creating
   * (or reusing) a Plivo Application and linking the number to it. Most
   * production deployments pre-configure this in the Plivo console; this
   * mirrors Twilio's ``configureNumber`` auto-setup convenience.
   */
  async configureNumber(number: string, answerUrl: string): Promise<void> {
    const { data } = await this.request<PlivoApplicationPayload>('POST', '/Application/', {
      app_name: 'patter-inbound',
      answer_url: answerUrl,
      answer_method: 'POST',
    });
    if (!data.app_id) {
      getLogger().warn('Plivo Application create returned no app_id');
      return;
    }
    await this.request('POST', `/Number/${encodeURIComponent(number)}/`, { app_id: data.app_id });
  }

  /**
   * Place an outbound Plivo call routed through ``answerUrl``. Returns Plivo's
   * ``request_uuid``. The WSS URL travels inside the answer XML, not as a dial
   * parameter — mirroring the Python adapter.
   */
  async initiateCall(opts: InitiateCallOptions): Promise<InitiateCallResult> {
    const payload: Record<string, unknown> = {
      from: opts.from,
      to: opts.to,
      answer_url: opts.answerUrl,
      answer_method: 'POST',
    };
    if (opts.ringTimeout != null) payload.ring_timeout = Math.max(1, Math.floor(opts.ringTimeout));
    if (opts.machineDetection) {
      payload.machine_detection = 'true';
      payload.machine_detection_time = 5000;
      if (opts.machineDetectionUrl) {
        payload.machine_detection_url = opts.machineDetectionUrl;
        payload.machine_detection_method = 'POST';
      }
    }
    const { data } = await this.request<PlivoCallPayload>('POST', '/Call/', payload);
    return { requestUuid: data.request_uuid ?? '' };
  }

  /** Hang up an active Plivo call by CallUUID. 204 and 404 are both success. */
  async endCall(callUuid: string): Promise<void> {
    if (!callUuid) throw new Error('PlivoAdapter: callUuid is required');
    try {
      await this.request('DELETE', `/Call/${encodeURIComponent(callUuid)}/`);
    } catch (err) {
      getLogger().warn(`[PlivoAdapter] endCall failed for ${callUuid}: ${String(err)}`);
      throw err;
    }
  }

  /**
   * Build the Plivo answer XML. Unlike Twilio (``url=`` attribute), Plivo's
   * ``<Stream>`` takes the WSS URL as its **text content**. ``bidirectional``
   * enables two-way audio; ``keepCallAlive`` keeps the leg up for the lifetime
   * of the WebSocket. ``extraHeaders`` (comma-separated ``key=value``) is
   * delivered back on the WS ``start`` frame as a caller/callee fallback.
   *
   * Mirrors the Python adapter's ``generate_stream_xml``.
   */
  static generateStreamXml(
    streamUrl: string,
    contentType = 'audio/x-mulaw;rate=8000',
    extraHeaders?: Record<string, string>,
  ): string {
    let attrs = `bidirectional="true" keepCallAlive="true" contentType="${xmlEscapePlivo(contentType)}"`;
    if (extraHeaders) {
      const joined = Object.entries(extraHeaders)
        .map(([k, v]) => `${k}=${v}`)
        .join(',');
      attrs += ` extraHeaders="${xmlEscapePlivo(joined)}"`;
    }
    return `<Response><Stream ${attrs}>${xmlEscapePlivo(streamUrl)}</Stream></Response>`;
  }
}

/**
 * Twilio telephony adapter — parity with Python ``TwilioAdapter``.
 *
 * This adapter talks directly to Twilio's REST API via ``fetch`` (Basic auth).
 * It intentionally avoids any dependency on the official ``twilio`` Node SDK so
 * the Patter SDK stays small.
 *
 * See also: ``sdk-py/getpatter/providers/twilio_adapter.py``.
 */
import { getLogger } from '../logger';

const TWILIO_API_BASE = 'https://api.twilio.com/2010-04-01';

export interface TwilioAdapterOptions {
  /** Optional Twilio edge region (e.g. ``ie1`` for Ireland). */
  region?: string;
}

export interface ProvisionNumberOptions {
  /** ISO-3166-1 alpha-2 country code, e.g. ``"US"``. */
  countryCode: string;
  /** Optional North-American area code (e.g. ``"415"``). */
  areaCode?: string;
}

export interface ProvisionNumberResult {
  readonly phoneNumber: string;
  readonly sid: string;
}

export interface ConfigureNumberOptions {
  /** URL Twilio should hit when the number receives a call. */
  voiceUrl: string;
  /** Optional status callback URL for call lifecycle events. */
  statusCallback?: string;
}

export interface InitiateCallOptions {
  from: string;
  to: string;
  /**
   * TwiML or absolute URL Twilio should request when the call connects.
   * Mutually exclusive with ``streamUrl`` — provide exactly one.
   */
  url?: string;
  /**
   * Optional WebSocket stream URL. When provided (and ``url`` is not), the
   * adapter auto-builds a ``<Response><Connect><Stream>`` TwiML document
   * via :meth:`generateStreamTwiml` and sends it as the ``Twiml`` form
   * parameter. Mirrors the Python adapter's ``stream_url`` convenience path.
   */
  streamUrl?: string;
  statusCallback?: string;
  /** Value accepted by Twilio's ``MachineDetection`` parameter. */
  machineDetection?: 'Enable' | 'DetectMessageEnd' | 'false';
  /** Raw extra form parameters forwarded to the Calls endpoint. */
  extraParams?: Record<string, string>;
}

export interface InitiateCallResult {
  readonly callSid: string;
}

interface TwilioAvailableNumberPayload {
  available_phone_numbers?: Array<{ phone_number?: string }>;
}

interface TwilioIncomingNumberPayload {
  sid?: string;
  phone_number?: string;
}

interface TwilioCallPayload {
  sid?: string;
}

export class TwilioAdapter {
  readonly accountSid: string;
  readonly region: string | undefined;
  private readonly baseUrl: string;
  private readonly authHeader: string;

  constructor(accountSid: string, authToken: string, opts: TwilioAdapterOptions = {}) {
    if (!accountSid) throw new Error('TwilioAdapter: accountSid is required');
    if (!authToken) throw new Error('TwilioAdapter: authToken is required');
    this.accountSid = accountSid;
    this.region = opts.region;
    // Twilio's edge/region prefix looks like ``https://api.ie1.twilio.com``.
    this.baseUrl = opts.region
      ? `https://api.${opts.region}.twilio.com/2010-04-01`
      : TWILIO_API_BASE;
    this.authHeader = `Basic ${Buffer.from(`${accountSid}:${authToken}`).toString('base64')}`;
  }

  private async request<T>(
    method: 'GET' | 'POST',
    path: string,
    body?: URLSearchParams,
  ): Promise<T> {
    const url = `${this.baseUrl}/Accounts/${encodeURIComponent(this.accountSid)}${path}`;
    const headers: Record<string, string> = { Authorization: this.authHeader };
    if (body) headers['Content-Type'] = 'application/x-www-form-urlencoded';

    const response = await fetch(url, {
      method,
      headers,
      body: body ? body.toString() : undefined,
      signal: AbortSignal.timeout(30_000),
    });

    const text = await response.text();
    if (!response.ok) {
      throw new Error(`Twilio ${method} ${path} failed: ${response.status} ${text}`);
    }
    if (!text) return {} as T;
    try {
      return JSON.parse(text) as T;
    } catch (e) {
      throw new Error(`Twilio returned non-JSON response: ${String(e)}`);
    }
  }

  /**
   * Provision a local phone number in the given country.
   *
   * Lists available local numbers, then purchases the first match.
   */
  async provisionNumber(opts: ProvisionNumberOptions): Promise<ProvisionNumberResult> {
    const country = encodeURIComponent(opts.countryCode);
    const queryParts: string[] = ['PageSize=1'];
    if (opts.areaCode) queryParts.push(`AreaCode=${encodeURIComponent(opts.areaCode)}`);
    const path = `/AvailablePhoneNumbers/${country}/Local.json?${queryParts.join('&')}`;

    const available = await this.request<TwilioAvailableNumberPayload>('GET', path);
    const first = available.available_phone_numbers?.[0]?.phone_number;
    if (!first) {
      throw new Error(`TwilioAdapter: no numbers available for country ${opts.countryCode}`);
    }

    const body = new URLSearchParams({ PhoneNumber: first });
    const purchased = await this.request<TwilioIncomingNumberPayload>(
      'POST',
      '/IncomingPhoneNumbers.json',
      body,
    );
    if (!purchased.sid || !purchased.phone_number) {
      throw new Error('TwilioAdapter: malformed response from IncomingPhoneNumbers.create');
    }
    return { phoneNumber: purchased.phone_number, sid: purchased.sid };
  }

  /** Update an already-purchased number to point at our voice webhook. */
  async configureNumber(phoneNumberSid: string, opts: ConfigureNumberOptions): Promise<void> {
    if (!phoneNumberSid) throw new Error('TwilioAdapter: phoneNumberSid is required');
    const body = new URLSearchParams({
      VoiceUrl: opts.voiceUrl,
      VoiceMethod: 'POST',
    });
    if (opts.statusCallback) body.set('StatusCallback', opts.statusCallback);
    await this.request<TwilioIncomingNumberPayload>(
      'POST',
      `/IncomingPhoneNumbers/${encodeURIComponent(phoneNumberSid)}.json`,
      body,
    );
  }

  /** Place an outbound call. Returns the Twilio call SID. */
  async initiateCall(opts: InitiateCallOptions): Promise<InitiateCallResult> {
    if (!opts.url && !opts.streamUrl) {
      throw new Error('TwilioAdapter: initiateCall requires either url or streamUrl');
    }
    const body = new URLSearchParams({
      From: opts.from,
      To: opts.to,
    });
    if (opts.url) {
      body.set('Url', opts.url);
    } else if (opts.streamUrl) {
      // Mirror the Python adapter: auto-build a ``<Connect><Stream>``
      // TwiML doc and inline it via the ``Twiml`` form parameter.
      body.set('Twiml', TwilioAdapter.generateStreamTwiml(opts.streamUrl));
    }
    if (opts.statusCallback) body.set('StatusCallback', opts.statusCallback);
    if (opts.machineDetection) body.set('MachineDetection', opts.machineDetection);
    if (opts.extraParams) {
      for (const [key, value] of Object.entries(opts.extraParams)) {
        body.set(key, value);
      }
    }

    const call = await this.request<TwilioCallPayload>('POST', '/Calls.json', body);
    if (!call.sid) {
      throw new Error('TwilioAdapter: Calls.create returned no SID');
    }
    return { callSid: call.sid };
  }

  /**
   * Build a minimal ``<Response><Connect><Stream url="..."/></Connect></Response>``
   * TwiML document. Mirrors the Python adapter's ``generate_stream_twiml``.
   */
  static generateStreamTwiml(streamUrl: string): string {
    const escaped = streamUrl
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&apos;');
    return `<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="${escaped}"/></Connect></Response>`;
  }

  /** Force-complete an in-progress call. */
  async endCall(callSid: string): Promise<void> {
    if (!callSid) throw new Error('TwilioAdapter: callSid is required');
    const body = new URLSearchParams({ Status: 'completed' });
    try {
      await this.request<TwilioCallPayload>(
        'POST',
        `/Calls/${encodeURIComponent(callSid)}.json`,
        body,
      );
    } catch (err) {
      getLogger().warn(`[TwilioAdapter] endCall failed for ${callSid}: ${String(err)}`);
      throw err;
    }
  }
}

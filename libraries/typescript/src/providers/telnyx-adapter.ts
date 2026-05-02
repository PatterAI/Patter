/**
 * Telnyx telephony adapter — parity with Python ``TelnyxAdapter``.
 *
 * This adapter talks directly to Telnyx's REST API via ``fetch`` (Bearer auth)
 * and avoids the vendor SDK dependency.
 *
 * Wave 1 fixes applied (aligned with Python Wave1-Team7):
 *   - Do NOT pass ``stream_url`` when dialing — streams are attached via the
 *     Call Control Application, not per call.
 *   - Use ``/number_orders`` with a ``filter[phone_number]`` param on the
 *     ``/available_phone_numbers`` lookup so we reserve specifically the
 *     number we intend to buy.
 *   - Fully URL-encode ``callControlId`` in every path interpolation.
 *
 * See: ``libraries/python/getpatter/providers/telnyx_adapter.py``.
 */
import { randomUUID } from 'node:crypto';
import { getLogger } from '../logger';

const TELNYX_API_BASE = 'https://api.telnyx.com/v2';

/** Options accepted by {@link TelnyxAdapter.provisionNumber}. */
export interface ProvisionNumberOptions {
  /** ISO-3166-1 alpha-2 country code (e.g. ``"US"``). */
  countryCode: string;
}

/** Result returned by {@link TelnyxAdapter.provisionNumber}. */
export interface ProvisionNumberResult {
  readonly phoneNumber: string;
  readonly orderId: string;
}

/** Options accepted by {@link TelnyxAdapter.configureNumber}. */
export interface ConfigureNumberOptions {
  /** Telnyx Call Control Application / Connection ID. */
  connectionId: string;
}

/** Options accepted by {@link TelnyxAdapter.initiateCall}. */
export interface InitiateCallOptions {
  from: string;
  to: string;
  /** Override ``connectionId`` at dial time. Falls back to the adapter default. */
  connectionId?: string;
  /** Opaque state string that Telnyx echoes back on webhooks. Base64-encoded on wire. */
  clientState?: string;
}

/** Result returned by {@link TelnyxAdapter.initiateCall}. */
export interface InitiateCallResult {
  readonly callControlId: string;
}

/** Options accepted by {@link TelnyxAdapter.endCall}. */
export interface EndCallOptions {
  /** Idempotency key for the hangup command. */
  commandId?: string;
}

interface TelnyxAvailableNumbersPayload {
  data?: Array<{ phone_number?: string }>;
}

interface TelnyxNumberOrderPayload {
  data?: { id?: string };
}

interface TelnyxCallPayload {
  data?: { call_control_id?: string };
}

/** Direct REST adapter for Telnyx Call Control & Numbers API. */
export class TelnyxAdapter {
  private readonly apiKey: string;
  readonly connectionId: string | undefined;
  private readonly baseUrl: string = TELNYX_API_BASE;

  constructor(apiKey: string, connectionId?: string) {
    if (!apiKey) throw new Error('TelnyxAdapter: apiKey is required');
    this.apiKey = apiKey;
    this.connectionId = connectionId;
  }

  private async request<T>(
    method: 'GET' | 'POST' | 'PATCH',
    path: string,
    body?: Record<string, unknown>,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
    };
    if (body !== undefined) headers['Content-Type'] = 'application/json';

    const response = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(30_000),
    });

    const text = await response.text();
    if (!response.ok) {
      throw new Error(`Telnyx ${method} ${path} failed: ${response.status} ${text}`);
    }
    if (!text) return {} as T;
    try {
      return JSON.parse(text) as T;
    } catch (e) {
      throw new Error(`Telnyx returned non-JSON response: ${String(e)}`);
    }
  }

  /**
   * Search available numbers for ``countryCode`` and place an order for the
   * first match. Returns both the reserved E.164 number and the order ID.
   */
  async provisionNumber(opts: ProvisionNumberOptions): Promise<ProvisionNumberResult> {
    const country = encodeURIComponent(opts.countryCode);
    // Telnyx search filter uses nested ``filter[phone_number][country_code]``
    // (not ``filter[country_code]``). See the Python adapter and the
    // telnyx-numbers-compliance skill.
    const searchPath =
      `/available_phone_numbers?filter[phone_number][country_code]=${country}&filter[limit]=1`;
    const available = await this.request<TelnyxAvailableNumbersPayload>('GET', searchPath);
    const chosen = available.data?.[0]?.phone_number;
    if (!chosen) {
      throw new Error(`TelnyxAdapter: no numbers available for ${opts.countryCode}`);
    }
    // When a Call Control Application is bound to this adapter, attach it to
    // the order so the newly-purchased number is immediately ready to place
    // and receive calls without a follow-up PATCH.
    const orderBody: Record<string, unknown> = {
      phone_numbers: [{ phone_number: chosen }],
    };
    if (this.connectionId) {
      orderBody.connection_id = this.connectionId;
    }
    const order = await this.request<TelnyxNumberOrderPayload>(
      'POST',
      '/number_orders',
      orderBody,
    );
    const orderId = order.data?.id ?? '';
    return { phoneNumber: chosen, orderId };
  }

  /** Attach a number to a Call Control Application. */
  async configureNumber(
    phoneNumber: string,
    opts: ConfigureNumberOptions,
  ): Promise<void> {
    if (!phoneNumber) throw new Error('TelnyxAdapter: phoneNumber is required');
    if (!opts.connectionId) throw new Error('TelnyxAdapter: connectionId is required');
    // Use ``PATCH /phone_numbers/{id}/voice`` — the correct voice settings
    // endpoint per the Telnyx numbers skill. The older
    // ``PATCH /phone_numbers/{id}`` endpoint does not accept ``connection_id``
    // consistently across the v2 API. ``phoneNumber`` may be the
    // phone_number ID or the E.164 string; both are accepted.
    await this.request<unknown>(
      'PATCH',
      `/phone_numbers/${encodeURIComponent(phoneNumber)}/voice`,
      { connection_id: opts.connectionId, tech_prefix_enabled: false },
    );
  }

  /**
   * Place an outbound call on the Call Control Application.
   *
   * Note: we intentionally do NOT pass ``stream_url`` here — audio streaming
   * is configured on the Application itself (or started explicitly via a
   * ``streaming_start`` command). Passing ``stream_url`` on dial is a
   * deprecated code path that Telnyx rejects in newer API versions.
   */
  async initiateCall(opts: InitiateCallOptions): Promise<InitiateCallResult> {
    const connectionId = opts.connectionId ?? this.connectionId;
    if (!connectionId) {
      throw new Error('TelnyxAdapter: connectionId must be provided to initiateCall');
    }
    const payload: Record<string, unknown> = {
      connection_id: connectionId,
      from: opts.from,
      to: opts.to,
    };
    if (opts.clientState) {
      payload.client_state = Buffer.from(opts.clientState, 'utf-8').toString('base64');
    }
    const resp = await this.request<TelnyxCallPayload>('POST', '/calls', payload);
    const callControlId = resp.data?.call_control_id;
    if (!callControlId) {
      throw new Error('TelnyxAdapter: /calls returned no call_control_id');
    }
    return { callControlId };
  }

  /** Hang up an in-progress call. */
  async endCall(callControlId: string, opts: EndCallOptions = {}): Promise<void> {
    if (!callControlId) throw new Error('TelnyxAdapter: callControlId is required');
    const encoded = encodeURIComponent(callControlId);
    // ``command_id`` provides idempotency on retries (Telnyx will ignore a
    // duplicate action with the same ``command_id``). When omitted we
    // auto-generate a UUID4 to match the Python adapter's behaviour.
    const body: Record<string, unknown> = {
      command_id: opts.commandId ?? randomUUID(),
    };
    try {
      await this.request<unknown>(
        'POST',
        `/calls/${encoded}/actions/hangup`,
        body,
      );
    } catch (err) {
      getLogger().warn(
        `[TelnyxAdapter] endCall failed for ${callControlId}: ${String(err)}`,
      );
      throw err;
    }
  }
}

/**
 * Carrier auto-configuration helpers.
 *
 * Mirror of Python's `TwilioAdapter.configure_number()` /
 * `TelnyxAdapter.configure_number()`: called from `serve()` once the public
 * webhookUrl is known, so that inbound calls "just work" without requiring
 * the user to open the Twilio/Telnyx console.
 */
import { getLogger } from './logger';

const TWILIO_API_BASE = 'https://api.twilio.com/2010-04-01';
const TELNYX_API_BASE = 'https://api.telnyx.com/v2';

interface TwilioIncomingNumber {
  sid: string;
  phone_number: string;
}

interface TwilioListResponse {
  incoming_phone_numbers: TwilioIncomingNumber[];
}

/**
 * Set the voice webhook (`voice_url`) on an existing Twilio phone number so
 * that inbound calls hit our embedded server. The URL is expected to be the
 * fully-qualified public URL (e.g. `https://abc.trycloudflare.com/webhooks/twilio/voice`).
 *
 * Uses the Twilio REST API directly to avoid pulling in the `twilio` npm
 * package as a runtime dependency.
 */
export async function configureTwilioNumber(
  accountSid: string,
  authToken: string,
  phoneNumber: string,
  voiceUrl: string,
): Promise<void> {
  const auth = `Basic ${Buffer.from(`${accountSid}:${authToken}`).toString('base64')}`;
  const listUrl =
    `${TWILIO_API_BASE}/Accounts/${accountSid}/IncomingPhoneNumbers.json` +
    `?PhoneNumber=${encodeURIComponent(phoneNumber)}`;

  const listResp = await fetch(listUrl, {
    method: 'GET',
    headers: { Authorization: auth },
  });
  if (!listResp.ok) {
    throw new Error(
      `Twilio IncomingPhoneNumbers.list failed: ${listResp.status} ${await listResp.text()}`,
    );
  }
  const body = (await listResp.json()) as TwilioListResponse;
  const match = body.incoming_phone_numbers?.[0];
  if (!match) {
    throw new Error(`Twilio number ${phoneNumber} not found on account ${accountSid}`);
  }

  const updateUrl = `${TWILIO_API_BASE}/Accounts/${accountSid}/IncomingPhoneNumbers/${match.sid}.json`;
  const form = new URLSearchParams({ VoiceUrl: voiceUrl, VoiceMethod: 'POST' });
  const updateResp = await fetch(updateUrl, {
    method: 'POST',
    headers: {
      Authorization: auth,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: form.toString(),
  });
  if (!updateResp.ok) {
    throw new Error(
      `Twilio IncomingPhoneNumbers.update failed: ${updateResp.status} ${await updateResp.text()}`,
    );
  }
}

/**
 * Associate a Telnyx phone number with the configured Call Control Application.
 *
 * Telnyx routes inbound calls based on the number's `connection_id`, not a
 * per-number webhook URL. This mirrors Python's `TelnyxAdapter.configure_number()`.
 */
export async function configureTelnyxNumber(
  apiKey: string,
  connectionId: string,
  phoneNumber: string,
): Promise<void> {
  const resp = await fetch(`${TELNYX_API_BASE}/phone_numbers/${encodeURIComponent(phoneNumber)}`, {
    method: 'PATCH',
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ connection_id: connectionId }),
  });
  if (!resp.ok) {
    throw new Error(
      `Telnyx PATCH /phone_numbers/${phoneNumber} failed: ${resp.status} ${await resp.text()}`,
    );
  }
}

/**
 * Best-effort auto-configuration invoked from `serve()` once the public
 * hostname is known. Any failure is logged and swallowed — the user can
 * still set the webhook manually in the provider console.
 */
export async function autoConfigureCarrier(params: {
  telephonyProvider?: 'twilio' | 'telnyx';
  twilioSid?: string;
  twilioToken?: string;
  telnyxKey?: string;
  telnyxConnectionId?: string;
  phoneNumber: string;
  webhookHost: string;
}): Promise<void> {
  const log = getLogger();
  const provider = params.telephonyProvider ?? (params.twilioSid ? 'twilio' : 'telnyx');

  if (provider === 'twilio' && params.twilioSid && params.twilioToken) {
    const voiceUrl = `https://${params.webhookHost}/webhooks/twilio/voice`;
    try {
      await configureTwilioNumber(params.twilioSid, params.twilioToken, params.phoneNumber, voiceUrl);
      log.info('Twilio webhook set to %s', voiceUrl);
    } catch (err) {
      log.warn('Could not auto-configure Twilio webhook: %s', err instanceof Error ? err.message : String(err));
      log.info('Set webhook manually to: %s', voiceUrl);
    }
    return;
  }

  if (provider === 'telnyx' && params.telnyxKey && params.telnyxConnectionId) {
    try {
      await configureTelnyxNumber(params.telnyxKey, params.telnyxConnectionId, params.phoneNumber);
      log.info('Telnyx number %s associated with connection %s', params.phoneNumber, params.telnyxConnectionId);
    } catch (err) {
      log.warn('Could not auto-configure Telnyx number: %s', err instanceof Error ? err.message : String(err));
    }
  }
}

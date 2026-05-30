/**
 * Carrier auto-configuration helpers.
 *
 * Mirror of Python's `TwilioAdapter.configure_number()` /
 * `TelnyxAdapter.configure_number()`: called from `serve()` once the public
 * webhookUrl is known, so that inbound calls "just work" without requiring
 * the user to open the Twilio/Telnyx console.
 */
import { getLogger } from './logger';
import type { CarrierKind } from './types';

/** Mask a phone number to last-4 digits for safe use in logs / errors. */
function redactPhone(n: string): string {
  return n.slice(0, 3) + '***' + n.slice(-4);
}

const TWILIO_API_BASE = 'https://api.twilio.com/2010-04-01';
const TELNYX_API_BASE = 'https://api.telnyx.com/v2';
const PLIVO_API_BASE = 'https://api.plivo.com/v1';

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
    throw new Error(`Twilio number ${redactPhone(phoneNumber)} not found on account ${accountSid}`);
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
  const resp = await fetch(
    `${TELNYX_API_BASE}/phone_numbers/${encodeURIComponent(phoneNumber)}/voice`,
    {
      method: 'PATCH',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ connection_id: connectionId, tech_prefix_enabled: false }),
    },
  );
  if (!resp.ok) {
    throw new Error(
      `Telnyx PATCH /phone_numbers/${redactPhone(phoneNumber)}/voice failed: ${resp.status} ${await resp.text()}`,
    );
  }
}

/**
 * Point a Plivo number's inbound answer flow at our embedded server.
 *
 * Plivo routes inbound calls through an Application, so we create (or reuse)
 * an application bound to `answerUrl` and link the number to it. Mirrors
 * Python's `PlivoAdapter.configure_number()`.
 */
export async function configurePlivoNumber(
  authId: string,
  authToken: string,
  phoneNumber: string,
  answerUrl: string,
): Promise<void> {
  const auth = `Basic ${Buffer.from(`${authId}:${authToken}`).toString('base64')}`;
  const base = `${PLIVO_API_BASE}/Account/${encodeURIComponent(authId)}`;
  const appResp = await fetch(`${base}/Application/`, {
    method: 'POST',
    headers: { Authorization: auth, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      app_name: 'patter-inbound',
      answer_url: answerUrl,
      answer_method: 'POST',
    }),
  });
  if (!appResp.ok) {
    throw new Error(`Plivo Application create failed: ${appResp.status} ${await appResp.text()}`);
  }
  const appBody = (await appResp.json()) as { app_id?: string };
  if (!appBody.app_id) {
    getLogger().warn('Plivo Application create returned no app_id');
    return;
  }
  const linkResp = await fetch(`${base}/Number/${encodeURIComponent(phoneNumber)}/`, {
    method: 'POST',
    headers: { Authorization: auth, 'Content-Type': 'application/json' },
    body: JSON.stringify({ app_id: appBody.app_id }),
  });
  if (!linkResp.ok) {
    throw new Error(`Plivo Number update failed: ${linkResp.status} ${await linkResp.text()}`);
  }
}

/**
 * Best-effort auto-configuration invoked from `serve()` once the public
 * hostname is known. Any failure is logged and swallowed — the user can
 * still set the webhook manually in the provider console.
 */
export async function autoConfigureCarrier(params: {
  telephonyProvider?: CarrierKind;
  twilioSid?: string;
  twilioToken?: string;
  telnyxKey?: string;
  telnyxConnectionId?: string;
  plivoAuthId?: string;
  plivoAuthToken?: string;
  phoneNumber: string;
  webhookHost: string;
}): Promise<void> {
  const log = getLogger();
  const provider =
    params.telephonyProvider ??
    (params.twilioSid ? 'twilio' : params.plivoAuthId ? 'plivo' : 'telnyx');

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
      log.info('Telnyx number ***%s associated with connection %s', params.phoneNumber.slice(-4), params.telnyxConnectionId);
    } catch (err) {
      log.warn('Could not auto-configure Telnyx number: %s', err instanceof Error ? err.message : String(err));
    }
    return;
  }

  if (provider === 'plivo' && params.plivoAuthId && params.plivoAuthToken) {
    const answerUrl = `https://${params.webhookHost}/webhooks/plivo/voice`;
    try {
      await configurePlivoNumber(params.plivoAuthId, params.plivoAuthToken, params.phoneNumber, answerUrl);
      log.info('Plivo answer URL set to %s', answerUrl);
    } catch (err) {
      log.warn('Could not auto-configure Plivo answer URL: %s', err instanceof Error ? err.message : String(err));
      log.info('Set the Plivo application answer URL manually to: %s', answerUrl);
    }
  }
}

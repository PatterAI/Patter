import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  autoConfigureCarrier,
  configureTelnyxNumber,
  configureTwilioNumber,
} from '../src/carrier-config';

const originalFetch = globalThis.fetch;

interface FetchCall {
  url: string;
  init?: RequestInit;
}

function mockFetch(handler: (call: FetchCall) => Response | Promise<Response>) {
  const calls: FetchCall[] = [];
  const impl: typeof fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : (input as URL).toString();
    const call = { url, init };
    calls.push(call);
    return handler(call);
  };
  globalThis.fetch = impl as typeof fetch;
  return calls;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe('configureTwilioNumber', () => {
  it('lists the number then PATCHes voice_url on the matching SID', async () => {
    const calls = mockFetch(({ url }) => {
      if (url.includes('/IncomingPhoneNumbers.json?PhoneNumber=')) {
        return new Response(
          JSON.stringify({
            incoming_phone_numbers: [{ sid: 'PN123', phone_number: '+15550001234' }],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/IncomingPhoneNumbers/PN123.json')) {
        return new Response('{}', { status: 200 });
      }
      return new Response('not found', { status: 404 });
    });

    await configureTwilioNumber(
      'AC_sid',
      'tok',
      '+15550001234',
      'https://abc.trycloudflare.com/webhooks/twilio/voice',
    );

    expect(calls).toHaveLength(2);

    const listCall = calls[0];
    expect(listCall.url).toContain('/Accounts/AC_sid/IncomingPhoneNumbers.json?PhoneNumber=');
    expect(listCall.url).toContain(encodeURIComponent('+15550001234'));
    expect((listCall.init?.headers as Record<string, string>)?.Authorization).toMatch(/^Basic /);

    const updateCall = calls[1];
    expect(updateCall.url).toBe(
      'https://api.twilio.com/2010-04-01/Accounts/AC_sid/IncomingPhoneNumbers/PN123.json',
    );
    expect(updateCall.init?.method).toBe('POST');
    const body = updateCall.init?.body as string;
    const params = new URLSearchParams(body);
    expect(params.get('VoiceUrl')).toBe('https://abc.trycloudflare.com/webhooks/twilio/voice');
    expect(params.get('VoiceMethod')).toBe('POST');
  });

  it('throws if the phone number is not on the account', async () => {
    mockFetch(() =>
      new Response(JSON.stringify({ incoming_phone_numbers: [] }), { status: 200 }),
    );

    await expect(
      configureTwilioNumber('AC_sid', 'tok', '+15550009999', 'https://x/webhooks/twilio/voice'),
    ).rejects.toThrow(/not found on account/);
  });

  it('throws if the list call fails', async () => {
    mockFetch(() => new Response('bad auth', { status: 401 }));

    await expect(
      configureTwilioNumber('AC_sid', 'tok', '+15550001234', 'https://x/webhooks/twilio/voice'),
    ).rejects.toThrow(/IncomingPhoneNumbers\.list failed: 401/);
  });
});

describe('configureTelnyxNumber', () => {
  it('PATCHes /phone_numbers/{number} with connection_id', async () => {
    const calls = mockFetch(() => new Response('{}', { status: 200 }));

    await configureTelnyxNumber('tk_live', '100200', '+15550001234');

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(
      `https://api.telnyx.com/v2/phone_numbers/${encodeURIComponent('+15550001234')}`,
    );
    expect(calls[0].init?.method).toBe('PATCH');
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ connection_id: '100200' });
  });

  it('throws on non-2xx response', async () => {
    mockFetch(() => new Response('nope', { status: 422 }));
    await expect(configureTelnyxNumber('tk', 'c', '+1')).rejects.toThrow(/422/);
  });
});

describe('autoConfigureCarrier', () => {
  it('swallows Twilio failures and logs a warning (does not throw)', async () => {
    mockFetch(() => new Response('boom', { status: 500 }));

    // Should NOT throw — carrier auto-config is best-effort.
    await expect(
      autoConfigureCarrier({
        telephonyProvider: 'twilio',
        twilioSid: 'AC_sid',
        twilioToken: 'tok',
        phoneNumber: '+15550001234',
        webhookHost: 'abc.trycloudflare.com',
      }),
    ).resolves.toBeUndefined();
  });

  it('no-ops when Twilio credentials are missing', async () => {
    const calls = mockFetch(() => new Response('{}', { status: 200 }));

    await autoConfigureCarrier({
      telephonyProvider: 'twilio',
      phoneNumber: '+15550001234',
      webhookHost: 'abc.trycloudflare.com',
    });

    expect(calls).toHaveLength(0);
  });

  it('calls Telnyx PATCH when telephonyProvider is telnyx', async () => {
    const calls = mockFetch(() => new Response('{}', { status: 200 }));

    await autoConfigureCarrier({
      telephonyProvider: 'telnyx',
      telnyxKey: 'tk_live',
      telnyxConnectionId: 'conn123',
      phoneNumber: '+15550001234',
      webhookHost: 'abc.trycloudflare.com',
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/phone_numbers/');
  });
});

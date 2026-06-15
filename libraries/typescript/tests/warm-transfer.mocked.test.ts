/**
 * [mocked] WARM TRANSFER (`transfer_call(mode: 'warm')`) — TypeScript parity
 * with `libraries/python/tests/unit/test_warm_transfer_unit.py`.
 *
 * Covers, mocking ONLY the carrier REST boundary (`fetch`):
 *   - The exact Twilio REST sequence for warm mode: (1) redirect the caller
 *     leg into a named conference with hold semantics, (2) create the target
 *     leg with a `<Say>` announcement + conference join.
 *   - Cold mode stays byte-identical to the historical single-POST `<Dial>`
 *     redirect.
 *   - Telnyx / Plivo return the documented "not yet supported" error envelope
 *     for warm mode and never touch the network.
 *   - The extended `transfer_call` tool schema (`mode` / `summary`).
 *   - The pipeline built-in transfer handler envelope behaviour.
 *   - The `/webhooks/twilio/conference` + `/webhooks/twilio/warm-status`
 *     server routes (signature-validated, fail-closed; failed target leg
 *     releases the parked caller).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import crypto from 'node:crypto';
import net from 'node:net';
import type { AddressInfo } from 'node:net';
import {
  EmbeddedServer,
  TwilioBridge,
  TelnyxBridge,
  TRANSFER_CALL_TOOL,
  warmTransferConferenceName,
} from '../src/server';
import type { LocalConfig } from '../src/server';
import { PlivoBridge } from '../src/telephony/plivo';
import { TwilioAdapter } from '../src/providers/twilio-adapter';
import { augmentWithBuiltinHandoffTools } from '../src/stream-handler';
import type { AgentOptions, TransferCallResult } from '../src/types';

const CALL_SID = 'CA' + 'a'.repeat(32);
const ACCOUNT_SID = 'AC' + '0'.repeat(32);

function makeConfig(overrides: Partial<LocalConfig> = {}): LocalConfig {
  return {
    twilioSid: ACCOUNT_SID,
    twilioToken: 'token',
    openaiKey: 'sk-test',
    phoneNumber: '+15552223333',
    webhookUrl: 'example.ngrok.io',
    telephonyProvider: 'twilio',
    requireSignature: false,
    ...overrides,
  };
}

function makeAgent(overrides: Partial<AgentOptions> = {}): AgentOptions {
  return {
    systemPrompt: 'You are helpful.',
    voice: 'alloy',
    model: 'gpt-realtime-mini',
    language: 'en',
    ...overrides,
  };
}

function getFreePort(): Promise<number> {
  return new Promise<number>((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address() as AddressInfo;
      srv.close(() => resolve(port));
    });
  });
}

/** Build a fake fetch Response with the given status. */
function fakeResponse(status = 200): Response {
  return {
    ok: status < 400,
    status,
    json: async () => ({}),
    text: async () => '',
  } as Response;
}

// ---------------------------------------------------------------------------
// Tool schema
// ---------------------------------------------------------------------------

describe('transfer_call tool schema', () => {
  it('adds mode and summary parameters', () => {
    const props = TRANSFER_CALL_TOOL.parameters.properties as Record<string, { enum?: string[] }>;
    expect(Object.keys(props).sort()).toEqual(['mode', 'number', 'summary']);
    expect(props.mode.enum).toEqual(['cold', 'warm']);
    // `number` stays the only required field — mode defaults to cold.
    expect(TRANSFER_CALL_TOOL.parameters.required).toEqual(['number']);
  });

  it('keeps the number field unchanged', () => {
    expect(TRANSFER_CALL_TOOL.parameters.properties.number).toEqual({
      type: 'string',
      description: 'Phone number to transfer to (E.164 format)',
    });
  });
});

// ---------------------------------------------------------------------------
// TwiML builders
// ---------------------------------------------------------------------------

describe('TwilioAdapter warm-transfer TwiML builders', () => {
  it('caller TwiML parks with hold semantics + status callback', () => {
    const twiml = TwilioAdapter.generateWarmTransferCallerTwiml(
      'patter-warm-' + CALL_SID,
      'https://example.ngrok.io/webhooks/twilio/conference',
    );
    expect(twiml).toContain('startConferenceOnEnter="false"');
    expect(twiml).toContain('endConferenceOnExit="true"');
    expect(twiml).toContain('patter-warm-' + CALL_SID);
    expect(twiml).toContain('statusCallback="https://example.ngrok.io/webhooks/twilio/conference"');
    expect(twiml).toContain('statusCallbackEvent="start end join leave"');
  });

  it('caller TwiML omits callbacks when no URL given', () => {
    const twiml = TwilioAdapter.generateWarmTransferCallerTwiml('conf-x');
    expect(twiml).not.toContain('statusCallback');
  });

  it('target TwiML speaks the summary then joins with start-on-enter', () => {
    const twiml = TwilioAdapter.generateWarmTransferTargetTwiml(
      'conf-x',
      'Mario needs help with order 12.',
    );
    expect(twiml).toContain('<Say>Mario needs help with order 12.</Say>');
    expect(twiml.indexOf('<Say>')).toBeLessThan(twiml.indexOf('<Conference'));
    expect(twiml).toContain('startConferenceOnEnter="true"');
    expect(twiml).toContain('endConferenceOnExit="true"');
  });

  it('target TwiML skips <Say> when summary is empty', () => {
    const twiml = TwilioAdapter.generateWarmTransferTargetTwiml('conf-x');
    expect(twiml).not.toContain('<Say>');
  });

  it('target TwiML XML-escapes the summary', () => {
    const twiml = TwilioAdapter.generateWarmTransferTargetTwiml(
      'conf-x',
      '<script>"a" & b</script>',
    );
    expect(twiml).not.toContain('<script>');
    expect(twiml).toContain('&lt;script&gt;');
  });
});

// ---------------------------------------------------------------------------
// TwilioBridge — exact REST sequence (fetch mocked = the carrier boundary)
// ---------------------------------------------------------------------------

describe('[mocked] TwilioBridge.transferCall', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(fakeResponse());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('cold mode issues EXACTLY the historical single POST with bare <Dial>', async () => {
    const bridge = new TwilioBridge(makeConfig());
    const result = await bridge.transferCall(CALL_SID, '+15550001111');
    expect(result).toBeUndefined(); // legacy contract: cold resolves void
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      `https://api.twilio.com/2010-04-01/Accounts/${ACCOUNT_SID}/Calls/${CALL_SID}.json`,
    );
    const body = new URLSearchParams(String(init.body));
    expect(body.get('Twiml')).toBe('<Response><Dial>+15550001111</Dial></Response>');
  });

  it('warm mode runs the two-POST conference sequence', async () => {
    const bridge = new TwilioBridge(makeConfig());
    const result = (await bridge.transferCall(CALL_SID, '+15550001111', {
      mode: 'warm',
      summary: 'Mario needs help with order 12.',
    })) as TransferCallResult;

    expect(result.status).toBe('transferring');
    expect(result.mode).toBe('warm');
    expect(result.to).toBe('+15550001111');
    expect(result.conference).toBe(warmTransferConferenceName(CALL_SID));
    expect(fetchSpy).toHaveBeenCalledTimes(2);

    // POST 1 — caller leg redirected into the conference (on hold).
    const [url1, init1] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url1).toBe(
      `https://api.twilio.com/2010-04-01/Accounts/${ACCOUNT_SID}/Calls/${CALL_SID}.json`,
    );
    const twiml1 = new URLSearchParams(String(init1.body)).get('Twiml') ?? '';
    expect(twiml1).toContain(`patter-warm-${CALL_SID}`);
    expect(twiml1).toContain('startConferenceOnEnter="false"');
    expect(twiml1).toContain('endConferenceOnExit="true"');
    // Conference status callback registered.
    expect(twiml1).toContain('https://example.ngrok.io/webhooks/twilio/conference');

    // POST 2 — target (human agent) leg created with Say announcement.
    const [url2, init2] = fetchSpy.mock.calls[1] as [string, RequestInit];
    expect(url2).toBe(
      `https://api.twilio.com/2010-04-01/Accounts/${ACCOUNT_SID}/Calls.json`,
    );
    const body2 = new URLSearchParams(String(init2.body));
    expect(body2.get('To')).toBe('+15550001111');
    expect(body2.get('From')).toBe('+15552223333'); // config.phoneNumber
    const twiml2 = body2.get('Twiml') ?? '';
    expect(twiml2).toContain('<Say>Mario needs help with order 12.</Say>');
    expect(twiml2).toContain(`patter-warm-${CALL_SID}`);
    expect(twiml2).toContain('startConferenceOnEnter="true"');
    expect(twiml2).toContain('endConferenceOnExit="true"');
    // Target-leg terminal status callback carries the caller's CallSid.
    expect(body2.get('StatusCallback')).toBe(
      `https://example.ngrok.io/webhooks/twilio/warm-status?caller_call_sid=${CALL_SID}`,
    );
    expect(body2.get('StatusCallbackEvent')).toBe('completed');
  });

  it('warm mode omits callbacks when webhookUrl is empty', async () => {
    const bridge = new TwilioBridge(makeConfig({ webhookUrl: '' }));
    const result = (await bridge.transferCall(CALL_SID, '+15550001111', {
      mode: 'warm',
    })) as TransferCallResult;
    expect(result.status).toBe('transferring');
    const twiml1 = new URLSearchParams(String((fetchSpy.mock.calls[0] as [string, RequestInit])[1].body)).get('Twiml') ?? '';
    expect(twiml1).not.toContain('statusCallback');
    const body2 = new URLSearchParams(String((fetchSpy.mock.calls[1] as [string, RequestInit])[1].body));
    expect(body2.get('StatusCallback')).toBeNull();
  });

  it('warm mode rejects an invalid number without any REST call', async () => {
    const bridge = new TwilioBridge(makeConfig());
    const result = (await bridge.transferCall(CALL_SID, 'not-a-number', {
      mode: 'warm',
    })) as TransferCallResult;
    expect(result.error).toBe('Invalid phone number format');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('warm mode rejects an invalid CallSid without any REST call', async () => {
    const bridge = new TwilioBridge(makeConfig());
    const result = (await bridge.transferCall('../../evil', '+15550001111', {
      mode: 'warm',
    })) as TransferCallResult;
    expect(result.error).toContain('invalid CallSid');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('warm mode rejects a missing From number without any REST call', async () => {
    const bridge = new TwilioBridge(makeConfig({ phoneNumber: '' }));
    const result = (await bridge.transferCall(CALL_SID, '+15550001111', {
      mode: 'warm',
    })) as TransferCallResult;
    expect(result.error).toContain('no valid agent number to dial from');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('warm mode returns an envelope when the conference redirect fails', async () => {
    fetchSpy.mockResolvedValue(fakeResponse(400));
    const bridge = new TwilioBridge(makeConfig());
    const result = (await bridge.transferCall(CALL_SID, '+15550001111', {
      mode: 'warm',
    })) as TransferCallResult;
    expect(result.error).toContain('could not place caller on hold');
    // Only the failed redirect was attempted — no target dial.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('warm mode releases the parked caller when the target dial fails', async () => {
    // Redirect OK (200), dial fails (400), recovery POST (200).
    fetchSpy
      .mockResolvedValueOnce(fakeResponse(200))
      .mockResolvedValueOnce(fakeResponse(400))
      .mockResolvedValueOnce(fakeResponse(200));
    const bridge = new TwilioBridge(makeConfig());
    const result = (await bridge.transferCall(CALL_SID, '+15550001111', {
      mode: 'warm',
    })) as TransferCallResult;
    expect(result.error).toContain('could not dial the transfer target');
    expect(fetchSpy).toHaveBeenCalledTimes(3);
    const [recoveryUrl, recoveryInit] = fetchSpy.mock.calls[2] as [string, RequestInit];
    expect(recoveryUrl).toContain(`/Calls/${CALL_SID}.json`);
    expect(new URLSearchParams(String(recoveryInit.body)).get('Twiml')).toContain('<Hangup/>');
  });
});

// ---------------------------------------------------------------------------
// Telnyx / Plivo — warm unsupported (clear envelope, no network)
// ---------------------------------------------------------------------------

describe('[mocked] Telnyx / Plivo warm transfer unsupported', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(fakeResponse());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('TelnyxBridge returns the documented error envelope and never dials', async () => {
    const bridge = new TelnyxBridge(makeConfig({ telephonyProvider: 'telnyx', telnyxKey: 'key' }));
    const result = await bridge.transferCall('cc-123', '+15550001111', {
      mode: 'warm',
      summary: 's',
    });
    expect(result).toEqual({ error: 'warm transfer not yet supported on telnyx' });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('PlivoBridge returns the documented error envelope and never dials', async () => {
    const bridge = new PlivoBridge(
      makeConfig({
        telephonyProvider: 'plivo',
        plivoAuthId: 'MA' + '0'.repeat(18),
        plivoAuthToken: 'token',
      }),
    );
    const result = await bridge.transferCall('uuid-1', '+15550001111', {
      mode: 'warm',
      summary: 's',
    });
    expect(result).toEqual({ error: 'warm transfer not yet supported on plivo' });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('TelnyxBridge cold transfer still issues the historical REST call', async () => {
    const bridge = new TelnyxBridge(makeConfig({ telephonyProvider: 'telnyx', telnyxKey: 'key' }));
    await bridge.transferCall('cc-123', '+15550001111');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe('https://api.telnyx.com/v2/calls/cc-123/actions/transfer');
  });

  it('TelnyxBridge cold transfer without clientState posts a bare { to } body', async () => {
    const bridge = new TelnyxBridge(makeConfig({ telephonyProvider: 'telnyx', telnyxKey: 'key' }));
    await bridge.transferCall('cc-123', '+15550001111');
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ to: '+15550001111' });
  });

  it('TelnyxBridge cold transfer base64-encodes an opt-in clientState into the body', async () => {
    const bridge = new TelnyxBridge(makeConfig({ telephonyProvider: 'telnyx', telnyxKey: 'key' }));
    await bridge.transferCall('cc-123', '+15550001111', { clientState: 'customer_id=VIP42' });
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body)) as { to: string; client_state?: string };
    expect(body.to).toBe('+15550001111');
    expect(Buffer.from(body.client_state ?? '', 'base64').toString('utf-8')).toBe('customer_id=VIP42');
  });
});

// ---------------------------------------------------------------------------
// Pipeline built-in transfer handler — mode plumbing
// ---------------------------------------------------------------------------

describe('pipeline built-in transfer handler modes', () => {
  type Handler = (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>;

  it('cold result strings stay byte-identical', async () => {
    const captured: string[] = [];
    const tools = augmentWithBuiltinHandoffTools(null, {
      transferCall: async (n) => {
        captured.push(n);
      },
    });
    const result = await (tools[0].handler as Handler)({ number: '+14155551234' }, {});
    expect(JSON.parse(result)).toEqual({ status: 'transferring', to: '+14155551234' });
    expect(captured).toEqual(['+14155551234']);
  });

  it('warm dispatches options and wraps the carrier result', async () => {
    const seen: Array<[string, string | undefined, string | undefined]> = [];
    const tools = augmentWithBuiltinHandoffTools(null, {
      transferCall: async (n, options) => {
        seen.push([n, options?.mode, options?.summary]);
        return { status: 'transferring', mode: options?.mode, to: n };
      },
    });
    const result = JSON.parse(
      await (tools[0].handler as Handler)(
        { number: '+14155551234', mode: 'warm', summary: 'ctx' },
        {},
      ),
    );
    expect(seen).toEqual([['+14155551234', 'warm', 'ctx']]);
    expect(result).toEqual({ status: 'transferring', mode: 'warm', to: '+14155551234' });
  });

  it('warm against a void-returning bridge falls back to the generic envelope', async () => {
    const tools = augmentWithBuiltinHandoffTools(null, {
      transferCall: async () => {
        /* legacy: resolves void */
      },
    });
    const result = JSON.parse(
      await (tools[0].handler as Handler)({ number: '+14155551234', mode: 'warm' }, {}),
    );
    expect(result).toEqual({ status: 'transferring', mode: 'warm', to: '+14155551234' });
  });

  it('rejects an invalid mode without dispatching', async () => {
    const captured: string[] = [];
    const tools = augmentWithBuiltinHandoffTools(null, {
      transferCall: async (n) => {
        captured.push(n);
      },
    });
    const result = JSON.parse(
      await (tools[0].handler as Handler)({ number: '+14155551234', mode: 'hot' }, {}),
    );
    expect(result.status).toBe('rejected');
    expect(result.error).toContain('Invalid transfer mode');
    expect(captured).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Server webhook routes — conference + warm-status callbacks
// ---------------------------------------------------------------------------

describe('[mocked] warm-transfer webhook routes', () => {
  // Captured BEFORE any spy so test requests to 127.0.0.1 bypass the mock.
  const realFetch = globalThis.fetch.bind(globalThis);

  function computeTwilioSignature(
    url: string,
    params: Record<string, string>,
    authToken: string,
  ): string {
    const data =
      url +
      Object.keys(params)
        .sort()
        .reduce((acc, key) => acc + key + (params[key] ?? ''), '');
    return crypto.createHmac('sha1', authToken).update(data).digest('base64');
  }

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('conference route logs and returns 204', async () => {
    const server = new EmbeddedServer(makeConfig({ twilioToken: '' }), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      const resp = await realFetch(`http://127.0.0.1:${port}/webhooks/twilio/conference`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          StatusCallbackEvent: 'participant-join',
          FriendlyName: `patter-warm-${CALL_SID}`,
          ConferenceSid: 'CF' + '0'.repeat(32),
          CallSid: CALL_SID,
        }).toString(),
      });
      expect(resp.status).toBe(204);
    } finally {
      await server.stop();
    }
  });

  it('conference route fails closed without a token when signatures are required', async () => {
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: '', requireSignature: true }),
      makeAgent(),
    );
    const port = await getFreePort();
    await server.start(port);
    try {
      const resp = await realFetch(`http://127.0.0.1:${port}/webhooks/twilio/conference`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: '',
      });
      expect(resp.status).toBe(503);
    } finally {
      await server.stop();
    }
  });

  it('conference route rejects an invalid signature with 403', async () => {
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: 'real_token', requireSignature: true }),
      makeAgent(),
    );
    const port = await getFreePort();
    await server.start(port);
    try {
      const resp = await realFetch(`http://127.0.0.1:${port}/webhooks/twilio/conference`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Twilio-Signature': 'badsig',
        },
        body: new URLSearchParams({ CallSid: CALL_SID }).toString(),
      });
      expect(resp.status).toBe(403);
    } finally {
      await server.stop();
    }
  });

  it('warm-status failure releases the parked caller (valid signature)', async () => {
    const token = 'tok_test';
    const server = new EmbeddedServer(makeConfig({ twilioToken: token }), makeAgent());
    const port = await getFreePort();
    await server.start(port);

    // Mock ONLY the Twilio REST boundary; the local test request passes through.
    const twilioCalls: Array<[string, RequestInit | undefined]> = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('api.twilio.com')) {
        twilioCalls.push([url, init as RequestInit | undefined]);
        return fakeResponse();
      }
      return realFetch(input as RequestInfo, init);
    });

    try {
      const params = { CallSid: 'CA' + 'b'.repeat(32), CallStatus: 'no-answer' };
      const path = `/webhooks/twilio/warm-status?caller_call_sid=${CALL_SID}`;
      const signature = computeTwilioSignature(`https://example.ngrok.io${path}`, params, token);
      const resp = await realFetch(`http://127.0.0.1:${port}${path}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Twilio-Signature': signature,
        },
        body: new URLSearchParams(params).toString(),
      });
      expect(resp.status).toBe(204);
      expect(twilioCalls).toHaveLength(1);
      const [releaseUrl, releaseInit] = twilioCalls[0];
      expect(releaseUrl).toContain(`/Calls/${CALL_SID}.json`);
      const twiml = new URLSearchParams(String(releaseInit?.body)).get('Twiml') ?? '';
      expect(twiml).toContain('<Hangup/>');
    } finally {
      await server.stop();
    }
  });

  it('warm-status completed is a no-op', async () => {
    const token = 'tok_test';
    const server = new EmbeddedServer(makeConfig({ twilioToken: token }), makeAgent());
    const port = await getFreePort();
    await server.start(port);

    const twilioCalls: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('api.twilio.com')) {
        twilioCalls.push(url);
        return fakeResponse();
      }
      return realFetch(input as RequestInfo, init);
    });

    try {
      const params = { CallSid: 'CA' + 'b'.repeat(32), CallStatus: 'completed' };
      const path = `/webhooks/twilio/warm-status?caller_call_sid=${CALL_SID}`;
      const signature = computeTwilioSignature(`https://example.ngrok.io${path}`, params, token);
      const resp = await realFetch(`http://127.0.0.1:${port}${path}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Twilio-Signature': signature,
        },
        body: new URLSearchParams(params).toString(),
      });
      expect(resp.status).toBe(204);
      expect(twilioCalls).toHaveLength(0);
    } finally {
      await server.stop();
    }
  });

  it('warm-status rejects a forged caller_call_sid (path-injection guard)', async () => {
    const token = 'tok_test';
    const server = new EmbeddedServer(makeConfig({ twilioToken: token }), makeAgent());
    const port = await getFreePort();
    await server.start(port);

    const twilioCalls: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('api.twilio.com')) {
        twilioCalls.push(url);
        return fakeResponse();
      }
      return realFetch(input as RequestInfo, init);
    });

    try {
      const params = { CallSid: 'CA' + 'b'.repeat(32), CallStatus: 'failed' };
      const forged = encodeURIComponent('../../evil');
      const path = `/webhooks/twilio/warm-status?caller_call_sid=${forged}`;
      const signature = computeTwilioSignature(`https://example.ngrok.io${path}`, params, token);
      const resp = await realFetch(`http://127.0.0.1:${port}${path}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Twilio-Signature': signature,
        },
        body: new URLSearchParams(params).toString(),
      });
      expect(resp.status).toBe(204);
      expect(twilioCalls).toHaveLength(0);
    } finally {
      await server.stop();
    }
  });

  it('warm-status fails closed without a token when signatures are required', async () => {
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: '', requireSignature: true }),
      makeAgent(),
    );
    const port = await getFreePort();
    await server.start(port);
    try {
      const resp = await realFetch(`http://127.0.0.1:${port}/webhooks/twilio/warm-status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: '',
      });
      expect(resp.status).toBe(503);
    } finally {
      await server.stop();
    }
  });
});

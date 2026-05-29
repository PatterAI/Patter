import { describe, it, expect, vi, afterEach } from 'vitest';
import { PlivoBridge } from '../src/server';
import type { LocalConfig } from '../src/server';
import { Carrier as Plivo, validatePlivoSignature } from '../src/telephony/plivo';
import { PlivoAdapter } from '../src/providers/plivo-adapter';
import crypto from 'node:crypto';
import { DEFAULT_PRICING, calculateTelephonyCost } from '../src/pricing';

function makeConfig(overrides: Partial<LocalConfig> = {}): LocalConfig {
  return {
    phoneNumber: '+15550000000',
    webhookUrl: 'abc.ngrok.io',
    telephonyProvider: 'plivo',
    // gitleaks:allow — fake test placeholder (not a real Plivo Auth ID).
    plivoAuthId: 'MA-test-only',
    plivoAuthToken: 'supersecrettoken',
    ...overrides,
  };
}

/** Minimal fake ws capturing the outbound text frames. */
function makeFakeWs(): { send: (t: string) => void; sent: Record<string, unknown>[] } {
  const sent: Record<string, unknown>[] = [];
  return { send: (t: string) => sent.push(JSON.parse(t)), sent };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Carrier credential dataclass
// ---------------------------------------------------------------------------

describe('Plivo Carrier', () => {
  it('reports kind=plivo with explicit creds', () => {
    const c = new Plivo({ authId: 'MA1', authToken: 't' });
    expect(c.kind).toBe('plivo');
    expect(c.authId).toBe('MA1');
  });

  it('reads PLIVO_* env vars', () => {
    vi.stubEnv('PLIVO_AUTH_ID', 'MAENV');
    vi.stubEnv('PLIVO_AUTH_TOKEN', 'tokenv');
    const c = new Plivo();
    expect(c.authId).toBe('MAENV');
    expect(c.authToken).toBe('tokenv');
  });

  it('throws when credentials are missing', () => {
    vi.stubEnv('PLIVO_AUTH_ID', '');
    vi.stubEnv('PLIVO_AUTH_TOKEN', '');
    expect(() => new Plivo()).toThrow(/authId/);
  });
});

// ---------------------------------------------------------------------------
// PlivoAdapter.generateStreamXml
// ---------------------------------------------------------------------------

describe('PlivoAdapter.generateStreamXml', () => {
  it('puts the WSS URL as <Stream> text content (not a url= attr)', () => {
    const xml = PlivoAdapter.generateStreamXml('wss://h/ws/plivo/stream/x');
    expect(xml).toContain('>wss://h/ws/plivo/stream/x</Stream>');
    expect(xml).not.toContain('url=');
    expect(xml).toContain('bidirectional="true"');
  });

  it('escapes the query-string ampersand', () => {
    const xml = PlivoAdapter.generateStreamXml('wss://h/x?caller=%2B1&callee=%2B2');
    expect(xml).toContain('?caller=%2B1&amp;callee=%2B2');
  });

  it('honours a custom contentType and extraHeaders', () => {
    const xml = PlivoAdapter.generateStreamXml('wss://h/x', 'audio/x-l16;rate=16000', {
      'X-PH-caller': '+1',
    });
    expect(xml).toContain('contentType="audio/x-l16;rate=16000"');
    expect(xml).toContain('extraHeaders="X-PH-caller=+1"');
  });
});

// ---------------------------------------------------------------------------
// PlivoAdapter REST shape
// ---------------------------------------------------------------------------

describe('PlivoAdapter REST', () => {
  it('initiateCall posts an answer_url payload and returns request_uuid', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return { ok: true, status: 201, text: async () => JSON.stringify({ request_uuid: 'req-1' }) };
      }),
    );
    const adapter = new PlivoAdapter('MA-test-only', 'tok');
    const res = await adapter.initiateCall({
      from: '+15550001111',
      to: '+15550002222',
      answerUrl: 'https://h/webhooks/plivo/voice',
      ringTimeout: 25,
      machineDetection: true,
      machineDetectionUrl: 'https://h/webhooks/plivo/amd',
    });
    expect(res.requestUuid).toBe('req-1');
    expect(calls[0].url).toContain('/Call/');
    const body = JSON.parse(String(calls[0].init.body));
    expect(body.answer_url).toBe('https://h/webhooks/plivo/voice');
    expect(body.ring_timeout).toBe(25);
    expect(body.machine_detection).toBe('true');
    expect(body.machine_detection_url).toBe('https://h/webhooks/plivo/amd');
  });

  it('endCall treats HTTP 404 as success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 404, text: async () => '' })),
    );
    const adapter = new PlivoAdapter('MA-test-only', 'tok');
    await expect(adapter.endCall('CALLUUID')).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// PlivoBridge — wire envelopes
// ---------------------------------------------------------------------------

describe('PlivoBridge', () => {
  it('telephonyProvider is plivo', () => {
    expect(new PlivoBridge(makeConfig()).telephonyProvider).toBe('plivo');
  });

  it('sendAudio wraps base64 in a playAudio command', () => {
    const ws = makeFakeWs();
    new PlivoBridge(makeConfig()).sendAudio(ws as never, 'YWJj', 'stream1');
    expect(ws.sent[0]).toEqual({
      event: 'playAudio',
      media: { contentType: 'audio/x-mulaw', sampleRate: 8000, payload: 'YWJj' },
    });
  });

  it('sendMark emits a checkpoint with the stream id', () => {
    const ws = makeFakeWs();
    new PlivoBridge(makeConfig()).sendMark(ws as never, 'audio_1', 'stream-xyz');
    expect(ws.sent[0]).toEqual({ event: 'checkpoint', streamId: 'stream-xyz', name: 'audio_1' });
  });

  it('sendClear emits clearAudio with the stream id', () => {
    const ws = makeFakeWs();
    new PlivoBridge(makeConfig()).sendClear(ws as never, 'stream-xyz');
    expect(ws.sent[0]).toEqual({ event: 'clearAudio', streamId: 'stream-xyz' });
  });

  it('sendDtmf filters invalid digits and sends over the given ws', async () => {
    const ws = makeFakeWs();
    const bridge = new PlivoBridge(makeConfig());
    await bridge.sendDtmf(ws as never, 'callid', '12ab#xZ', 0);
    expect(ws.sent[0]).toEqual({ event: 'sendDTMF', dtmf: '12ab#' });
  });

  it('sendDtmf with no valid digits sends nothing', async () => {
    const ws = makeFakeWs();
    const bridge = new PlivoBridge(makeConfig());
    await bridge.sendDtmf(ws as never, 'callid', 'xyz', 0);
    expect(ws.sent).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// V3 signature
// ---------------------------------------------------------------------------

/** Mirror of plivo-python ``signature_v3``: HMAC-SHA256 of
 *  ``url + sorted_post_params + "." + nonce`` (POST) or ``url + "." + nonce``
 *  (GET), base64-encoded. */
function v3Sig(
  url: string,
  nonce: string,
  token: string,
  params?: Record<string, string>,
): string {
  let base = url;
  if (params) {
    const keys = Object.keys(params).sort();
    base += keys.map((k) => `${k}${params[k]}`).join('');
  }
  return crypto.createHmac('sha256', token).update(`${base}.${nonce}`).digest('base64');
}

describe('V3 signature', () => {
  const url = 'https://h/webhooks/plivo/voice';
  const token = 'tok';
  const nonce = 'n1';

  it('accepts a GET signed as url + "." + nonce', () => {
    const sig = v3Sig(url, nonce, token);
    expect(validatePlivoSignature(url, nonce, sig, token, undefined, 'GET')).toBe(true);
  });

  it('accepts a POST signed as url + sorted(key+value) + "." + nonce', () => {
    const params = { CallUUID: 'CU1', From: '+15551112222', To: '+15553334444' };
    const sig = v3Sig(url, nonce, token, params);
    expect(validatePlivoSignature(url, nonce, sig, token, params, 'POST')).toBe(true);
  });

  it('rejects a tampered POST param', () => {
    const original = { CallUUID: 'CU1', From: '+1' };
    const sig = v3Sig(url, nonce, token, original);
    const tampered = { CallUUID: 'CU1', From: '+9' };
    expect(validatePlivoSignature(url, nonce, sig, token, tampered, 'POST')).toBe(false);
  });

  it('supports comma-separated signatures for key rotation', () => {
    const sig = v3Sig(url, nonce, token);
    expect(
      validatePlivoSignature(url, nonce, `oldsig, ${sig}`, token, undefined, 'GET'),
    ).toBe(true);
  });

  it('returns false when any input is missing', () => {
    expect(validatePlivoSignature('u', '', 'sig', 'tok')).toBe(false);
    expect(validatePlivoSignature('u', 'n', '', 'tok')).toBe(false);
    expect(validatePlivoSignature('u', 'n', 'sig', '')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Pricing
// ---------------------------------------------------------------------------

describe('Plivo pricing', () => {
  it('is present in DEFAULT_PRICING at the official inbound rate with roundUp', () => {
    expect(DEFAULT_PRICING.plivo).toEqual({ unit: 'minute', price: 0.0055, roundUp: true });
  });

  it('rounds partial minutes up like Twilio', () => {
    // 61 s → 2 minutes → 2 * 0.0055
    expect(calculateTelephonyCost('plivo', 61, DEFAULT_PRICING)).toBeCloseTo(0.011, 6);
  });
});

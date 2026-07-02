/**
 * Media-stream authentication tests (GitHub issue #204).
 *
 * Verifies that the per-call stream-auth token minted by the
 * signature-validated carrier webhook / outbound dial is REQUIRED by the media
 * WebSocket before any provider (STT/LLM/TTS) session opens, for all three
 * carriers (Twilio <Parameter>, Telnyx query string, Plivo extra_headers), plus
 * the fail-closed default, the opt-out escape hatch, and the guarantee that the
 * token never appears in logs.
 *
 * Only the WS transport is mocked — the token is minted through the real
 * webhook / stream-URL code path on a real EmbeddedServer instance, then
 * presented (or withheld) on a simulated 'start' frame. The billable provider
 * session (`StreamHandler.handleCallStart`) is spied so "no session opened" is
 * asserted directly.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import net from 'node:net';
import type { AddressInfo } from 'node:net';

import { EmbeddedServer } from '../../src/server';
import type { LocalConfig } from '../../src/server';
import type { AgentOptions } from '../../src/types';
import { StreamHandler } from '../../src/stream-handler';
import {
  StreamTokenStore,
  generateStreamToken,
  constantTimeEquals,
  STREAM_TOKEN_TTL_MS,
} from '../../src/stream-auth';
import { getLogger, setLogger } from '../../src/logger';
import type { Logger } from '../../src/logger';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

function makeConfig(overrides: Partial<LocalConfig> = {}): LocalConfig {
  return {
    twilioSid: 'AC_test',
    twilioToken: 'tok_test',
    openaiKey: 'sk_test',
    phoneNumber: '+15550000000',
    webhookUrl: 'abc.ngrok.io',
    telephonyProvider: 'twilio',
    requireSignature: false,
    ...overrides,
  };
}

function makeAgent(overrides: Partial<AgentOptions> = {}): AgentOptions {
  return {
    systemPrompt: 'You are helpful.',
    voice: 'alloy',
    model: 'gpt-4o-mini-realtime-preview',
    language: 'en',
    ...overrides,
  } as AgentOptions;
}

/** Twilio config with NO auth token so /webhooks/twilio/voice skips signature
 *  validation (requireSignature=false) and returns TwiML we can mint from. */
function twilioConfig(overrides: Partial<LocalConfig> = {}): LocalConfig {
  return makeConfig({ twilioToken: undefined, ...overrides });
}

/** EventEmitter-backed mock WS with the socket methods the handlers call. */
class MockWs extends EventEmitter {
  readyState = 1;
  send = vi.fn();
  close = vi.fn();
  terminate = vi.fn();
  ping = vi.fn();
}

/** Await the per-connection async FIFO used by the WS handlers. */
const flush = (): Promise<void> => new Promise((r) => setTimeout(r, 25));

const VALID_SID = 'CA' + 'abcdef0123456789abcdef0123456789';

/** Install a capturing logger; returns the recorded lines and a restore fn. */
function captureLogs(): { lines: string[]; restore: () => void } {
  const lines: string[] = [];
  const record = (msg: string, ...args: unknown[]): void => {
    lines.push([msg, ...args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a)))].join(' '));
  };
  const prev = getLogger();
  const capturing: Logger = { info: record, warn: record, error: record, debug: record };
  setLogger(capturing);
  return { lines, restore: () => setLogger(prev) };
}

// ---------------------------------------------------------------------------
// StreamTokenStore (pure unit)
// ---------------------------------------------------------------------------

describe('StreamTokenStore', () => {
  it('mints a high-entropy url-safe token that validates for its key', () => {
    const store = new StreamTokenStore();
    const token = store.mint('call-1');
    expect(token).toMatch(/^[A-Za-z0-9_-]+$/); // base64url, no padding
    expect(token.length).toBeGreaterThanOrEqual(43);
    expect(store.validate('call-1', token)).toBe(true);
  });

  it('rejects a wrong token, an unknown key, and a missing token', () => {
    const store = new StreamTokenStore();
    const token = store.mint('call-1');
    expect(store.validate('call-1', token + 'x')).toBe(false);
    expect(store.validate('call-1', 'totally-different')).toBe(false);
    expect(store.validate('other-key', token)).toBe(false);
    expect(store.validate('call-1', undefined)).toBe(false);
    expect(store.validate('call-1', '')).toBe(false);
  });

  it('rejects an expired token and prunes it', () => {
    const store = new StreamTokenStore(1_000);
    const t0 = 10_000;
    const token = store.mint('call-1', t0);
    expect(store.validate('call-1', token, t0 + 999)).toBe(true);
    // At/after expiry the token is rejected and the entry is dropped.
    expect(store.validate('call-1', token, t0 + 1_000)).toBe(false);
    expect(store.size).toBe(0);
  });

  it('keeps the token valid within the TTL (carrier reconnect allowed)', () => {
    const store = new StreamTokenStore();
    const token = store.mint('call-1');
    expect(store.validate('call-1', token)).toBe(true);
    // Second presentation inside the window still succeeds (not single-use).
    expect(store.validate('call-1', token)).toBe(true);
  });

  it('register() stores an externally generated token (outbound path)', () => {
    const store = new StreamTokenStore();
    const token = generateStreamToken();
    store.register('CA123', token);
    expect(store.validate('CA123', token)).toBe(true);
  });

  it('prune() drops only expired entries', () => {
    const store = new StreamTokenStore(1_000);
    const t0 = 5_000;
    store.register('a', generateStreamToken(), t0);
    store.register('b', generateStreamToken(), t0 + 900);
    store.prune(t0 + 1_001);
    expect(store.validate('a', 'x', t0 + 1_001)).toBe(false);
    expect(store.size).toBe(1); // 'b' survives
  });

  it('constantTimeEquals is length-safe and value-correct', () => {
    expect(constantTimeEquals('abc', 'abc')).toBe(true);
    expect(constantTimeEquals('abc', 'abd')).toBe(false);
    expect(constantTimeEquals('abc', 'abcd')).toBe(false); // unequal length
    expect(STREAM_TOKEN_TTL_MS).toBe(120_000);
  });
});

// ---------------------------------------------------------------------------
// Twilio media WS — token via start.customParameters
// ---------------------------------------------------------------------------

describe('Twilio media-stream auth (#204)', () => {
  let startSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    startSpy = vi.spyOn(StreamHandler.prototype, 'handleCallStart').mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** Mint a token through the real /webhooks/twilio/voice TwiML path. */
  async function mintViaWebhook(server: EmbeddedServer, port: number, callSid: string): Promise<string> {
    const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/voice`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ CallSid: callSid, From: '+15551111111', To: '+15552222222' }).toString(),
    });
    const twiml = await resp.text();
    const m = twiml.match(/name="patter_stream_token" value="([^"]+)"/);
    expect(m, 'TwiML should carry a patter_stream_token <Parameter>').toBeTruthy();
    return m![1];
  }

  function startFrame(callSid: string, token?: string): string {
    const customParameters: Record<string, string> = { caller: '', callee: '' };
    if (token !== undefined) customParameters.patter_stream_token = token;
    return JSON.stringify({ event: 'start', streamSid: 'MZ_test', start: { callSid, customParameters } });
  }

  it('accepts a valid minted token and starts the session', async () => {
    const server = new EmbeddedServer(twilioConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      const token = await mintViaWebhook(server, port, VALID_SID);
      const ws = new MockWs();
      (server as any).handleTwilioStream(ws, new URL('http://localhost/ws/stream/outbound'));
      ws.emit('message', Buffer.from(startFrame(VALID_SID, token)));
      await flush();
      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(startSpy).toHaveBeenCalledWith(VALID_SID, expect.not.objectContaining({ patter_stream_token: token }));
      expect(ws.close).not.toHaveBeenCalledWith(1008, expect.anything());
    } finally {
      await server.stop();
    }
  });

  it('rejects a missing token: closes 1008, opens no session', async () => {
    const server = new EmbeddedServer(makeConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      const ws = new MockWs();
      (server as any).handleTwilioStream(ws, new URL('http://localhost/ws/stream/outbound'));
      ws.emit('message', Buffer.from(startFrame(VALID_SID))); // no token
      await flush();
      expect(startSpy).not.toHaveBeenCalled();
      expect(ws.close).toHaveBeenCalledWith(1008, expect.any(String));
    } finally {
      await server.stop();
    }
  });

  it('rejects a wrong token even for a minted call', async () => {
    const server = new EmbeddedServer(twilioConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      await mintViaWebhook(server, port, VALID_SID); // mint the real token, then present a forgery
      const ws = new MockWs();
      (server as any).handleTwilioStream(ws, new URL('http://localhost/ws/stream/outbound'));
      ws.emit('message', Buffer.from(startFrame(VALID_SID, 'forged-token-value')));
      await flush();
      expect(startSpy).not.toHaveBeenCalled();
      expect(ws.close).toHaveBeenCalledWith(1008, expect.any(String));
    } finally {
      await server.stop();
    }
  });
});

// ---------------------------------------------------------------------------
// Telnyx media WS — token via query string, validated at accept
// ---------------------------------------------------------------------------

describe('Telnyx media-stream auth (#204)', () => {
  let startSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    startSpy = vi.spyOn(StreamHandler.prototype, 'handleCallStart').mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const CTRL = 'ctrl-accept-1';

  /** Drive the real /webhooks/telnyx/voice call.initiated path and capture the
   *  stream_url (with the minted token) sent to the Telnyx API. */
  async function mintViaWebhook(server: EmbeddedServer, port: number): Promise<URL> {
    const originalFetch = globalThis.fetch;
    let captured = '';
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: string | URL | Request, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        if (url.includes('api.telnyx.com')) {
          const body = JSON.parse(String(init?.body ?? '{}')) as { stream_url?: string };
          if (body.stream_url) captured = body.stream_url;
          return { ok: true, status: 200, json: async () => ({ data: {} }), text: async () => '' } as Response;
        }
        return originalFetch(input, init);
      },
    );
    try {
      const rawBody = JSON.stringify({
        data: { event_type: 'call.initiated', payload: { call_control_id: CTRL, from: '+15551111111', to: '+15552222222' } },
      });
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: rawBody,
      });
      expect(resp.status).toBe(200);
    } finally {
      spy.mockRestore();
    }
    expect(captured, 'stream_url should have been sent to Telnyx').toContain('patter_stream_token=');
    const su = new URL(captured);
    return new URL(su.pathname + su.search, 'http://localhost');
  }

  function telnyxConfig(): LocalConfig {
    return makeConfig({ telephonyProvider: 'telnyx', telnyxKey: 'KEY_test', telnyxConnectionId: 'conn_test', twilioSid: undefined, twilioToken: undefined });
  }

  it('accepts a valid minted token and starts the session', async () => {
    const server = new EmbeddedServer(telnyxConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      const wsUrl = await mintViaWebhook(server, port);
      const ws = new MockWs();
      (server as any).handleTelnyxStream(ws, wsUrl);
      expect(ws.close).not.toHaveBeenCalledWith(1008, expect.anything());
      ws.emit('message', Buffer.from(JSON.stringify({ event: 'start', start: { call_control_id: CTRL } })));
      await flush();
      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(startSpy).toHaveBeenCalledWith(CTRL);
    } finally {
      await server.stop();
    }
  });

  it('rejects a missing token at accept: closes 1008, opens no session', async () => {
    const server = new EmbeddedServer(telnyxConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      const ws = new MockWs();
      // No patter_stream_token in the query → rejected before any frame.
      (server as any).handleTelnyxStream(ws, new URL(`http://localhost/ws/stream/${CTRL}?caller=&callee=`));
      expect(ws.close).toHaveBeenCalledWith(1008, expect.any(String));
      ws.emit('message', Buffer.from(JSON.stringify({ event: 'start', start: { call_control_id: CTRL } })));
      await flush();
      expect(startSpy).not.toHaveBeenCalled();
    } finally {
      await server.stop();
    }
  });

  it('rejects a wrong token at accept', async () => {
    const server = new EmbeddedServer(telnyxConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      await mintViaWebhook(server, port); // mint the real token for CTRL
      const ws = new MockWs();
      (server as any).handleTelnyxStream(ws, new URL(`http://localhost/ws/stream/${CTRL}?patter_stream_token=forged`));
      expect(ws.close).toHaveBeenCalledWith(1008, expect.any(String));
      expect(startSpy).not.toHaveBeenCalled();
    } finally {
      await server.stop();
    }
  });
});

// ---------------------------------------------------------------------------
// Plivo media WS — token via extra_headers on the start frame
// ---------------------------------------------------------------------------

describe('Plivo media-stream auth (#204)', () => {
  let startSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    startSpy = vi.spyOn(StreamHandler.prototype, 'handleCallStart').mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const CALL_UUID = 'plivo-uuid-1';

  function plivoConfig(): LocalConfig {
    // No plivoAuthToken so /webhooks/plivo/voice skips V3 signature validation
    // (requireSignature=false) and returns the <Stream> XML we mint from.
    return makeConfig({ telephonyProvider: 'plivo', plivoAuthId: 'MA_test', plivoAuthToken: undefined, twilioSid: undefined, twilioToken: undefined });
  }

  /** Mint via the real /webhooks/plivo/voice XML path; return the token from
   *  the delivered extraHeaders. */
  async function mintViaWebhook(server: EmbeddedServer, port: number): Promise<string> {
    const resp = await fetch(`http://127.0.0.1:${port}/webhooks/plivo/voice`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ CallUUID: CALL_UUID, From: '+15551111111', To: '+15552222222' }).toString(),
    });
    const xml = await resp.text();
    const m = xml.match(/X-Patter-Stream-Token=([A-Za-z0-9_-]+)/);
    expect(m, 'stream XML should carry X-Patter-Stream-Token in extraHeaders').toBeTruthy();
    return m![1];
  }

  function startFrame(token?: string): string {
    const headers: Record<string, string> = { 'X-PH-caller': '+15551111111', 'X-PH-callee': '+15552222222' };
    if (token !== undefined) headers['X-Patter-Stream-Token'] = token;
    return JSON.stringify({ event: 'start', start: { callId: CALL_UUID, streamId: 'stream-1' }, extra_headers: JSON.stringify(headers) });
  }

  it('accepts a valid minted token and starts the session', async () => {
    const server = new EmbeddedServer(plivoConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      const token = await mintViaWebhook(server, port);
      const ws = new MockWs();
      (server as any).handlePlivoStream(ws, new URL('http://localhost/ws/plivo/stream/x?caller=&callee='));
      ws.emit('message', Buffer.from(startFrame(token)));
      await flush();
      expect(startSpy).toHaveBeenCalledTimes(1);
      // Token stripped from the custom params handed to the session.
      const params = startSpy.mock.calls[0][1] as Record<string, string>;
      expect(params).not.toHaveProperty('X-Patter-Stream-Token');
      expect(ws.close).not.toHaveBeenCalledWith(1008, expect.anything());
    } finally {
      await server.stop();
    }
  });

  it('rejects a missing token: closes 1008, opens no session', async () => {
    const server = new EmbeddedServer(plivoConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      const ws = new MockWs();
      (server as any).handlePlivoStream(ws, new URL('http://localhost/ws/plivo/stream/x?caller=&callee='));
      ws.emit('message', Buffer.from(startFrame())); // no token
      await flush();
      expect(startSpy).not.toHaveBeenCalled();
      expect(ws.close).toHaveBeenCalledWith(1008, expect.any(String));
    } finally {
      await server.stop();
    }
  });

  it('rejects a wrong token even for a minted call', async () => {
    const server = new EmbeddedServer(plivoConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    try {
      await mintViaWebhook(server, port);
      const ws = new MockWs();
      (server as any).handlePlivoStream(ws, new URL('http://localhost/ws/plivo/stream/x?caller=&callee='));
      ws.emit('message', Buffer.from(startFrame('forged-token')));
      await flush();
      expect(startSpy).not.toHaveBeenCalled();
      expect(ws.close).toHaveBeenCalledWith(1008, expect.any(String));
    } finally {
      await server.stop();
    }
  });
});

// ---------------------------------------------------------------------------
// Opt-out (requireStreamAuth=false) + never-log-token
// ---------------------------------------------------------------------------

describe('stream auth opt-out and log hygiene (#204)', () => {
  let startSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    startSpy = vi.spyOn(StreamHandler.prototype, 'handleCallStart').mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('requireStreamAuth=false allows an unauthenticated connect but warns loudly (once)', async () => {
    const server = new EmbeddedServer(makeConfig({ requireStreamAuth: false }), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    const cap = captureLogs();
    try {
      const ws = new MockWs();
      (server as any).handleTwilioStream(ws, new URL('http://localhost/ws/stream/outbound'));
      ws.emit('message', Buffer.from(JSON.stringify({ event: 'start', streamSid: 'MZ', start: { callSid: VALID_SID, customParameters: {} } })));
      await flush();
      // No token, yet the session is allowed to start.
      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(ws.close).not.toHaveBeenCalledWith(1008, expect.anything());
      const warned = cap.lines.filter((l) => /requireStreamAuth=false|Stream authentication is DISABLED/i.test(l));
      expect(warned.length).toBe(1); // one-time warning
    } finally {
      cap.restore();
      await server.stop();
    }
  });

  it('never writes the token value to logs (accept and reject paths)', async () => {
    const server = new EmbeddedServer(twilioConfig(), makeAgent());
    const port = await getFreePort();
    await server.start(port);
    // Mint outside the capture window (the webhook TwiML response is not a log).
    const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/voice`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ CallSid: VALID_SID, From: '+1', To: '+2' }).toString(),
    });
    const token = (await resp.text()).match(/name="patter_stream_token" value="([^"]+)"/)![1];
    const cap = captureLogs();
    try {
      // Accept path.
      const wsOk = new MockWs();
      (server as any).handleTwilioStream(wsOk, new URL('http://localhost/ws/stream/outbound'));
      wsOk.emit('message', Buffer.from(JSON.stringify({ event: 'start', streamSid: 'MZ', start: { callSid: VALID_SID, customParameters: { patter_stream_token: token } } })));
      await flush();
      // Reject path.
      const wsBad = new MockWs();
      (server as any).handleTwilioStream(wsBad, new URL('http://localhost/ws/stream/outbound'));
      wsBad.emit('message', Buffer.from(JSON.stringify({ event: 'start', streamSid: 'MZ', start: { callSid: VALID_SID, customParameters: { patter_stream_token: 'forged' } } })));
      await flush();
      expect(startSpy).toHaveBeenCalledTimes(1); // accept started, reject did not
      for (const line of cap.lines) {
        expect(line).not.toContain(token);
      }
    } finally {
      cap.restore();
      await server.stop();
    }
  });
});

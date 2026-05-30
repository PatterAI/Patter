/**
 * Tests for EmbeddedServer route handlers via supertest-style mocked
 * req/res objects. Also tests Twilio/Telnyx signature validation,
 * webhook routing, and EmbeddedServer.start() webhookUrl validation.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import crypto from 'node:crypto';
import net from 'node:net';
import type { AddressInfo } from 'node:net';
import { EmbeddedServer } from '../../src/server';
import type { LocalConfig } from '../../src/server';
import type { AgentOptions } from '../../src/types';

// ---------------------------------------------------------------------------
// Free-port helper — binds to port 0, reads OS-assigned port, then releases.
// This eliminates EADDRINUSE collisions between concurrently running tests.
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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
  };
}

describe('EmbeddedServer', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --- Constructor ---

  describe('constructor', () => {
    it('initializes with default values', () => {
      const server = new EmbeddedServer(makeConfig(), makeAgent());
      expect(server).toBeDefined();
    });

    it('accepts all optional callbacks', () => {
      const server = new EmbeddedServer(
        makeConfig(),
        makeAgent(),
        async () => {}, // onCallStart
        async () => {}, // onCallEnd
        async () => {}, // onTranscript
        async () => 'response', // onMessage
        true, // recording
        'Leave a message', // voicemailMessage
        async () => {}, // onMetrics
        { twilio: { unit: 'minute', price: 0.02 } }, // pricing
        true, // dashboard
        'my-token', // dashboardToken
      );
      expect(server).toBeDefined();
    });

    it('accepts telnyx config', () => {
      const server = new EmbeddedServer(
        makeConfig({
          telephonyProvider: 'telnyx',
          telnyxKey: 'KEY_test',
          telnyxConnectionId: 'conn_123',
          telnyxPublicKey: 'pubkey_base64',
        }),
        makeAgent(),
      );
      expect(server).toBeDefined();
    });
  });

  // --- start() webhookUrl validation ---

  describe('start() webhookUrl validation', () => {
    it('throws for webhookUrl with protocol prefix', async () => {
      const server = new EmbeddedServer(
        makeConfig({ webhookUrl: 'https://abc.ngrok.io' }),
        makeAgent(),
      );
      await expect(server.start(9999)).rejects.toThrow('Invalid webhookUrl');
    });

    it('throws for webhookUrl with path', async () => {
      const server = new EmbeddedServer(
        makeConfig({ webhookUrl: 'abc.ngrok.io/path' }),
        makeAgent(),
      );
      await expect(server.start(9999)).rejects.toThrow('Invalid webhookUrl');
    });

    it('throws for webhookUrl starting with special char', async () => {
      const server = new EmbeddedServer(
        makeConfig({ webhookUrl: '-abc.ngrok.io' }),
        makeAgent(),
      );
      await expect(server.start(9999)).rejects.toThrow('Invalid webhookUrl');
    });
  });

  // --- stop() ---

  describe('stop()', () => {
    it('resolves immediately when server is not started', async () => {
      const server = new EmbeddedServer(makeConfig(), makeAgent());
      await expect(server.stop()).resolves.toBeUndefined();
    });
  });

  // --- voicemailMessage setter ---

  describe('voicemailMessage', () => {
    it('can be set after construction', () => {
      const server = new EmbeddedServer(makeConfig(), makeAgent());
      server.voicemailMessage = 'New voicemail message';
      expect(server.voicemailMessage).toBe('New voicemail message');
    });
  });
});

// ---------------------------------------------------------------------------
// Twilio HMAC-SHA1 signature validation
// validateTwilioSignature is not exported, so we test it via the live route:
// a correctly-signed request must be accepted (200/TwiML), a tampered one
// must be rejected (403). This exercises the real production function end-to-end.
// ---------------------------------------------------------------------------

describe('Twilio HMAC-SHA1 signature validation', () => {
  // Helper: computes the Twilio HMAC-SHA1 signature the same way the
  // production server does — used only to build a correctly signed test request.
  function computeTwilioSignature(
    url: string,
    params: Record<string, string>,
    authToken: string,
  ): string {
    const data = url + Object.keys(params).sort().reduce((acc, key) => acc + key + (params[key] ?? ''), '');
    return crypto.createHmac('sha1', authToken).update(data).digest('base64');
  }

  it('accepts a correctly-signed Twilio webhook (real production path)', async () => {
    const authToken = 'tok_integration_test';
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: authToken }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const validSid = 'CA' + 'abcdef0123456789abcdef0123456789';
      const params: Record<string, string> = {
        CallSid: validSid,
        From: '+15551111111',
        To: '+15552222222',
      };
      const sigUrl = `https://abc.ngrok.io/webhooks/twilio/voice`;
      const validSig = computeTwilioSignature(sigUrl, params, authToken);

      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/voice`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Twilio-Signature': validSig,
        },
        body: new URLSearchParams(params).toString(),
      });

      // A valid signature must not be rejected — the server returns TwiML (200).
      expect(resp.ok).toBe(true);
      const text = await resp.text();
      expect(text).toContain('<Response>');
    } finally {
      await server.stop();
    }
  });

  it('rejects a Twilio webhook with a tampered signature (403)', async () => {
    const authToken = 'tok_integration_test';
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: authToken }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const validSid = 'CA' + 'abcdef0123456789abcdef0123456789';
      const params: Record<string, string> = {
        CallSid: validSid,
        From: '+15551111111',
        To: '+15552222222',
      };
      // Use a DIFFERENT token to produce a wrong signature.
      const wrongSig = computeTwilioSignature(
        `https://abc.ngrok.io/webhooks/twilio/voice`,
        params,
        'wrong_token',
      );

      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/voice`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Twilio-Signature': wrongSig,
        },
        body: new URLSearchParams(params).toString(),
      });

      expect(resp.status).toBe(403);
    } finally {
      await server.stop();
    }
  });

  it('sorts parameters by key (signature invariant across key-insertion order)', () => {
    // Pure determinism check — same params, different insertion order → same sig.
    const url = 'https://abc.ngrok.io/webhooks/twilio/voice';
    const authToken = 'tok';

    const sig1 = computeTwilioSignature(url, { Z: '1', A: '2' }, authToken);
    const sig2 = computeTwilioSignature(url, { A: '2', Z: '1' }, authToken);
    expect(sig1).toBe(sig2);

    // And a different token produces a different signature (not trivially equal).
    const sig3 = computeTwilioSignature(url, { Z: '1', A: '2' }, 'other_tok');
    expect(sig1).not.toBe(sig3);
  });
});

// ---------------------------------------------------------------------------
// Telnyx Ed25519 signature validation
// validateTelnyxSignature is not exported, so we test it via the live
// /webhooks/telnyx/voice route with a real Ed25519 key pair. This verifies the
// production crypto and anti-replay logic without mock-on-mock patterns.
// ---------------------------------------------------------------------------

describe('Telnyx Ed25519 signature validation', () => {
  // Generate a real Ed25519 key pair once for the whole describe block.
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ed25519');
  // Export the public key as DER SPKI (base64) — the format the server expects.
  const pubKeyBase64 = publicKey.export({ type: 'spki', format: 'der' }).toString('base64');

  /** Sign `timestamp|rawBody` with the private key and return base64 signature. */
  function signTelnyxPayload(timestamp: string, rawBody: string): string {
    const payload = `${timestamp}|${rawBody}`;
    return crypto.sign(null, Buffer.from(payload), privateKey).toString('base64');
  }

  it('accepts a valid Ed25519-signed Telnyx webhook (real production path)', async () => {
    const server = new EmbeddedServer(
      makeConfig({
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_test',
        telnyxConnectionId: 'conn_test',
        telnyxPublicKey: pubKeyBase64,
      }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      // Intercept outbound Telnyx API calls so they don't fail on missing credentials.
      const originalFetch = globalThis.fetch;
      const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(
        async (input: string | URL | Request, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
          if (url.includes('api.telnyx.com')) {
            return { ok: true, status: 200, json: async () => ({ data: {} }), text: async () => '' } as Response;
          }
          return originalFetch(input, init);
        },
      );

      try {
        const rawBody = JSON.stringify({
          data: {
            event_type: 'call.initiated',
            payload: { call_control_id: 'ctrl-sig-test', from: '+15551111111', to: '+15552222222' },
          },
        });
        // Telnyx sends timestamp in seconds.
        const timestamp = String(Math.floor(Date.now() / 1000));
        const signature = signTelnyxPayload(timestamp, rawBody);

        const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'telnyx-signature-ed25519': signature,
            'telnyx-timestamp': timestamp,
          },
          body: rawBody,
        });

        // Valid signature → the server processes the event (200).
        expect(resp.status).toBe(200);
      } finally {
        spy.mockRestore();
      }
    } finally {
      await server.stop();
    }
  });

  it('rejects a Telnyx webhook signed with the wrong key (403)', async () => {
    const server = new EmbeddedServer(
      makeConfig({
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_test',
        telnyxConnectionId: 'conn_test',
        telnyxPublicKey: pubKeyBase64,
      }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const rawBody = JSON.stringify({ data: { event_type: 'call.initiated', payload: {} } });
      const timestamp = String(Math.floor(Date.now() / 1000));
      // Sign with a DIFFERENT key — the server must reject this.
      const { privateKey: wrongKey } = crypto.generateKeyPairSync('ed25519');
      const badSig = crypto.sign(null, Buffer.from(`${timestamp}|${rawBody}`), wrongKey).toString('base64');

      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'telnyx-signature-ed25519': badSig,
          'telnyx-timestamp': timestamp,
        },
        body: rawBody,
      });

      expect(resp.status).toBe(403);
    } finally {
      await server.stop();
    }
  });

  it('rejects an expired Telnyx timestamp (anti-replay: >300s old)', async () => {
    const server = new EmbeddedServer(
      makeConfig({
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_test',
        telnyxConnectionId: 'conn_test',
        telnyxPublicKey: pubKeyBase64,
      }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const rawBody = JSON.stringify({ data: { event_type: 'call.initiated', payload: {} } });
      // Timestamp 400 seconds in the past — outside the 300s tolerance window.
      const staleTimestamp = String(Math.floor(Date.now() / 1000) - 400);
      const signature = signTelnyxPayload(staleTimestamp, rawBody);

      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'telnyx-signature-ed25519': signature,
          'telnyx-timestamp': staleTimestamp,
        },
        body: rawBody,
      });

      expect(resp.status).toBe(403);
    } finally {
      await server.stop();
    }
  });

  it('rejects a Telnyx webhook with a non-numeric timestamp', async () => {
    const server = new EmbeddedServer(
      makeConfig({
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_test',
        telnyxConnectionId: 'conn_test',
        telnyxPublicKey: pubKeyBase64,
      }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const rawBody = JSON.stringify({ data: { event_type: 'call.initiated', payload: {} } });
      const badTimestamp = 'not-a-number';
      const signature = signTelnyxPayload(badTimestamp, rawBody);

      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'telnyx-signature-ed25519': signature,
          'telnyx-timestamp': badTimestamp,
        },
        body: rawBody,
      });

      expect(resp.status).toBe(403);
    } finally {
      await server.stop();
    }
  });
});

// ---------------------------------------------------------------------------
// Route handler behavior (tested via EmbeddedServer start/stop lifecycle)
// ---------------------------------------------------------------------------

describe('EmbeddedServer route behavior', () => {
  it('can be started and stopped', async () => {
    const server = new EmbeddedServer(makeConfig(), makeAgent(), undefined, undefined, undefined, undefined, false, '', undefined, undefined, false);

    const port = await getFreePort();

    await server.start(port);

    // Server should be running now — verify by making a health check
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/health`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as Record<string, unknown>;
      expect(body.status).toBe('ok');
      expect(body.mode).toBe('local');
    } finally {
      await server.stop();
    }
  });

  it('serves /webhooks/twilio/voice and returns TwiML', async () => {
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: '' }), // no token = skip sig validation
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const validSid = 'CA' + 'abcdef0123456789abcdef0123456789';
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/voice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          CallSid: validSid,
          From: '+15551111111',
          To: '+15552222222',
        }).toString(),
      });

      expect(resp.ok).toBe(true);
      const text = await resp.text();
      expect(text).toContain('<?xml');
      expect(text).toContain('<Response>');
      expect(text).toContain('<Connect>');
      expect(text).toContain('<Stream');
      expect(text).toContain(`wss://abc.ngrok.io/ws/stream/${validSid}`);
    } finally {
      await server.stop();
    }
  });

  it('rejects twilio voice webhook with invalid signature', async () => {
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: 'real_token' }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/voice`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Twilio-Signature': 'invalid_signature',
        },
        body: new URLSearchParams({ CallSid: 'CA_test', From: '+1', To: '+2' }).toString(),
      });

      expect(resp.status).toBe(403);
    } finally {
      await server.stop();
    }
  });

  it('serves /webhooks/twilio/recording and returns 204', async () => {
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: '' }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/recording`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          RecordingSid: 'RE_123',
          RecordingUrl: 'https://api.twilio.com/recordings/RE_123',
          CallSid: 'CA_test',
        }).toString(),
      });

      expect(resp.status).toBe(204);
    } finally {
      await server.stop();
    }
  });

  it('serves /webhooks/twilio/amd and returns 204', async () => {
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: '' }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/amd`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          AnsweredBy: 'human',
          CallSid: 'CA_amd_test',
        }).toString(),
      });

      expect(resp.status).toBe(204);
    } finally {
      await server.stop();
    }
  });

  it('serves /webhooks/telnyx/voice for call.initiated with inline streaming params', async () => {
    // BUG #16 — Telnyx Call Control is REST-first: the webhook body is an
    // informational notification, so the SDK must POST ``actions/answer`` to
    // Telnyx and respond with 200 + empty body.
    //
    // PERF — the streaming params live INSIDE the answer body so Telnyx
    // auto-starts the stream when the leg picks up. This removes the
    // ``call.answered`` round-trip + a second POST (~100-200 ms).
    const originalFetch = globalThis.fetch;
    const telnyxFetchCalls: Array<[string | URL | Request, RequestInit | undefined]> = [];
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: string | URL | Request, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        if (url.includes('api.telnyx.com')) {
          telnyxFetchCalls.push([input, init]);
          return {
            ok: true,
            status: 200,
            json: async () => ({ data: {} }),
            text: async () => '',
          } as Response;
        }
        return originalFetch(input, init);
      },
    );

    try {
      const server = new EmbeddedServer(
        makeConfig({
          telephonyProvider: 'telnyx',
          telnyxKey: 'KEY_test',
          telnyxConnectionId: 'conn_test',
          // No telnyxPublicKey = skip sig validation
        }),
        makeAgent(),
        undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
      );

      const port = await getFreePort();
      await server.start(port);

      try {
        const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            data: {
              event_type: 'call.initiated',
              payload: {
                call_control_id: 'ctrl-123',
                from: '+15551111111',
                to: '+15552222222',
              },
            },
          }),
        });

        expect(resp.status).toBe(200);

        const answerCall = telnyxFetchCalls.find(([url]) =>
          typeof url === 'string' && url.includes('/calls/ctrl-123/actions/answer'),
        );
        expect(answerCall).toBeDefined();
        // Validate the answer payload now carries the streaming params
        // inline (PCMU bidirectional, inbound-only track).
        const body = JSON.parse((answerCall?.[1]?.body as string) ?? '{}') as Record<string, unknown>;
        expect(body.stream_url).toContain('ctrl-123');
        expect(body.stream_track).toBe('inbound_track');
        expect(body.stream_bidirectional_codec).toBe('PCMU');
      } finally {
        await server.stop();
      }
    } finally {
      spy.mockRestore();
    }
  });

  it('does not POST a second action when telnyx call.answered arrives', async () => {
    // The streaming params are folded into ``actions/answer`` at
    // ``call.initiated`` time, so ``call.answered`` is now a no-op
    // acknowledgement — no redundant POSTs to Telnyx.
    const originalFetch = globalThis.fetch;
    const telnyxFetchCalls: Array<[string | URL | Request, RequestInit | undefined]> = [];
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: string | URL | Request, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        if (url.includes('api.telnyx.com')) {
          telnyxFetchCalls.push([input, init]);
          return {
            ok: true,
            status: 200,
            json: async () => ({ data: {} }),
            text: async () => '',
          } as Response;
        }
        return originalFetch(input, init);
      },
    );

    try {
      const server = new EmbeddedServer(
        makeConfig({
          telephonyProvider: 'telnyx',
          telnyxKey: 'KEY_test',
          telnyxConnectionId: 'conn_test',
        }),
        makeAgent(),
        undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
      );

      const port = await getFreePort();
      await server.start(port);

      try {
        const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            data: {
              event_type: 'call.answered',
              payload: {
                call_control_id: 'ctrl-456',
                from: '+15551111111',
                to: '+15552222222',
              },
            },
          }),
        });

        expect(resp.status).toBe(200);

        // No streaming_start POST should be made — the answer body already
        // carried the params.
        const startCall = telnyxFetchCalls.find(([url]) =>
          typeof url === 'string' && url.includes('/streaming_start'),
        );
        expect(startCall).toBeUndefined();
        expect(telnyxFetchCalls.length).toBe(0);
      } finally {
        await server.stop();
      }
    } finally {
      spy.mockRestore();
    }
  });

  it('returns 400 for invalid telnyx body', async () => {
    const server = new EmbeddedServer(
      makeConfig({
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_test',
      }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: 'not an object' }),
      });

      expect(resp.status).toBe(400);
    } finally {
      await server.stop();
    }
  });

  it('returns 400 for telnyx body missing event_type', async () => {
    const server = new EmbeddedServer(
      makeConfig({
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_test',
      }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = await getFreePort();
    await server.start(port);

    try {
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: { payload: {} } }),
      });

      expect(resp.status).toBe(400);
    } finally {
      await server.stop();
    }
  });

  it('AMD webhook triggers voicemail for machine_end_beep', async () => {
    // Intercept fetch calls: pass through local server requests, mock Twilio API calls
    const originalFetch = globalThis.fetch;
    const twilioFetchCalls: Array<[string | URL | Request, RequestInit | undefined]> = [];
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: string | URL | Request, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        if (url.includes('api.twilio.com')) {
          twilioFetchCalls.push([input, init]);
          return {
            ok: true,
            status: 200,
            json: async () => ({}),
            text: async () => '',
          } as Response;
        }
        return originalFetch(input, init);
      },
    );

    try {
      const token = 'tok_test';
      const server = new EmbeddedServer(
        makeConfig({ twilioToken: token }),
        makeAgent(),
        undefined, undefined, undefined, undefined, false,
        'Please leave a message after the beep',
        undefined, undefined, false,
      );

      const port = await getFreePort();
      await server.start(port);

      try {
        // Compute a valid Twilio signature for this request
        const validSid = 'CA' + 'fedcba9876543210fedcba9876543210';
        const params: Record<string, string> = {
          AnsweredBy: 'machine_end_beep',
          CallSid: validSid,
        };
        const sigUrl = `https://abc.ngrok.io/webhooks/twilio/amd`;
        const sigData = sigUrl + Object.keys(params).sort().reduce(
          (acc, key) => acc + key + params[key], '',
        );
        const validSig = crypto.createHmac('sha1', token).update(sigData).digest('base64');

        const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/amd`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Twilio-Signature': validSig,
          },
          body: new URLSearchParams(params).toString(),
        });

        expect(resp.status).toBe(204);

        // Wait a tick for the async voicemail fire-and-forget to complete
        await new Promise((r) => setTimeout(r, 50));

        // Should have called Twilio API to update the call with voicemail TwiML
        const vmCall = twilioFetchCalls.find(
          ([url]) => typeof url === 'string' && url.includes(`Calls/${validSid}.json`),
        );
        expect(vmCall).toBeDefined();
      } finally {
        await server.stop();
      }
    } finally {
      spy.mockRestore();
    }
  });

  // -------------------------------------------------------------------------
  // FIX #91 — twilio /status invokes recordPrewarmWaste on no-answer/busy/...
  // -------------------------------------------------------------------------

  it('twilio /status invokes recordPrewarmWaste on abnormal CallStatus', async () => {
    const server = new EmbeddedServer(
      makeConfig({ twilioToken: '' }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const wasteCalls: string[] = [];
    server.recordPrewarmWaste = (callId: string) => {
      wasteCalls.push(callId);
    };

    const port = await getFreePort();
    await server.start(port);

    try {
      for (const status of ['no-answer', 'busy', 'failed', 'canceled']) {
        const sid = `CA_${status.replace('-', '_')}_001`;
        const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ CallSid: sid, CallStatus: status }).toString(),
        });
        expect(resp.status).toBe(204);
      }
      expect(wasteCalls).toEqual([
        'CA_no_answer_001',
        'CA_busy_001',
        'CA_failed_001',
        'CA_canceled_001',
      ]);

      // ``completed`` is normal — must NOT trigger eviction.
      wasteCalls.length = 0;
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ CallSid: 'CA_done_001', CallStatus: 'completed' }).toString(),
      });
      expect(resp.status).toBe(204);
      expect(wasteCalls).toEqual([]);
    } finally {
      await server.stop();
    }
  });

  // -------------------------------------------------------------------------
  // FIX #91 — telnyx /voice on call.hangup invokes recordPrewarmWaste
  // -------------------------------------------------------------------------

  it('telnyx call.hangup invokes recordPrewarmWaste', async () => {
    const server = new EmbeddedServer(
      makeConfig({
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_test',
        telnyxConnectionId: 'conn_test',
      }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );
    const wasteCalls: string[] = [];
    server.recordPrewarmWaste = (callId: string) => {
      wasteCalls.push(callId);
    };

    const port = await getFreePort();
    await server.start(port);

    try {
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          data: {
            event_type: 'call.hangup',
            payload: {
              call_control_id: 'ctrl-hung-up',
              hangup_cause: 'no_answer',
            },
          },
        }),
      });
      expect(resp.status).toBe(200);
      expect(wasteCalls).toEqual(['ctrl-hung-up']);
    } finally {
      await server.stop();
    }
  });
});

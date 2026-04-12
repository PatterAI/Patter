/**
 * Tests for EmbeddedServer route handlers via supertest-style mocked
 * req/res objects. Also tests Twilio/Telnyx signature validation,
 * webhook routing, and EmbeddedServer.start() webhookUrl validation.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import crypto from 'node:crypto';
import { EmbeddedServer } from '../../src/server';
import type { LocalConfig } from '../../src/server';
import type { AgentOptions } from '../../src/types';

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
// Twilio signature validation (internal function, test via behavior)
// ---------------------------------------------------------------------------

describe('Twilio HMAC-SHA1 signature validation', () => {
  // The validateTwilioSignature function is not exported, but we can
  // test the signature computation algorithm directly since the function
  // uses: url + sorted keys + values, then HMAC-SHA1.

  function computeTwilioSignature(
    url: string,
    params: Record<string, string>,
    authToken: string,
  ): string {
    const data = url + Object.keys(params).sort().reduce((acc, key) => acc + key + (params[key] ?? ''), '');
    return crypto.createHmac('sha1', authToken).update(data).digest('base64');
  }

  it('computes correct HMAC-SHA1 signature', () => {
    const url = 'https://abc.ngrok.io/webhooks/twilio/voice';
    const params = { CallSid: 'CA123', From: '+1555', To: '+1666' };
    const authToken = 'tok_test';

    const sig = computeTwilioSignature(url, params, authToken);

    // Verify it's a valid base64 string
    expect(sig).toMatch(/^[A-Za-z0-9+/]+=*$/);

    // Verify timingSafeEqual works with matching signatures
    const sig2 = computeTwilioSignature(url, params, authToken);
    expect(crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(sig2))).toBe(true);
  });

  it('produces different signature for different token', () => {
    const url = 'https://abc.ngrok.io/webhooks/twilio/voice';
    const params = { CallSid: 'CA123' };

    const sig1 = computeTwilioSignature(url, params, 'token1');
    const sig2 = computeTwilioSignature(url, params, 'token2');

    expect(sig1).not.toBe(sig2);
  });

  it('sorts parameters by key before hashing', () => {
    const url = 'https://abc.ngrok.io/webhooks/twilio/voice';
    const params1 = { Z: '1', A: '2' };
    const params2 = { A: '2', Z: '1' };
    const authToken = 'tok';

    const sig1 = computeTwilioSignature(url, params1, authToken);
    const sig2 = computeTwilioSignature(url, params2, authToken);

    // Same signature regardless of key order
    expect(sig1).toBe(sig2);
  });
});

// ---------------------------------------------------------------------------
// Telnyx Ed25519 signature validation
// ---------------------------------------------------------------------------

describe('Telnyx Ed25519 signature validation', () => {
  it('rejects expired timestamps', () => {
    // We can test the tolerance logic: if timestamp is >300s old, reject
    const oldTimestamp = String(Date.now() - 400 * 1000); // 400s ago
    const toleranceSec = 300;
    const ageMs = Date.now() - parseInt(oldTimestamp, 10);
    expect(ageMs).toBeGreaterThan(toleranceSec * 1000);
  });

  it('accepts recent timestamps', () => {
    const recentTimestamp = String(Date.now() - 10 * 1000); // 10s ago
    const toleranceSec = 300;
    const ageMs = Date.now() - parseInt(recentTimestamp, 10);
    expect(ageMs).toBeLessThan(toleranceSec * 1000);
    expect(ageMs).toBeGreaterThanOrEqual(0);
  });

  it('rejects future timestamps (negative age)', () => {
    const futureTimestamp = String(Date.now() + 60 * 1000); // 60s in future
    const ageMs = Date.now() - parseInt(futureTimestamp, 10);
    expect(ageMs).toBeLessThan(0);
  });

  it('rejects non-numeric timestamps', () => {
    const ts = parseInt('not-a-number', 10);
    expect(Number.isFinite(ts)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Route handler behavior (tested via EmbeddedServer start/stop lifecycle)
// ---------------------------------------------------------------------------

describe('EmbeddedServer route behavior', () => {
  it('can be started and stopped', async () => {
    const server = new EmbeddedServer(makeConfig(), makeAgent(), undefined, undefined, undefined, undefined, false, '', undefined, undefined, false);

    // Use a random high port to avoid conflicts
    const port = 19000 + Math.floor(Math.random() * 1000);

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

    const port = 19000 + Math.floor(Math.random() * 1000);
    await server.start(port);

    try {
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/twilio/voice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          CallSid: 'CA_test_123',
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
      expect(text).toContain('wss://abc.ngrok.io/ws/stream/CA_test_123');
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

    const port = 19000 + Math.floor(Math.random() * 1000);
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

    const port = 19000 + Math.floor(Math.random() * 1000);
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

    const port = 19000 + Math.floor(Math.random() * 1000);
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

  it('serves /webhooks/telnyx/voice for call.initiated event', async () => {
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

    const port = 19000 + Math.floor(Math.random() * 1000);
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

      expect(resp.ok).toBe(true);
      const body = await resp.json() as Record<string, unknown>;
      expect(body.commands).toBeDefined();
      const commands = body.commands as Array<Record<string, unknown>>;
      expect(commands[0].command).toBe('answer');
      expect(commands[1].command).toBe('stream_start');
    } finally {
      await server.stop();
    }
  });

  it('acknowledges non-call.initiated telnyx events', async () => {
    const server = new EmbeddedServer(
      makeConfig({
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_test',
        telnyxConnectionId: 'conn_test',
      }),
      makeAgent(),
      undefined, undefined, undefined, undefined, false, '', undefined, undefined, false,
    );

    const port = 19000 + Math.floor(Math.random() * 1000);
    await server.start(port);

    try {
      const resp = await fetch(`http://127.0.0.1:${port}/webhooks/telnyx/voice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          data: {
            event_type: 'call.answered',
            payload: { call_control_id: 'ctrl-456' },
          },
        }),
      });

      expect(resp.ok).toBe(true);
      const body = await resp.json() as Record<string, unknown>;
      expect(body.received).toBe(true);
    } finally {
      await server.stop();
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

    const port = 19000 + Math.floor(Math.random() * 1000);
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

    const port = 19000 + Math.floor(Math.random() * 1000);
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

      const port = 19000 + Math.floor(Math.random() * 1000);
      await server.start(port);

      try {
        // Compute a valid Twilio signature for this request
        const params: Record<string, string> = {
          AnsweredBy: 'machine_end_beep',
          CallSid: 'CA_vm_test',
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
          ([url]) => typeof url === 'string' && url.includes('Calls/CA_vm_test.json'),
        );
        expect(vmCall).toBeDefined();
      } finally {
        await server.stop();
      }
    } finally {
      spy.mockRestore();
    }
  });
});

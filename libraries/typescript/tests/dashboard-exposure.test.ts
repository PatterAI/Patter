/**
 * Fail-closed dashboard exposure tests.
 *
 * These exercise the REAL Express app mounted by the REAL ``EmbeddedServer``:
 * each test binds the server on a loopback port and uses a real ``fetch`` to
 * probe which routes are actually mounted. No mocks — the litmus test is that
 * replacing the gate in ``server.ts`` with a no-op (always mount) makes case 1
 * fail, because the dashboard root + ``/api/*`` routes would then respond 200
 * on an exposed, unauthenticated bind.
 *
 * The gate: when the dashboard is enabled, the token is empty, AND the server
 * is reachable beyond loopback (a public ``webhookUrl`` here, mirroring a
 * cloudflared tunnel / static tunnel / explicit public webhook), the dashboard
 * + call-data ``/api/*`` routes are NOT mounted. The carrier webhook route
 * stays mounted so calls keep working.
 */

import { afterEach, describe, expect, it } from 'vitest';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { EmbeddedServer } from '../src/server';
import type { LocalConfig } from '../src/server';
import type { AgentOptions } from '../src/types';

/** A public-looking webhook host triggers exposure signals (a)+(b). */
const EXPOSED_WEBHOOK = 'abc123.trycloudflare.com';
/** A loopback webhook host keeps the server local-only (not exposed). */
const LOOPBACK_WEBHOOK = '127.0.0.1';

function makeConfig(overrides: Partial<LocalConfig> = {}): LocalConfig {
  return {
    twilioSid: 'AC_test',
    twilioToken: 'tok_test',
    openaiKey: 'sk_test',
    phoneNumber: '+15550000000',
    webhookUrl: EXPOSED_WEBHOOK,
    telephonyProvider: 'twilio',
    // Keep the dashboard purely in-memory so tests don't hydrate from disk.
    persistRoot: null,
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

/** Reserve a free loopback port by opening then closing a throwaway listener. */
function reserveFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const port = (probe.address() as AddressInfo).port;
      probe.close(() => resolve(port));
    });
  });
}

interface Running {
  readonly server: EmbeddedServer;
  readonly port: number;
}

async function startServer(
  config: LocalConfig,
  opts: { token?: string; allowInsecure?: boolean } = {},
): Promise<Running> {
  const port = await reserveFreePort();
  const server = new EmbeddedServer(
    config,
    makeAgent(),
    undefined, // onCallStart
    undefined, // onCallEnd
    undefined, // onTranscript
    undefined, // onMessage
    false, // recording
    '', // voicemailMessage
    undefined, // onMetrics
    undefined, // pricingOverrides
    true, // dashboard ENABLED
    opts.token ?? '', // dashboardToken
    opts.allowInsecure ?? false, // allowInsecureDashboard
  );
  await server.start(port);
  return { server, port };
}

describe('[integration] dashboard fail-closed exposure gate', () => {
  let running: Running | null = null;

  afterEach(async () => {
    if (running) {
      await running.server.stop();
      running = null;
    }
    delete process.env.PATTER_BIND_HOST;
  });

  it('does NOT mount dashboard or call-data API when exposed + unauthenticated, but keeps the carrier webhook', async () => {
    running = await startServer(makeConfig({ webhookUrl: EXPOSED_WEBHOOK }));
    const base = `http://127.0.0.1:${running.port}`;

    // Dashboard UI root: absent (no GET '/' route) => 404.
    const root = await fetch(`${base}/`);
    expect(root.status).toBe(404);

    // Call-data dashboard API: absent => 404 (NOT 200 with PII, NOT 401).
    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(404);

    // B2B call-data API: absent => 404.
    const v1Calls = await fetch(`${base}/api/v1/calls`);
    expect(v1Calls.status).toBe(404);

    // Health route is unaffected.
    const health = await fetch(`${base}/health`);
    expect(health.status).toBe(200);

    // Carrier webhook route still mounted => calls keep working. A POST with
    // no signature is rejected (503 signature-required) — crucially NOT 404,
    // which would mean the route was dropped.
    const webhook = await fetch(`${base}/webhooks/twilio/voice`, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: 'CallSid=CA0000000000000000000000000000a001',
    });
    expect(webhook.status).not.toBe(404);
  });

  it('mounts dashboard + API (reachable, 401 when unauthenticated) when exposed + token SET', async () => {
    running = await startServer(makeConfig({ webhookUrl: EXPOSED_WEBHOOK }), {
      token: 'secret-token-123',
    });
    const base = `http://127.0.0.1:${running.port}`;

    // Routes ARE mounted; the auth middleware answers => 401 (not 404).
    const root = await fetch(`${base}/`);
    expect(root.status).toBe(401);

    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(401);

    const v1Calls = await fetch(`${base}/api/v1/calls`);
    expect(v1Calls.status).toBe(401);

    // With the bearer token the call-data route serves normally.
    const authed = await fetch(`${base}/api/dashboard/calls`, {
      headers: { authorization: 'Bearer secret-token-123' },
    });
    expect(authed.status).toBe(200);
  });

  it('mounts dashboard + API when loopback-only + unauthenticated (local-dev backward compat)', async () => {
    running = await startServer(makeConfig({ webhookUrl: LOOPBACK_WEBHOOK }));
    const base = `http://127.0.0.1:${running.port}`;

    // Local dev: dashboard served unauthenticated => 200, not gated.
    const root = await fetch(`${base}/`);
    expect(root.status).toBe(200);

    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(200);

    const v1Calls = await fetch(`${base}/api/v1/calls`);
    expect(v1Calls.status).toBe(200);
  });

  it('mounts dashboard + API when exposed + unauthenticated + allowInsecureDashboard escape hatch', async () => {
    running = await startServer(makeConfig({ webhookUrl: EXPOSED_WEBHOOK }), {
      allowInsecure: true,
    });
    const base = `http://127.0.0.1:${running.port}`;

    // Escape hatch: routes mounted unauthenticated => 200.
    const root = await fetch(`${base}/`);
    expect(root.status).toBe(200);

    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(200);

    const v1Calls = await fetch(`${base}/api/v1/calls`);
    expect(v1Calls.status).toBe(200);
  });

  it('does NOT mount dashboard when an explicit non-loopback PATTER_BIND_HOST override is set (signal c)', async () => {
    // Even with a loopback webhook host, an explicit bind override to a
    // non-loopback address marks the server exposed (signal c). The override
    // also drives the real listen address — the server binds to 0.0.0.0,
    // which still accepts connections on 127.0.0.1 so the test can fetch it.
    process.env.PATTER_BIND_HOST = '0.0.0.0';
    const port = await reserveFreePort();
    const server = new EmbeddedServer(
      makeConfig({ webhookUrl: LOOPBACK_WEBHOOK }),
      makeAgent(),
      undefined,
      undefined,
      undefined,
      undefined,
      false,
      '',
      undefined,
      undefined,
      true,
      '',
      false,
    );
    await server.start(port);
    running = { server, port };
    const base = `http://127.0.0.1:${port}`;

    const root = await fetch(`${base}/`);
    expect(root.status).toBe(404);

    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(404);

    // Health + webhook unaffected.
    const health = await fetch(`${base}/health`);
    expect(health.status).toBe(200);
  });
});

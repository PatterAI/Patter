/**
 * Dashboard auto-token exposure tests.
 *
 * These exercise the REAL Express app mounted by the REAL ``EmbeddedServer``:
 * each test binds the server on a loopback port and uses a real ``fetch`` to
 * probe the actual auth behaviour of the mounted routes. No mocks — the
 * dashboard is ALWAYS mounted now (it never 404s); the protection comes from
 * the token resolved in ``start()``.
 *
 * Resolution: when the dashboard is enabled with no explicit token AND the
 * server is reachable beyond loopback (a public ``webhookUrl`` here, mirroring
 * a cloudflared tunnel / static tunnel / explicit public webhook), the SDK
 * generates a one-time token and protects the dashboard + call-data ``/api/*``
 * routes with it. An explicit token is honoured as-is. Loopback-only dev and
 * the ``allowInsecureDashboard`` escape hatch serve the dashboard OPEN.
 *
 * LITMUS: case 1 proves protection — if the auto-token branch were removed
 * (effective token left ``''``), the unauthenticated request would return 200
 * and the 401 assertion would FAIL.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { EmbeddedServer } from '../src/server';
import type { LocalConfig } from '../src/server';
import type { AgentOptions } from '../src/types';
import { getLogger } from '../src/logger';

/** A public-looking webhook host triggers exposure signals (a)+(b). */
const EXPOSED_WEBHOOK = 'abc123.trycloudflare.com';
/** A loopback webhook host keeps the server local-only (not exposed). */
const LOOPBACK_WEBHOOK = '127.0.0.1';

/** RFC 4122 v4 UUID matcher (the shape ``crypto.randomUUID()`` produces). */
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

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

describe('[integration] dashboard auto-token exposure protection', () => {
  let running: Running | null = null;

  afterEach(async () => {
    if (running) {
      await running.server.stop();
      running = null;
    }
    delete process.env.PATTER_BIND_HOST;
    vi.restoreAllMocks();
  });

  it('case 1: exposed + no token => routes mounted, 401 unauthenticated, 200 with the resolved auto-token (a UUID)', async () => {
    running = await startServer(makeConfig({ webhookUrl: EXPOSED_WEBHOOK }));
    const base = `http://127.0.0.1:${running.port}`;

    // The SDK resolved a non-empty UUID token and stored it on the server.
    const token = running.server.resolvedDashboardToken;
    expect(token).toMatch(UUID_RE);

    // Dashboard UI root + call-data routes ARE mounted (not 404), but the
    // auth middleware rejects the unauthenticated request => 401. This is the
    // litmus: if the auto-token branch were removed these would be 200.
    const root = await fetch(`${base}/`);
    expect(root.status).toBe(401);

    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(401);

    const v1Calls = await fetch(`${base}/api/v1/calls`);
    expect(v1Calls.status).toBe(401);

    // The resolved token authenticates via the ?token= query param.
    const queryAuthed = await fetch(`${base}/api/dashboard/calls?token=${token}`);
    expect(queryAuthed.status).toBe(200);

    // ...and via the Authorization: Bearer header.
    const headerAuthed = await fetch(`${base}/api/dashboard/calls`, {
      headers: { authorization: `Bearer ${token}` },
    });
    expect(headerAuthed.status).toBe(200);

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

  it('case 2: exposed + explicit dashboardToken => 401 without it, 200 with it', async () => {
    running = await startServer(makeConfig({ webhookUrl: EXPOSED_WEBHOOK }), {
      token: 'secret-token-123',
    });
    const base = `http://127.0.0.1:${running.port}`;

    // The explicit token is honoured as-is (no auto-generation).
    expect(running.server.resolvedDashboardToken).toBe('secret-token-123');

    const root = await fetch(`${base}/`);
    expect(root.status).toBe(401);

    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(401);

    const authed = await fetch(`${base}/api/dashboard/calls`, {
      headers: { authorization: 'Bearer secret-token-123' },
    });
    expect(authed.status).toBe(200);
  });

  it('case 3: loopback-only + no token => routes mounted and OPEN (200 without any token)', async () => {
    running = await startServer(makeConfig({ webhookUrl: LOOPBACK_WEBHOOK }));
    const base = `http://127.0.0.1:${running.port}`;

    // Local dev: no token auto-generated, dashboard served OPEN.
    expect(running.server.resolvedDashboardToken).toBe('');

    const root = await fetch(`${base}/`);
    expect(root.status).toBe(200);

    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(200);

    const v1Calls = await fetch(`${base}/api/v1/calls`);
    expect(v1Calls.status).toBe(200);
  });

  it('case 4: exposed + no token + allowInsecureDashboard=true => routes mounted and OPEN (200), warning logged', async () => {
    const warnSpy = vi.spyOn(getLogger(), 'warn');
    running = await startServer(makeConfig({ webhookUrl: EXPOSED_WEBHOOK }), {
      allowInsecure: true,
    });
    const base = `http://127.0.0.1:${running.port}`;

    // Escape hatch: served OPEN, no token resolved.
    expect(running.server.resolvedDashboardToken).toBe('');

    const root = await fetch(`${base}/`);
    expect(root.status).toBe(200);

    const dashCalls = await fetch(`${base}/api/dashboard/calls`);
    expect(dashCalls.status).toBe(200);

    const v1Calls = await fetch(`${base}/api/v1/calls`);
    expect(v1Calls.status).toBe(200);

    // A warning about serving the PII surface unauthenticated was logged.
    const warned = warnSpy.mock.calls.some((args) =>
      String(args[0] ?? '').includes('WITHOUT authentication'),
    );
    expect(warned).toBe(true);
  });
});

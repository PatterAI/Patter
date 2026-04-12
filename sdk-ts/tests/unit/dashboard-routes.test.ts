/**
 * Tests for dashboard/routes.ts — mountDashboard and mountApi route handlers.
 * Uses mocked Express req/res objects to test route handlers directly.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import express from 'express';
import { createServer } from 'http';
import { mountDashboard, mountApi } from '../../src/dashboard/routes';
import { MetricsStore } from '../../src/dashboard/store';

// ---------------------------------------------------------------------------
// Helpers: create a real Express app and test via HTTP
// ---------------------------------------------------------------------------

function createTestApp(opts: { token?: string; seedCalls?: boolean } = {}) {
  const store = new MetricsStore();
  const app = express();
  app.use(express.json());
  app.use(express.urlencoded({ extended: true }));

  mountDashboard(app, store, opts.token ?? '');
  mountApi(app, store, opts.token ?? '');

  if (opts.seedCalls) {
    // Seed some test call data
    store.recordCallStart({
      call_id: 'call-1',
      caller: '+15551111111',
      callee: '+15552222222',
      direction: 'inbound',
      start_time: Date.now() / 1000 - 60,
    });
    store.recordCallEnd({
      call_id: 'call-1',
      metrics: {
        duration: 30,
        cost: { total: 0.05, stt: 0.01, tts: 0.02, llm: 0.015, telephony: 0.005 },
      },
    });
    store.recordCallStart({
      call_id: 'call-2',
      caller: '+15553333333',
      callee: '+15554444444',
      direction: 'outbound',
      start_time: Date.now() / 1000,
    });
  }

  return { app, store };
}

async function startServer(app: express.Express): Promise<{ port: number; close: () => Promise<void> }> {
  const server = createServer(app);
  const port = 19500 + Math.floor(Math.random() * 500);
  await new Promise<void>((resolve) => server.listen(port, '127.0.0.1', resolve));
  return {
    port,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

// ---------------------------------------------------------------------------
// Dashboard routes
// ---------------------------------------------------------------------------

describe('mountDashboard', () => {
  it('serves dashboard HTML at /', async () => {
    const { app } = createTestApp();
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/`);
      expect(resp.ok).toBe(true);
      const text = await resp.text();
      expect(text).toContain('html');
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/calls returns call list', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/calls`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as unknown[];
      expect(Array.isArray(body)).toBe(true);
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/calls respects limit and offset', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/calls?limit=1&offset=0`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as unknown[];
      expect(body.length).toBeLessThanOrEqual(1);
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/calls/:callId returns single call', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/calls/call-1`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as Record<string, unknown>;
      expect(body.call_id).toBe('call-1');
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/calls/:callId returns 404 for unknown call', async () => {
    const { app } = createTestApp();
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/calls/nonexistent`);
      expect(resp.status).toBe(404);
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/active returns active calls', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/active`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as unknown[];
      expect(Array.isArray(body)).toBe(true);
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/aggregates returns stats', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/aggregates`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as Record<string, unknown>;
      expect(body).toBeDefined();
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/export/calls returns JSON by default', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/export/calls`);
      expect(resp.ok).toBe(true);
      expect(resp.headers.get('content-type')).toContain('application/json');
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/export/calls?format=csv returns CSV', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/export/calls?format=csv`);
      expect(resp.ok).toBe(true);
      expect(resp.headers.get('content-type')).toContain('text/csv');
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/export/calls supports date range filtering', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const from = new Date(Date.now() - 3600_000).toISOString();
      const to = new Date().toISOString();
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/export/calls?from=${from}&to=${to}`);
      expect(resp.ok).toBe(true);
    } finally {
      await srv.close();
    }
  });

  it('GET /api/dashboard/events returns SSE stream', async () => {
    const { app } = createTestApp();
    const srv = await startServer(app);
    try {
      const controller = new AbortController();
      // SSE endpoints never resolve the response body — abort after getting headers
      setTimeout(() => controller.abort(), 200);
      try {
        const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/events`, {
          signal: controller.signal,
        });
        // If we get here, check status/headers before abort fires
        expect(resp.status).toBe(200);
      } catch (err: unknown) {
        // AbortError is expected — the SSE stream was opened and then aborted
        if (err instanceof Error && err.name !== 'AbortError') throw err;
      }
    } finally {
      await srv.close();
    }
  });

  it('rejects unauthenticated requests when token is set', async () => {
    const { app } = createTestApp({ token: 'secret-token' });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/dashboard/calls`);
      expect(resp.status).toBe(401);
    } finally {
      await srv.close();
    }
  });
});

// ---------------------------------------------------------------------------
// B2B API routes
// ---------------------------------------------------------------------------

describe('mountApi', () => {
  it('GET /api/v1/calls returns paginated response', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/calls`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as { data: unknown[]; pagination: { limit: number; offset: number; count: number; total: number } };
      expect(body.data).toBeDefined();
      expect(body.pagination).toBeDefined();
      expect(body.pagination.limit).toBe(50);
      expect(body.pagination.offset).toBe(0);
    } finally {
      await srv.close();
    }
  });

  it('GET /api/v1/calls respects limit and offset', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/calls?limit=1&offset=1`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as { pagination: { limit: number; offset: number } };
      expect(body.pagination.limit).toBe(1);
      expect(body.pagination.offset).toBe(1);
    } finally {
      await srv.close();
    }
  });

  it('GET /api/v1/calls/active returns active calls', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/calls/active`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as { data: unknown[]; count: number };
      expect(body.data).toBeDefined();
      expect(typeof body.count).toBe('number');
    } finally {
      await srv.close();
    }
  });

  it('GET /api/v1/calls/:callId returns single call', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/calls/call-1`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as { data: { call_id: string } };
      expect(body.data.call_id).toBe('call-1');
    } finally {
      await srv.close();
    }
  });

  it('GET /api/v1/calls/:callId returns 404 for unknown call', async () => {
    const { app } = createTestApp();
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/calls/nonexistent`);
      expect(resp.status).toBe(404);
      const body = await resp.json() as { error: string };
      expect(body.error).toContain('not found');
    } finally {
      await srv.close();
    }
  });

  it('GET /api/v1/analytics/overview returns aggregate stats', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/analytics/overview`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as { data: Record<string, unknown> };
      expect(body.data).toBeDefined();
    } finally {
      await srv.close();
    }
  });

  it('GET /api/v1/analytics/costs returns cost breakdown', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/analytics/costs`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as { data: { total_cost: number; breakdown: Record<string, number>; calls_analyzed: number; period: { from: string | null; to: string | null } } };
      expect(body.data.total_cost).toBeDefined();
      expect(body.data.breakdown).toBeDefined();
      expect(typeof body.data.calls_analyzed).toBe('number');
    } finally {
      await srv.close();
    }
  });

  it('GET /api/v1/analytics/costs supports date range filtering', async () => {
    const { app } = createTestApp({ seedCalls: true });
    const srv = await startServer(app);
    try {
      const from = new Date(Date.now() - 3600_000).toISOString();
      const to = new Date().toISOString();
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/analytics/costs?from=${from}&to=${to}`);
      expect(resp.ok).toBe(true);
      const body = await resp.json() as { data: { period: { from: string | null; to: string | null } } };
      expect(body.data.period.from).toBe(from);
      expect(body.data.period.to).toBe(to);
    } finally {
      await srv.close();
    }
  });

  it('rejects unauthenticated requests when token is set', async () => {
    const { app } = createTestApp({ token: 'api-token' });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/calls`);
      expect(resp.status).toBe(401);
    } finally {
      await srv.close();
    }
  });

  it('accepts authenticated requests with Bearer token', async () => {
    const { app } = createTestApp({ token: 'api-token', seedCalls: true });
    const srv = await startServer(app);
    try {
      const resp = await fetch(`http://127.0.0.1:${srv.port}/api/v1/calls`, {
        headers: { Authorization: 'Bearer api-token' },
      });
      expect(resp.ok).toBe(true);
    } finally {
      await srv.close();
    }
  });
});

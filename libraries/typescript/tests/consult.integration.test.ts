/**
 * Integration tests for the consult tool handler against a REAL local HTTP
 * orchestrator server (Node ``http``) with a real ``fetch``. Only the SSRF
 * guard is relaxed (so the loopback-bound test server is reachable); the guard
 * itself is verified in ``consult.test.ts``.
 */

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { createServer, type Server } from 'node:http';
import { AddressInfo } from 'node:net';

// Relax ONLY the SSRF validator so the consult handler can reach the
// loopback test server. Everything else in server.ts stays real.
vi.mock('../src/server', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/server')>();
  return { ...actual, validateWebhookUrl: () => {} };
});

import { buildConsultTool } from '../src/consult';

interface Captured {
  path?: string;
  headers: Record<string, string | string[] | undefined>;
  json?: unknown;
}

function startServer(status: number, body: string): { server: Server; url: () => string; captured: Captured } {
  const captured: Captured = { headers: {} };
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c as Buffer));
    req.on('end', () => {
      captured.path = req.url;
      captured.headers = req.headers;
      try {
        captured.json = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
      } catch {
        captured.json = undefined;
      }
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(body);
    });
  });
  const url = (): string => `http://127.0.0.1:${(server.address() as AddressInfo).port}/consult`;
  return { server, url, captured };
}

describe('[integration] consult tool handler', () => {
  let ctx: ReturnType<typeof startServer>;

  function listen(c: ReturnType<typeof startServer>): Promise<void> {
    return new Promise((resolve) => c.server.listen(0, '127.0.0.1', resolve));
  }

  afterAll(() => {
    ctx?.server.close();
  });

  it('POSTs the request + call correlation, forwards headers, returns the reply field', async () => {
    ctx = startServer(200, JSON.stringify({ reply: 'The order ships Tuesday.' }));
    await listen(ctx);
    const tool = buildConsultTool({ url: ctx.url(), headers: { Authorization: 'Bearer secret-xyz' } });
    const result = await (tool.handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
      { request: 'When does my order ship?' },
      { call_id: 'CAtest', caller: '+15555550100', callee: '+15555550199' },
    );
    expect(result).toBe('The order ships Tuesday.');
    expect(ctx.captured.json).toEqual({
      request: 'When does my order ship?',
      call_id: 'CAtest',
      caller: '+15555550100',
      callee: '+15555550199',
    });
    expect(ctx.captured.headers['authorization']).toBe('Bearer secret-xyz');
    ctx.server.close();
  });

  it('returns raw text when the orchestrator does not reply with JSON', async () => {
    ctx = startServer(200, 'plain text answer');
    await listen(ctx);
    const tool = buildConsultTool({ url: ctx.url() });
    const result = await (tool.handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
      { request: 'hi' },
      { call_id: 'x' },
    );
    expect(result).toBe('plain text answer');
    ctx.server.close();
  });

  it('returns a graceful fallback on a server error', async () => {
    ctx = startServer(500, 'boom');
    await listen(ctx);
    const tool = buildConsultTool({ url: ctx.url() });
    const result = await (tool.handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
      { request: 'hi' },
      { call_id: 'x' },
    );
    expect(result.toLowerCase()).toContain("wasn't able to reach");
    ctx.server.close();
  });
});

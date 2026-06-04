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

import { buildConsultTool, openclawConsult, openclawPostCallNotifier } from '../src/consult';

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

describe('[integration] openclaw consult handler', () => {
  function listen(c: ReturnType<typeof startServer>): Promise<void> {
    return new Promise((resolve) => c.server.listen(0, '127.0.0.1', resolve));
  }
  function baseUrl(c: ReturnType<typeof startServer>): string {
    return `http://127.0.0.1:${(c.server.address() as AddressInfo).port}/v1`;
  }
  const callHandler = (tool: ReturnType<typeof buildConsultTool>) =>
    tool.handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>;

  it('POSTs chat-completions to /v1/chat/completions and speaks the content', async () => {
    const ctx = startServer(
      200,
      JSON.stringify({ choices: [{ message: { content: "You're set for Thursday at 10am." } }] }),
    );
    await listen(ctx);
    const tool = buildConsultTool(
      openclawConsult('receptionist', { baseUrl: baseUrl(ctx), apiKey: 'op-secret' }),
    );
    const result = await callHandler(tool)(
      { request: 'Reschedule my roof inspection to Thursday' },
      { call_id: 'CAxyz', caller: '+15555550100', callee: '+15555550199' },
    );
    expect(result).toBe("You're set for Thursday at 10am.");
    expect(ctx.captured.path).toBe('/v1/chat/completions');
    const body = ctx.captured.json as {
      model: string;
      user: string;
      stream: boolean;
      messages: Array<{ role: string; content: string }>;
    };
    expect(body.model).toBe('openclaw/receptionist');
    expect(body.stream).toBe(false);
    expect(body.user).toBe('CAxyz');
    expect(body.messages.map((m) => m.role)).toEqual(['system', 'user']);
    expect(body.messages[1].content).toContain('Reschedule my roof inspection');
    expect(body.messages[0].content).toContain('+15555550100');
    expect(ctx.captured.headers['authorization']).toBe('Bearer op-secret');
    expect(ctx.captured.headers['x-openclaw-session-key']).toBe('CAxyz');
    ctx.server.close();
  });

  it('reads the bearer from OPENCLAW_API_KEY when apiKey is not given', async () => {
    const prev = process.env.OPENCLAW_API_KEY;
    process.env.OPENCLAW_API_KEY = 'env-op-key';
    try {
      const ctx = startServer(200, JSON.stringify({ choices: [{ message: { content: 'ok' } }] }));
      await listen(ctx);
      const tool = buildConsultTool(openclawConsult('receptionist', { baseUrl: baseUrl(ctx) }));
      await callHandler(tool)({ request: 'hi' }, { call_id: 'c1' });
      expect(ctx.captured.headers['authorization']).toBe('Bearer env-op-key');
      ctx.server.close();
    } finally {
      if (prev === undefined) delete process.env.OPENCLAW_API_KEY;
      else process.env.OPENCLAW_API_KEY = prev;
    }
  });

  it('returns a graceful fallback on 404 (endpoint disabled)', async () => {
    const ctx = startServer(404, 'not found');
    await listen(ctx);
    const tool = buildConsultTool(openclawConsult('receptionist', { baseUrl: baseUrl(ctx) }));
    const result = await callHandler(tool)({ request: 'hi' }, { call_id: 'c1' });
    expect(result.toLowerCase()).toContain("wasn't able to reach");
    ctx.server.close();
  });
});

describe('[integration] openclaw post-call notifier', () => {
  function listen(c: ReturnType<typeof startServer>): Promise<void> {
    return new Promise((resolve) => c.server.listen(0, '127.0.0.1', resolve));
  }
  function baseUrl(c: ReturnType<typeof startServer>): string {
    return `http://127.0.0.1:${(c.server.address() as AddressInfo).port}/v1`;
  }

  it('POSTs the finished call record to the OpenClaw agent (same session)', async () => {
    const ctx = startServer(200, JSON.stringify({ choices: [{ message: { content: 'logged' } }] }));
    await listen(ctx);
    const notify = openclawPostCallNotifier('receptionist', {
      baseUrl: baseUrl(ctx),
      apiKey: 'op-secret',
    });
    await notify({
      call_id: 'CAend',
      caller: '+15555550100',
      callee: '+15555550199',
      transcript: [
        { role: 'user', text: 'Reschedule Tuesday' },
        { role: 'assistant', text: 'Done, moved to Thursday.' },
      ],
      metrics: { durationSeconds: 42 },
    });
    expect(ctx.captured.path).toBe('/v1/chat/completions');
    const body = ctx.captured.json as {
      model: string;
      user: string;
      messages: Array<{ role: string; content: string }>;
    };
    expect(body.model).toBe('openclaw/receptionist');
    expect(body.user).toBe('CAend');
    expect(ctx.captured.headers['x-openclaw-session-key']).toBe('CAend');
    expect(ctx.captured.headers['authorization']).toBe('Bearer op-secret');
    const record = body.messages[1].content;
    expect(record).toContain('+15555550100');
    expect(record).toContain('Reschedule Tuesday');
    expect(record).toContain('42s');
    ctx.server.close();
  });

  it('is fire-and-forget on a server error (never throws into teardown)', async () => {
    const ctx = startServer(500, 'boom');
    await listen(ctx);
    const notify = openclawPostCallNotifier('receptionist', { baseUrl: baseUrl(ctx) });
    await expect(notify({ call_id: 'x', caller: '+15555550100' })).resolves.toBeUndefined();
    ctx.server.close();
  });
});

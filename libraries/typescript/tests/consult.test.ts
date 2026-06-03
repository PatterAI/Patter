/**
 * Unit tests for the built-in ``consult`` escalation tool (parity with the
 * Python ``test_consult.py``).
 *
 * No network: SSRF rejection uses the real validator; handler HTTP behaviour
 * is covered in ``consult.integration.test.ts``.
 */

import { describe, expect, it } from 'vitest';
import { buildConsultTool, openclawConsult } from '../src/consult';
import { validateWebhookUrl } from '../src/server';
import type { ConsultConfig } from '../src/types';

describe('[unit] buildConsultTool', () => {
  it('rejects a non-http scheme', () => {
    expect(() => buildConsultTool({ url: 'ftp://orchestrator.example.com' } as ConsultConfig)).toThrow();
  });

  it('rejects an SSRF target (loopback)', () => {
    expect(() => buildConsultTool({ url: 'http://127.0.0.1:9/consult' } as ConsultConfig)).toThrow();
  });

  it('builds a tool with the default name and a request param', () => {
    const tool = buildConsultTool({ url: 'https://orchestrator.example.com/consult' });
    expect(tool.name).toBe('consult_agent');
    expect(typeof tool.handler).toBe('function');
    expect((tool.parameters as { required: string[] }).required).toEqual(['request']);
  });

  it('honours a custom tool name', () => {
    const tool = buildConsultTool({ url: 'https://orchestrator.example.com', toolName: 'ask_brain' });
    expect(tool.name).toBe('ask_brain');
  });

  it('rejects a loopback target by default (allowLoopback omitted)', () => {
    expect(() => buildConsultTool({ url: 'http://127.0.0.1:8642/consult' } as ConsultConfig)).toThrow();
    expect(() => buildConsultTool({ url: 'http://localhost:8642/consult' } as ConsultConfig)).toThrow();
  });

  it('permits a loopback IP when allowLoopback is true', () => {
    const tool = buildConsultTool({ url: 'http://127.0.0.1:8642/consult', allowLoopback: true });
    expect(tool.name).toBe('consult_agent');
    expect(typeof tool.handler).toBe('function');
  });

  it('permits the localhost hostname when allowLoopback is true', () => {
    const tool = buildConsultTool({ url: 'http://localhost:8642/consult', allowLoopback: true });
    expect(typeof tool.handler).toBe('function');
  });

  it('permits an RFC1918 private host when allowLoopback is true', () => {
    expect(() => buildConsultTool({ url: 'http://192.168.1.50:8642/consult', allowLoopback: true })).not.toThrow();
    expect(() => buildConsultTool({ url: 'http://10.0.0.4/consult', allowLoopback: true })).not.toThrow();
  });

  it('still rejects a non-http scheme even when allowLoopback is true', () => {
    expect(() =>
      buildConsultTool({ url: 'file:///etc/passwd', allowLoopback: true } as ConsultConfig),
    ).toThrow();
    expect(() =>
      buildConsultTool({ url: 'ftp://127.0.0.1/consult', allowLoopback: true } as ConsultConfig),
    ).toThrow();
  });

  it('treats allowLoopback=false the same as omitting it (still strict)', () => {
    expect(() =>
      buildConsultTool({ url: 'http://127.0.0.1:8642/consult', allowLoopback: false } as ConsultConfig),
    ).toThrow();
  });
});

describe('[unit] validateWebhookUrl loopback gating', () => {
  it('rejects loopback by default — the generic webhook-tool path stays strict', () => {
    // executeToolWebhook / mcp-client / remote-message all call the validator
    // with the single-arg default; assert that default rejects loopback.
    expect(() => validateWebhookUrl('http://127.0.0.1:8642/x')).toThrow();
    expect(() => validateWebhookUrl('http://localhost/x')).toThrow();
    expect(() => validateWebhookUrl('http://10.0.0.1/x')).toThrow();
    expect(() => validateWebhookUrl('http://[::1]/x')).toThrow();
  });

  it('permits loopback/private only when the second arg is explicitly true', () => {
    expect(() => validateWebhookUrl('http://127.0.0.1:8642/x', true)).not.toThrow();
    expect(() => validateWebhookUrl('http://localhost/x', true)).not.toThrow();
    expect(() => validateWebhookUrl('http://10.0.0.1/x', true)).not.toThrow();
    expect(() => validateWebhookUrl('http://[::1]/x', true)).not.toThrow();
  });

  it('never relaxes the scheme check, even with allowLoopback true', () => {
    expect(() => validateWebhookUrl('file:///etc/passwd', true)).toThrow();
    expect(() => validateWebhookUrl('javascript:alert(1)', true)).toThrow();
  });

  it('keeps a public host valid in both modes', () => {
    expect(() => validateWebhookUrl('https://orchestrator.example.com/consult')).not.toThrow();
    expect(() => validateWebhookUrl('https://orchestrator.example.com/consult', true)).not.toThrow();
  });
});

describe('[unit] openclawConsult preset (parity with ConsultConfig.openclaw)', () => {
  it('builds a namespaced model + OpenClaw defaults', () => {
    const c = openclawConsult('receptionist');
    expect(c.url).toBeUndefined();
    expect(c.openaiCompatible?.model).toBe('openclaw/receptionist');
    expect(c.openaiCompatible?.baseUrl).toBe('http://127.0.0.1:18789/v1');
    expect(c.openaiCompatible?.apiKeyEnv).toBe('OPENCLAW_API_KEY');
    expect(c.openaiCompatible?.sessionHeader).toBe('x-openclaw-session-key');
    expect(c.allowLoopback).toBe(true); // loopback default → SSRF auto-relaxed
    expect(c.timeoutMs).toBe(30_000); // phone-safe default, not regressed
    expect(typeof c.reassurance).toBe('string'); // default filler attached
    expect(c.description).toContain('NEVER');
  });

  it.each([
    ['receptionist', 'openclaw/receptionist'],
    ['openclaw/roofing-ca', 'openclaw/roofing-ca'],
    ['openclaw:home-fl', 'openclaw:home-fl'],
    ['agent:desk-1', 'agent:desk-1'],
  ])('passes through an already-namespaced agent target (%s)', (agent, expected) => {
    expect(openclawConsult(agent).openaiCompatible?.model).toBe(expected);
  });

  it.each(['', 'has space', 'a b', 'drop;table', 'x\n'])(
    'rejects an unsafe agent id (%j)',
    (bad) => {
      expect(() => openclawConsult(bad)).toThrow();
    },
  );

  it('keeps the strict SSRF default for a public base_url', () => {
    expect(
      openclawConsult('receptionist', { baseUrl: 'https://gw.example.com/v1' }).allowLoopback,
    ).toBe(false);
  });

  it('auto-enables allowLoopback for an IPv6 unique-local gateway (parity with Python)', () => {
    expect(openclawConsult('r', { baseUrl: 'http://[fd00::1]:18789/v1' }).allowLoopback).toBe(true);
  });

  it('honours an explicit allowLoopback override', () => {
    expect(openclawConsult('r', { allowLoopback: false }).allowLoopback).toBe(false);
  });

  it('attaches the default reassurance to the built tool', () => {
    const tool = buildConsultTool(openclawConsult('receptionist'));
    expect((tool as { reassurance?: unknown }).reassurance).toBe(
      'Let me check on that for you, one moment.',
    );
  });
});

describe('[unit] buildConsultTool target xor', () => {
  it('throws when neither url nor openaiCompatible is set', () => {
    expect(() => buildConsultTool({} as ConsultConfig)).toThrow(/exactly one/);
  });

  it('throws when both url and openaiCompatible are set', () => {
    expect(() =>
      buildConsultTool({
        url: 'https://x.example.com',
        openaiCompatible: { baseUrl: 'https://gw.example.com/v1', model: 'openclaw/r' },
      } as ConsultConfig),
    ).toThrow(/exactly one/);
  });

  it('the generic url path attaches no reassurance by default', () => {
    const tool = buildConsultTool({ url: 'https://x.example.com/consult' });
    expect((tool as { reassurance?: unknown }).reassurance).toBeUndefined();
  });
});

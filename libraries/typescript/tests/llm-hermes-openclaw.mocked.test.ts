/**
 * Tests for the Hermes and OpenClaw LLM presets.
 *
 * Construction, default resolution, env-key fallback, agent-id validation, and
 * agent→model namespacing are ALL real code. The only mocked surface is
 * ``global.fetch`` — used to inspect the request the preset would POST (base
 * URL, body.user, session header, Authorization) without touching the network.
 */

import { describe, expect, it, vi, afterEach } from 'vitest';
import { LLM as HermesLLM } from '../src/llm/hermes';
import { LLM as OpenClawLLM } from '../src/llm/openclaw';
import { openclawConsult } from '../src/consult';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
  delete process.env.API_SERVER_MODEL_NAME;
  delete process.env.API_SERVER_KEY;
  delete process.env.OPENCLAW_API_KEY;
});

/** Capture the single fetch a preset issues, returning a 200 + empty body. */
function captureFetch(): { calls: Array<{ url: string; init: RequestInit }> } {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  globalThis.fetch = vi.fn(
    async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), init: init ?? {} });
      return new Response('', { status: 200 });
    },
  ) as unknown as typeof fetch;
  return { calls };
}

async function inspectRequest(
  provider: { stream: (m: Array<Record<string, unknown>>, t?: unknown, o?: { callId?: string }) => AsyncGenerator<unknown> },
  callId?: string,
): Promise<{ url: string; body: Record<string, unknown>; headers: Record<string, string> }> {
  const { calls } = captureFetch();
  for await (const _ of provider.stream(
    [{ role: 'user', content: 'hi' }],
    null,
    callId ? { callId } : undefined,
  )) {
    // drain
  }
  const init = calls[0].init;
  return {
    url: calls[0].url,
    body: JSON.parse(init.body as string) as Record<string, unknown>,
    headers: init.headers as Record<string, string>,
  };
}

describe('[unit] HermesLLM preset', () => {
  it('defaults baseUrl, model, timeout (120 s), the user prefix and X-Hermes-Session-Id header', async () => {
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout');
    const llm = new HermesLLM();
    expect(llm.model).toBe('hermes-agent');
    const { url, body, headers } = await inspectRequest(llm, 'c1');
    expect(url).toBe('http://127.0.0.1:8642/v1/chat/completions');
    expect(body.model).toBe('hermes-agent');
    expect(body.user).toBe('patter-call-c1'); // upstream-log correlation, kept
    // PRIMARY mechanism: per-call session id header, on by default.
    expect(headers['X-Hermes-Session-Id']).toBe('patter-call-c1');
    expect(timeoutSpy).toHaveBeenCalledWith(120_000);
  });

  it('omits X-Hermes-Session-Key by default and emits it only when sessionKey is set', async () => {
    // Default: no memory-scope header (opt-in).
    const { headers: defaultHeaders } = await inspectRequest(new HermesLLM(), 'c1');
    expect(defaultHeaders['X-Hermes-Session-Key']).toBeUndefined();

    // Configured: the static memory-scope header is sent on the wire.
    const scoped = new HermesLLM({ sessionKey: 'mem-123' });
    const { headers: scopedHeaders } = await inspectRequest(scoped, 'c1');
    expect(scopedHeaders['X-Hermes-Session-Key']).toBe('mem-123');
    // Per-call session id still flows alongside the memory scope.
    expect(scopedHeaders['X-Hermes-Session-Id']).toBe('patter-call-c1');
  });

  it('reads the model from API_SERVER_MODEL_NAME, with an explicit model still winning', () => {
    process.env.API_SERVER_MODEL_NAME = 'hermes-7b';
    expect(new HermesLLM().model).toBe('hermes-7b');
    expect(new HermesLLM({ model: 'explicit-model' }).model).toBe('explicit-model');
  });

  it('resolves the bearer from API_SERVER_KEY, and stays keyless when absent', async () => {
    process.env.API_SERVER_KEY = 'hermes-secret';
    const { headers } = await inspectRequest(new HermesLLM());
    expect(headers.Authorization).toBe('Bearer hermes-secret');

    delete process.env.API_SERVER_KEY;
    const { headers: keyless } = await inspectRequest(new HermesLLM());
    expect(keyless.Authorization).toBeUndefined(); // keyless local Hermes
  });
});

describe('[unit] OpenClawLLM preset', () => {
  it('maps a bare agent id to openclaw/<agent>', () => {
    expect(new OpenClawLLM({ agent: 'receptionist' }).model).toBe('openclaw/receptionist');
  });

  it('passes through an already-namespaced agent id unchanged', () => {
    expect(new OpenClawLLM({ agent: 'openclaw/receptionist' }).model).toBe('openclaw/receptionist');
    expect(new OpenClawLLM({ agent: 'agent:receptionist' }).model).toBe('agent:receptionist');
  });

  it('rejects an invalid agent id (charset) with a thrown error', () => {
    expect(() => new OpenClawLLM({ agent: 'a b' })).toThrow(/agent id/i);
    expect(() => new OpenClawLLM({ agent: '' })).toThrow(/agent id/i);
  });

  it('defaults baseUrl :18789, OPENCLAW_API_KEY, the session header and 120 s timeout', async () => {
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout');
    process.env.OPENCLAW_API_KEY = 'oc-operator-secret';
    const llm = new OpenClawLLM({ agent: 'receptionist' });
    const { url, body, headers } = await inspectRequest(llm, 'c2');
    expect(url).toBe('http://127.0.0.1:18789/v1/chat/completions');
    expect(headers.Authorization).toBe('Bearer oc-operator-secret');
    expect(body.user).toBe('patter-call-c2');
    expect(headers['x-openclaw-session-key']).toBe('c2');
    expect(timeoutSpy).toHaveBeenCalledWith(120_000);
  });
});

describe('[unit] OpenClaw LLM ↔ consult preset parity', () => {
  it('shares base URL, api-key env, session header and agent→model mapping with openclawConsult', () => {
    const cfg = openclawConsult('receptionist');
    const oc = cfg.openaiCompatible!;

    // Same loopback base URL.
    const llm = new OpenClawLLM({ agent: 'receptionist' });
    expect(new URL(oc.baseUrl).href).toBe('http://127.0.0.1:18789/v1');

    // Same env var name and session header (the consult preset is the
    // shipped source of truth — both must stay byte-identical).
    expect(oc.apiKeyEnv).toBe('OPENCLAW_API_KEY');
    expect(oc.sessionHeader).toBe('x-openclaw-session-key');

    // Same agent→model namespacing rule.
    expect(oc.model).toBe('openclaw/receptionist');
    expect(llm.model).toBe(oc.model);

    // Already-namespaced ids map identically in both code paths.
    expect(openclawConsult('openclaw/x').openaiCompatible!.model).toBe(
      new OpenClawLLM({ agent: 'openclaw/x' }).model,
    );
  });
});

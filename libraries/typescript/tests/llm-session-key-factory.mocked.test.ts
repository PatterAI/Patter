/**
 * Tests for the per-call session-key factory (Feature #7).
 *
 * A ``sessionKeyFactory`` derives the memory-scope header value per call from a
 * {@link SessionContext} (carrying ``caller`` + its non-reversible
 * {@link hashCaller}). The Hermes convenience ``sessionKeyFrom: 'caller_hash'``
 * installs a default factory that scopes durable memory per caller WITHOUT the
 * raw number ever reaching the wire.
 *
 * Factory resolution, the SessionContext construction, and the caller threading
 * through the REAL ``LLMLoop`` are all real code. The only mocked surface is
 * ``global.fetch`` — used to inspect the request the provider would POST (the
 * X-Hermes-Session-Key header value) without touching the network.
 */

import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  OpenAICompatibleLLMProvider,
  hashCaller,
} from '../src/llm/openai-compatible';
import { LLM as HermesLLM } from '../src/llm/hermes';
import { LLMLoop } from '../src/llm-loop';
import type {
  LLMChunk,
  LLMProvider,
  LLMStreamOptions,
} from '../src/llm-loop';
import type { SessionContext } from '../src/types';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

/** Capture the single fetch a provider issues, returning a 200 + empty body. */
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

async function inspectHeaders(
  provider: {
    stream: (
      m: Array<Record<string, unknown>>,
      t?: unknown,
      o?: LLMStreamOptions,
    ) => AsyncGenerator<unknown>;
  },
  opts?: LLMStreamOptions,
): Promise<Record<string, string>> {
  const { calls } = captureFetch();
  for await (const _ of provider.stream(
    [{ role: 'user', content: 'hi' }],
    null,
    opts,
  )) {
    // drain
  }
  return calls[0].init.headers as Record<string, string>;
}

// ---------------------------------------------------------------------------
// hashCaller — stable, non-reversible, never the raw number
// ---------------------------------------------------------------------------

describe('[unit] hashCaller', () => {
  it('is stable across calls and never the raw number', () => {
    const number = '+15555550100';
    const h1 = hashCaller(number);
    const h2 = hashCaller(number);
    expect(h1).toBe(h2);
    expect(h1).toHaveLength(16);
    expect(h1).toMatch(/^[0-9a-f]{16}$/);
    expect(h1).not.toContain(number);
    expect(h1).not.toBe(number);
  });

  it('distinguishes different callers and returns undefined for empty input', () => {
    expect(hashCaller('+15555550100')).not.toBe(hashCaller('+15555550101'));
    expect(hashCaller(undefined)).toBeUndefined();
    expect(hashCaller('')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Factory precedence on the generic provider — observed on the wire
// ---------------------------------------------------------------------------

describe('[mocked] sessionKeyFactory on OpenAICompatibleLLMProvider', () => {
  it('factory overrides the static sessionKey and sees the caller hash', async () => {
    let seen: SessionContext | undefined;
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionKeyHeader: 'X-Mem',
      sessionKey: 'static-key', // must be overridden
      sessionKeyFactory: (ctx) => {
        seen = ctx;
        return `scope-${ctx.callerHash}`;
      },
    });

    const headers = await inspectHeaders(provider, {
      callId: 'c1',
      caller: '+15555550100',
      callee: '+15555550101',
    });

    const expectedHash = hashCaller('+15555550100');
    expect(headers['X-Mem']).toBe(`scope-${expectedHash}`);
    // The factory saw the full context; the EMITTED value carries only the hash.
    expect(seen?.callId).toBe('c1');
    expect(seen?.caller).toBe('+15555550100');
    expect(seen?.callee).toBe('+15555550101');
    expect(seen?.callerHash).toBe(expectedHash);
  });

  it('factory returning undefined omits the header', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionKeyHeader: 'X-Mem',
      sessionKey: 'static-key',
      sessionKeyFactory: () => undefined,
    });

    const headers = await inspectHeaders(provider, {
      callId: 'c1',
      caller: '+15555550100',
    });
    expect(headers['X-Mem']).toBeUndefined();
  });

  it('static sessionKey is used when no factory is configured', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionKeyHeader: 'X-Mem',
      sessionKey: 'static-key',
    });

    const headers = await inspectHeaders(provider, {
      callId: 'c1',
      caller: '+15555550100',
    });
    expect(headers['X-Mem']).toBe('static-key');
  });

  it('factory fires even without a callId (keys off the caller hash alone)', async () => {
    const provider = new OpenAICompatibleLLMProvider({
      baseUrl: 'http://127.0.0.1:9/v1',
      model: 'm',
      sessionKeyHeader: 'X-Mem',
      sessionKeyFactory: (ctx) => `caller-${ctx.callerHash}`,
    });

    const headers = await inspectHeaders(provider, { caller: '+15555550100' });
    expect(headers['X-Mem']).toBe(`caller-${hashCaller('+15555550100')}`);
  });
});

// ---------------------------------------------------------------------------
// Hermes convenience: sessionKeyFrom: 'caller_hash'
// ---------------------------------------------------------------------------

describe('[mocked] HermesLLM sessionKeyFrom convenience', () => {
  it('caller_hash derives patter-caller-<hash> on the wire (raw number absent)', async () => {
    const llm = new HermesLLM({ sessionKeyFrom: 'caller_hash' });
    const caller = '+15555550100';
    const headers = await inspectHeaders(llm, { callId: 'hid-1', caller });

    const expected = `patter-caller-${hashCaller(caller)}`;
    expect(headers['X-Hermes-Session-Key']).toBe(expected);
    // The raw number is never in the memory-scope header value.
    expect(headers['X-Hermes-Session-Key']).not.toContain(caller);
    // Per-call session id still flows alongside the memory scope.
    expect(headers['X-Hermes-Session-Id']).toBe('patter-call-hid-1');
  });

  it('omits the memory-scope header when there is no caller', async () => {
    const llm = new HermesLLM({ sessionKeyFrom: 'caller_hash' });
    const headers = await inspectHeaders(llm, { callId: 'hid-1' });
    expect(headers['X-Hermes-Session-Key']).toBeUndefined();
    expect(headers['X-Hermes-Session-Id']).toBe('patter-call-hid-1');
  });

  it('an explicit sessionKeyFactory wins over sessionKeyFrom', async () => {
    const llm = new HermesLLM({
      sessionKeyFrom: 'caller_hash',
      sessionKeyFactory: () => 'custom-scope',
    });
    const headers = await inspectHeaders(llm, {
      callId: 'hid-1',
      caller: '+15555550100',
    });
    expect(headers['X-Hermes-Session-Key']).toBe('custom-scope');
  });

  it('rejects an unknown sessionKeyFrom at runtime (parity with Python ValueError)', () => {
    // A non-TypeScript / dynamic-JS caller can pass an invalid value the literal
    // type would reject only at compile time; it must fail loudly, not silently.
    expect(
      () => new HermesLLM({ sessionKeyFrom: 'bogus' as unknown as 'caller_hash' }),
    ).toThrow(/caller_hash/);
  });
});

// ---------------------------------------------------------------------------
// Caller threads through the REAL LLMLoop into the provider's stream()
// ---------------------------------------------------------------------------

/** Records the stream options it received and yields a single text chunk. */
class RecordingProvider implements LLMProvider {
  static readonly providerKey = 'recording';
  public lastOpts: LLMStreamOptions | undefined;

  async *stream(
    _messages: Array<Record<string, unknown>>,
    _tools?: Array<Record<string, unknown>> | null,
    opts?: LLMStreamOptions,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    this.lastOpts = opts;
    yield { type: 'text', content: 'ok' };
  }
}

async function drain(
  gen: AsyncGenerator<string, void, unknown>,
): Promise<string> {
  let out = '';
  for await (const tok of gen) out += tok;
  return out;
}

describe('[unit] LLMLoop threads caller/callee into stream opts', () => {
  it('forwards caller and callee from call_context into provider.stream opts', async () => {
    const provider = new RecordingProvider();
    const loop = new LLMLoop('', 'm', 'be helpful', null, provider);

    const out = await drain(
      loop.run('hi', [], {
        call_id: 'xyz',
        caller: '+15555550100',
        callee: '+15555550101',
      }),
    );

    expect(out).toBe('ok');
    expect(provider.lastOpts?.callId).toBe('xyz');
    expect(provider.lastOpts?.caller).toBe('+15555550100');
    expect(provider.lastOpts?.callee).toBe('+15555550101');
  });

  it('leaves opts untouched when no call context is present', async () => {
    const provider = new RecordingProvider();
    const loop = new LLMLoop('', 'm', 'be helpful', null, provider);

    await drain(loop.run('hi', [], {}));

    expect(provider.lastOpts?.callId).toBeUndefined();
    expect(provider.lastOpts?.caller).toBeUndefined();
    expect(provider.lastOpts?.callee).toBeUndefined();
  });

  it('end-to-end: a Hermes caller_hash key reaches the wire via the real loop', async () => {
    const llm = new HermesLLM({ sessionKeyFrom: 'caller_hash' });
    const loop = new LLMLoop('', 'hermes-agent', 'be helpful', null, llm);

    const { calls } = captureFetch();
    const caller = '+15555550100';
    await drain(loop.run('hi', [], { call_id: 'c9', caller, callee: '+15555550101' }));

    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers['X-Hermes-Session-Key']).toBe(
      `patter-caller-${hashCaller(caller)}`,
    );
  });
});

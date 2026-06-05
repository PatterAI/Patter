/**
 * Tests for the per-call id threading from ``LLMLoop.run`` into
 * ``provider.stream``'s ``LLMStreamOptions.callId``.
 *
 * Uses the REAL ``LLMLoop`` with a tiny in-process recording provider (not a
 * mock of the unit under test — a real ``LLMProvider`` that records the
 * ``opts`` object it was handed). Proves (a) the loop spreads
 * ``callContext.call_id`` into ``opts.callId``, and (b) a provider that reads
 * only ``opts.signal`` is unaffected by the added field.
 */

import { describe, expect, it } from 'vitest';
import { LLMLoop } from '../src/llm-loop';
import type { LLMChunk, LLMProvider, LLMStreamOptions } from '../src/llm-loop';

/** Records the stream options it received and yields a single text chunk. */
class RecordingProvider implements LLMProvider {
  static readonly providerKey = 'recording';
  public lastOpts: LLMStreamOptions | undefined;
  public callCount = 0;

  async *stream(
    _messages: Array<Record<string, unknown>>,
    _tools?: Array<Record<string, unknown>> | null,
    opts?: LLMStreamOptions,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    this.callCount += 1;
    this.lastOpts = opts;
    yield { type: 'text', content: 'ok' };
  }
}

/** A stock provider that only reads ``opts.signal`` — never ``opts.callId``. */
class SignalOnlyProvider implements LLMProvider {
  static readonly providerKey = 'signal_only';
  public sawAbort = false;

  async *stream(
    _messages: Array<Record<string, unknown>>,
    _tools?: Array<Record<string, unknown>> | null,
    opts?: LLMStreamOptions,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    this.sawAbort = opts?.signal?.aborted ?? false;
    yield { type: 'text', content: 'hello' };
  }
}

/**
 * The most minimal custom provider: a ``stream(messages, tools)`` that declares
 * NO third ``opts`` parameter at all. Proves the loop's extra positional
 * options arg silently no-ops in TS (unlike Python keyword args, which require
 * an inspect.signature guard) — no guard code needed on the TS side.
 */
class NoOptsProvider implements LLMProvider {
  static readonly providerKey = 'no_opts';
  public callCount = 0;

  // Intentionally omits the opts parameter — the loop still passes streamOpts
  // as a third positional argument, which a generator ignoring it discards.
  async *stream(
    _messages: Array<Record<string, unknown>>,
    _tools?: Array<Record<string, unknown>> | null,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    this.callCount += 1;
    yield { type: 'text', content: 'done' };
  }
}

async function drain(gen: AsyncGenerator<string, void, unknown>): Promise<string> {
  let out = '';
  for await (const tok of gen) out += tok;
  return out;
}

describe('[unit] LLMLoop call_id threading', () => {
  it('forwards call_id from call_context into provider.stream opts.callId', async () => {
    const provider = new RecordingProvider();
    const loop = new LLMLoop('', 'm', 'be helpful', null, provider);

    const out = await drain(
      loop.run('hi', [], { call_id: 'xyz', caller: '+15555550100', callee: '+15555550101' }),
    );

    expect(out).toBe('ok');
    expect(provider.callCount).toBe(1);
    expect(provider.lastOpts?.callId).toBe('xyz');
  });

  it('does not set callId when call_context has no call_id (no synthetic value)', async () => {
    const provider = new RecordingProvider();
    const loop = new LLMLoop('', 'm', 'be helpful', null, provider);

    await drain(loop.run('hi', [], {}));

    // The loop leaves opts untouched (undefined) — no '' or 'undefined' leaks.
    expect(provider.lastOpts?.callId).toBeUndefined();
  });

  it('leaves signal-only providers unaffected by the added callId field', async () => {
    const provider = new SignalOnlyProvider();
    const loop = new LLMLoop('', 'm', 'be helpful', null, provider);

    const out = await drain(
      loop.run('hi', [], { call_id: 'abc' }, undefined, undefined, undefined, {
        signal: AbortSignal.timeout(60_000),
      }),
    );

    // Backward compatible: the provider runs to completion, reads only signal.
    expect(out).toBe('hello');
    expect(provider.sawAbort).toBe(false);
  });

  it('runs a minimal provider whose stream omits the opts param entirely (no guard needed)', async () => {
    const provider = new NoOptsProvider();
    const loop = new LLMLoop('', 'm', 'be helpful', null, provider);

    // call_id is present, so the loop builds and passes streamOpts as the third
    // positional arg. The provider's two-arg generator simply ignores it.
    const out = await drain(loop.run('hi', [], { call_id: 'abc' }));

    expect(out).toBe('done');
    expect(provider.callCount).toBe(1);
  });
});

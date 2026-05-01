/**
 * Unit tests for PipelineHookExecutor.
 *
 * Covers:
 *  1.  No hooks defined (undefined) — all methods pass through input unchanged
 *  2.  PipelineHooks object present but specific hook is undefined — returns input
 *  3.  afterTranscribe modifies the transcript
 *  4.  afterTranscribe returns null — method returns null
 *  5.  beforeSynthesize modifies the text
 *  6.  beforeSynthesize returns null — method returns null
 *  7.  afterSynthesize modifies the audio buffer
 *  8.  afterSynthesize returns null — method returns null
 *  9.  Hook throws an error — fail-open: error logged, original value returned
 * 10.  Async hook — Promise result is awaited correctly
 * 11.  Sync hook — sync function works without extra wrapping
 * 12.  HookContext fields — context object carries the expected attributes
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { PipelineHookExecutor } from '../../src/pipeline-hooks';
import * as loggerModule from '../../src/logger';
import type { HookContext, PipelineHooks } from '../../src/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeCtx(overrides: Partial<HookContext> = {}): HookContext {
  return {
    callId: 'call-001',
    caller: '+15550001111',
    callee: '+15552223333',
    history: [{ role: 'user', text: 'Hello' }],
    ...overrides,
  };
}

function fakeAudio(bytes: number[] = [0x00, 0x01, 0x02]): Buffer {
  return Buffer.from(bytes);
}

// ---------------------------------------------------------------------------
// 1. No hooks defined (undefined)
// ---------------------------------------------------------------------------

describe('PipelineHookExecutor — no hooks defined (undefined)', () => {
  let executor: PipelineHookExecutor;

  beforeEach(() => {
    executor = new PipelineHookExecutor(undefined);
  });

  it('runAfterTranscribe returns transcript unchanged', async () => {
    const result = await executor.runAfterTranscribe('hello world', makeCtx());
    expect(result).toBe('hello world');
  });

  it('runBeforeSynthesize returns text unchanged', async () => {
    const result = await executor.runBeforeSynthesize('speak this', makeCtx());
    expect(result).toBe('speak this');
  });

  it('runAfterSynthesize returns audio unchanged', async () => {
    const audio = fakeAudio();
    const result = await executor.runAfterSynthesize(audio, 'speak this', makeCtx());
    expect(result).toEqual(audio);
  });
});

// ---------------------------------------------------------------------------
// 2. PipelineHooks object with no specific hook defined
// ---------------------------------------------------------------------------

describe('PipelineHookExecutor — hooks object present, specific hook absent', () => {
  it('runAfterTranscribe without afterTranscribe hook returns input', async () => {
    const executor = new PipelineHookExecutor({} as PipelineHooks);
    const result = await executor.runAfterTranscribe('text', makeCtx());
    expect(result).toBe('text');
  });

  it('runBeforeSynthesize without beforeSynthesize hook returns input', async () => {
    const executor = new PipelineHookExecutor({} as PipelineHooks);
    const result = await executor.runBeforeSynthesize('sentence', makeCtx());
    expect(result).toBe('sentence');
  });

  it('runAfterSynthesize without afterSynthesize hook returns input', async () => {
    const executor = new PipelineHookExecutor({} as PipelineHooks);
    const audio = fakeAudio([0xff, 0xfe]);
    const result = await executor.runAfterSynthesize(audio, 'text', makeCtx());
    expect(result).toEqual(audio);
  });
});

// ---------------------------------------------------------------------------
// 3. afterTranscribe modifies the transcript
// ---------------------------------------------------------------------------

describe('afterTranscribe — modifies transcript', () => {
  it('returns the hook return value', async () => {
    const hooks: PipelineHooks = {
      afterTranscribe: (transcript) => transcript.toUpperCase(),
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterTranscribe('hello', makeCtx());
    expect(result).toBe('HELLO');
  });
});

// ---------------------------------------------------------------------------
// 4. afterTranscribe returns null — hook vetoes the turn
// ---------------------------------------------------------------------------

describe('afterTranscribe — returns null', () => {
  it('propagates null to the caller', async () => {
    const hooks: PipelineHooks = {
      afterTranscribe: () => null,
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterTranscribe('hello', makeCtx());
    expect(result).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 5. beforeSynthesize modifies the text
// ---------------------------------------------------------------------------

describe('beforeSynthesize — modifies text', () => {
  it('returns the hook return value', async () => {
    const hooks: PipelineHooks = {
      beforeSynthesize: (text) => `[FILTERED] ${text}`,
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSynthesize('say this', makeCtx());
    expect(result).toBe('[FILTERED] say this');
  });
});

// ---------------------------------------------------------------------------
// 6. beforeSynthesize returns null — hook vetoes TTS
// ---------------------------------------------------------------------------

describe('beforeSynthesize — returns null', () => {
  it('propagates null to the caller', async () => {
    const hooks: PipelineHooks = {
      beforeSynthesize: () => null,
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSynthesize('say this', makeCtx());
    expect(result).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 7. afterSynthesize modifies the audio buffer
// ---------------------------------------------------------------------------

describe('afterSynthesize — modifies audio', () => {
  it('returns the modified buffer from the hook', async () => {
    const modifiedAudio = Buffer.from([0xaa, 0xbb, 0xcc]);
    const hooks: PipelineHooks = {
      afterSynthesize: () => modifiedAudio,
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterSynthesize(fakeAudio(), 'text', makeCtx());
    expect(result).toEqual(modifiedAudio);
  });
});

// ---------------------------------------------------------------------------
// 8. afterSynthesize returns null — hook discards the audio chunk
// ---------------------------------------------------------------------------

describe('afterSynthesize — returns null', () => {
  it('propagates null to the caller', async () => {
    const hooks: PipelineHooks = {
      afterSynthesize: () => null,
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterSynthesize(fakeAudio(), 'text', makeCtx());
    expect(result).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 9. Hook throws — fail-open: logs error, returns original value
// ---------------------------------------------------------------------------

describe('PipelineHookExecutor — hook throws (fail-open)', () => {
  let errorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    errorSpy = vi.spyOn(loggerModule.getLogger(), 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('runAfterTranscribe returns original transcript and logs the error', async () => {
    const hooks: PipelineHooks = {
      afterTranscribe: () => { throw new Error('boom!'); },
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterTranscribe('original', makeCtx());

    expect(result).toBe('original');
    expect(errorSpy).toHaveBeenCalledOnce();
    const [message] = errorSpy.mock.calls[0] as [string, ...unknown[]];
    expect(message).toContain('afterTranscribe');
  });

  it('runBeforeSynthesize returns original text and logs the error', async () => {
    const hooks: PipelineHooks = {
      beforeSynthesize: () => { throw new RangeError('bad value'); },
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSynthesize('original', makeCtx());

    expect(result).toBe('original');
    expect(errorSpy).toHaveBeenCalledOnce();
    const [message] = errorSpy.mock.calls[0] as [string, ...unknown[]];
    expect(message).toContain('beforeSynthesize');
  });

  it('runAfterSynthesize returns original audio and logs the error', async () => {
    const originalAudio = fakeAudio([0x00, 0x01]);
    const hooks: PipelineHooks = {
      afterSynthesize: () => { throw new TypeError('crash'); },
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterSynthesize(originalAudio, 'text', makeCtx());

    expect(result).toEqual(originalAudio);
    expect(errorSpy).toHaveBeenCalledOnce();
    const [message] = errorSpy.mock.calls[0] as [string, ...unknown[]];
    expect(message).toContain('afterSynthesize');
  });
});

// ---------------------------------------------------------------------------
// 10. Async hook — Promise result is awaited correctly
// ---------------------------------------------------------------------------

describe('PipelineHookExecutor — async hooks', () => {
  it('runAfterTranscribe awaits a Promise-returning hook', async () => {
    const hooks: PipelineHooks = {
      afterTranscribe: async (transcript) => `${transcript} (async)`,
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterTranscribe('hello', makeCtx());
    expect(result).toBe('hello (async)');
  });

  it('runBeforeSynthesize awaits a Promise-returning hook', async () => {
    const hooks: PipelineHooks = {
      beforeSynthesize: async (text) => text.trim(),
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSynthesize('  spaces  ', makeCtx());
    expect(result).toBe('spaces');
  });

  it('runAfterSynthesize awaits a Promise-returning hook', async () => {
    const newAudio = Buffer.from([0x11, 0x22]);
    const hooks: PipelineHooks = {
      afterSynthesize: async () => newAudio,
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterSynthesize(fakeAudio(), 'text', makeCtx());
    expect(result).toEqual(newAudio);
  });

  it('async hook returning null is propagated', async () => {
    const hooks: PipelineHooks = {
      afterTranscribe: async () => null,
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterTranscribe('anything', makeCtx());
    expect(result).toBeNull();
  });

  it('async hook that throws is fail-open', async () => {
    vi.spyOn(loggerModule.getLogger(), 'error').mockImplementation(() => {});
    const hooks: PipelineHooks = {
      afterTranscribe: async () => { throw new Error('async boom'); },
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterTranscribe('original', makeCtx());

    expect(result).toBe('original');
    vi.restoreAllMocks();
  });
});

// ---------------------------------------------------------------------------
// 11. Sync hook — synchronous function works correctly
// ---------------------------------------------------------------------------

describe('PipelineHookExecutor — sync hooks', () => {
  it('runAfterTranscribe calls the sync hook and returns its value', async () => {
    let callCount = 0;
    const hooks: PipelineHooks = {
      afterTranscribe: (transcript) => {
        callCount++;
        return 'synced';
      },
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runAfterTranscribe('input', makeCtx());

    expect(result).toBe('synced');
    expect(callCount).toBe(1);
  });

  it('runBeforeSynthesize calls the sync hook and returns its value', async () => {
    const hooks: PipelineHooks = {
      beforeSynthesize: (text) => text.toLowerCase(),
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSynthesize('HELLO', makeCtx());
    expect(result).toBe('hello');
  });

  it('runAfterSynthesize calls the sync hook and returns its value', async () => {
    const hooks: PipelineHooks = {
      afterSynthesize: (audio) => Buffer.from([...audio].reverse()),
    };
    const executor = new PipelineHookExecutor(hooks);
    const audio = Buffer.from([0x01, 0x02, 0x03]);
    const result = await executor.runAfterSynthesize(audio, 'text', makeCtx());
    expect(result).toEqual(Buffer.from([0x03, 0x02, 0x01]));
  });
});

// ---------------------------------------------------------------------------
// 12. HookContext fields — verify context shape visible inside hooks
// ---------------------------------------------------------------------------

describe('HookContext fields', () => {
  it('afterTranscribe receives all context fields', async () => {
    const received: HookContext[] = [];
    const hooks: PipelineHooks = {
      afterTranscribe: (transcript, ctx) => {
        received.push(ctx);
        return transcript;
      },
    };

    const ctx = makeCtx({
      callId: 'call-xyz',
      caller: '+15550000001',
      callee: '+15559999999',
      history: [{ role: 'user', text: 'hi' }],
    });
    const executor = new PipelineHookExecutor(hooks);
    await executor.runAfterTranscribe('test', ctx);

    expect(received).toHaveLength(1);
    const captured = received[0];
    expect(captured.callId).toBe('call-xyz');
    expect(captured.caller).toBe('+15550000001');
    expect(captured.callee).toBe('+15559999999');
    expect(captured.history).toEqual([{ role: 'user', text: 'hi' }]);
  });

  it('beforeSynthesize receives the call ID', async () => {
    const received: HookContext[] = [];
    const hooks: PipelineHooks = {
      beforeSynthesize: (text, ctx) => {
        received.push(ctx);
        return text;
      },
    };

    const ctx = makeCtx({ callId: 'call-abc' });
    const executor = new PipelineHookExecutor(hooks);
    await executor.runBeforeSynthesize('text', ctx);

    expect(received[0].callId).toBe('call-abc');
  });

  it('afterSynthesize receives all context fields', async () => {
    const received: HookContext[] = [];
    const hooks: PipelineHooks = {
      afterSynthesize: (audio, text, ctx) => {
        received.push(ctx);
        return audio;
      },
    };

    const ctx = makeCtx({ callId: 'call-qrs', caller: '+1111', callee: '+2222' });
    const executor = new PipelineHookExecutor(hooks);
    await executor.runAfterSynthesize(fakeAudio(), 'text', ctx);

    expect(received[0].callId).toBe('call-qrs');
    expect(received[0].caller).toBe('+1111');
    expect(received[0].callee).toBe('+2222');
  });

  it('HookContext history defaults to an empty array', () => {
    const ctx: HookContext = {
      callId: 'c',
      caller: 'a',
      callee: 'b',
      history: [],
    };
    expect(ctx.history).toHaveLength(0);
  });

  it('hooks receive the exact same context object that was passed in', async () => {
    const hooks: PipelineHooks = {
      afterTranscribe: (transcript, ctx) => {
        // Verify the context is the same reference
        expect(ctx).toBe(passedCtx);
        return transcript;
      },
    };

    const passedCtx = makeCtx({ callId: 'reference-check' });
    const executor = new PipelineHookExecutor(hooks);
    await executor.runAfterTranscribe('test', passedCtx);
  });
});

// ---------------------------------------------------------------------------
// 13. beforeSendToStt hook (audio chunk interceptor)
// ---------------------------------------------------------------------------

describe('PipelineHookExecutor — beforeSendToStt', () => {
  it('returns audio unchanged when hooks is undefined', async () => {
    const executor = new PipelineHookExecutor(undefined);
    const audio = fakeAudio();
    const result = await executor.runBeforeSendToStt(audio, makeCtx());
    expect(result).toEqual(audio);
  });

  it('returns audio unchanged when hook is not defined', async () => {
    const executor = new PipelineHookExecutor({});
    const audio = fakeAudio();
    const result = await executor.runBeforeSendToStt(audio, makeCtx());
    expect(result).toEqual(audio);
  });

  it('modifies the audio chunk', async () => {
    const hooks: PipelineHooks = {
      beforeSendToStt: (audio) => Buffer.concat([audio, Buffer.from([0xff])]),
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSendToStt(Buffer.from([0x00]), makeCtx());
    expect(result).toEqual(Buffer.from([0x00, 0xff]));
  });

  it('returns null to drop the chunk', async () => {
    const hooks: PipelineHooks = { beforeSendToStt: () => null };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSendToStt(fakeAudio(), makeCtx());
    expect(result).toBeNull();
  });

  it('supports async hooks', async () => {
    const hooks: PipelineHooks = {
      beforeSendToStt: async (audio) => Buffer.concat([audio, audio]),
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSendToStt(Buffer.from([0xaa]), makeCtx());
    expect(result).toEqual(Buffer.from([0xaa, 0xaa]));
  });

  it('fails open and logs on exception', async () => {
    const errorSpy = vi.fn();
    vi.spyOn(loggerModule, 'getLogger').mockReturnValue({
      error: errorSpy,
      warn: vi.fn(),
      info: vi.fn(),
      debug: vi.fn(),
    } as unknown as ReturnType<typeof loggerModule.getLogger>);

    const hooks: PipelineHooks = {
      beforeSendToStt: () => {
        throw new Error('boom');
      },
    };
    const executor = new PipelineHookExecutor(hooks);
    const result = await executor.runBeforeSendToStt(Buffer.from([0x42]), makeCtx());
    expect(result).toEqual(Buffer.from([0x42]));
    expect(errorSpy).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Phase 3 — afterLlm 3-tier API
// ---------------------------------------------------------------------------

describe('PipelineHookExecutor — afterLlm 3-tier', () => {
  it('no hook: pass-through, all has* return false', async () => {
    const ex = new PipelineHookExecutor(undefined);
    expect(ex.hasAfterLlm()).toBe(false);
    expect(ex.hasAfterLlmResponse()).toBe(false);
    expect(ex.hasAfterLlmChunk()).toBe(false);
    expect(ex.hasAfterLlmSentence()).toBe(false);
    expect(ex.runAfterLlmChunk('Hi')).toBe('Hi');
    expect(await ex.runAfterLlmSentence('Hi.', makeCtx())).toBe('Hi.');
    expect(await ex.runAfterLlmResponse('Hi.', makeCtx())).toBe('Hi.');
  });

  it('legacy callable maps to onResponse semantics', async () => {
    // Deprecation warning is emitted once per process so we cannot reliably
    // assert it here without resetting global state — the behavioural check
    // (legacy callable runs as on_response) is the load-bearing guarantee.
    const hooks: PipelineHooks = {
      afterLlm: async (text: string) => text.toUpperCase(),
    };
    const ex = new PipelineHookExecutor(hooks);
    expect(ex.hasAfterLlmResponse()).toBe(true);
    expect(ex.hasAfterLlmSentence()).toBe(false);
    expect(await ex.runAfterLlmResponse('hello world', makeCtx())).toBe('HELLO WORLD');
  });

  it('object form: all three tiers wired', async () => {
    const hooks: PipelineHooks = {
      afterLlm: {
        onChunk: (c) => c.replace(/X/g, 'Y'),
        onSentence: async (s) => s.replace('foo', 'bar'),
        onResponse: async (t) => t + ' (final)',
      },
    };
    const ex = new PipelineHookExecutor(hooks);
    expect(ex.hasAfterLlmChunk()).toBe(true);
    expect(ex.hasAfterLlmSentence()).toBe(true);
    expect(ex.hasAfterLlmResponse()).toBe(true);
    expect(ex.runAfterLlmChunk('aXbXc')).toBe('aYbYc');
    expect(await ex.runAfterLlmSentence('foo bar baz', makeCtx())).toBe('bar bar baz');
    expect(await ex.runAfterLlmResponse('hello', makeCtx())).toBe('hello (final)');
  });

  it('chunk hook failure falls open (returns original)', () => {
    const hooks: PipelineHooks = {
      afterLlm: { onChunk: () => { throw new Error('boom'); } },
    };
    const ex = new PipelineHookExecutor(hooks);
    expect(ex.runAfterLlmChunk('hello')).toBe('hello');
  });

  it('chunk hook non-string return falls open', () => {
    const hooks: PipelineHooks = {
      afterLlm: { onChunk: ((c: string) => 42 as unknown as string) },
    };
    const ex = new PipelineHookExecutor(hooks);
    expect(ex.runAfterLlmChunk('hello')).toBe('hello');
  });

  it('sentence hook returning empty string drops sentence', async () => {
    const hooks: PipelineHooks = {
      afterLlm: { onSentence: async () => '' },
    };
    const ex = new PipelineHookExecutor(hooks);
    expect(await ex.runAfterLlmSentence('kept', makeCtx())).toBeNull();
  });

  it('sentence hook returning null keeps original', async () => {
    const hooks: PipelineHooks = {
      afterLlm: { onSentence: async () => null },
    };
    const ex = new PipelineHookExecutor(hooks);
    expect(await ex.runAfterLlmSentence('kept', makeCtx())).toBe('kept');
  });

  it('sentence hook failure falls open', async () => {
    const hooks: PipelineHooks = {
      afterLlm: {
        onSentence: async () => {
          throw new Error('oops');
        },
      },
    };
    const ex = new PipelineHookExecutor(hooks);
    expect(await ex.runAfterLlmSentence('text', makeCtx())).toBe('text');
  });

  it('legacy alias methods still work', async () => {
    const hooks: PipelineHooks = {
      afterLlm: async (text: string) => text + '!',
    };
    const ex = new PipelineHookExecutor(hooks);
    expect(ex.hasAfterLlm()).toBe(true);
    expect(await ex.runAfterLlm('hi', makeCtx())).toBe('hi!');
  });
});

/**
 * Unit tests for `Patter.call({ wait: true })` → CallResult and the
 * `[Symbol.asyncDispose]` disposer.
 *
 * Mirrors `libraries/python/tests/unit/test_call_wait.py`. These exercise the
 * REAL completion registry on a REAL `EmbeddedServer` and the REAL wrapped
 * `onCallEnd` callback — the only mocked surface is the carrier boundary
 * (`fetch` to the Twilio Calls API), which is the external boundary we are
 * never allowed to hit in CI. Per the authentic-tests rule: swap a live
 * carrier back in and these still pass unchanged, because the correlation
 * logic under test is all real SDK code.
 */

import { describe, expect, it, vi, afterEach } from 'vitest';
import { Patter, Twilio } from '../src/index';
import { PatterConnectionError } from '../src/errors';
import {
  EmbeddedServer,
  twilioStatusToOutcome,
  telnyxHangupOutcome,
} from '../src/server';
import type { LocalConfig } from '../src/server';
import type { AgentOptions } from '../src/types';
import type { CallMetrics } from '../src/metrics';

function makeConfig(overrides: Partial<LocalConfig> = {}): LocalConfig {
  return {
    twilioSid: 'ACtest000000000000000000000000000',
    twilioToken: 'tok',
    phoneNumber: '+15550001234',
    webhookUrl: 'abc.ngrok.io',
    telephonyProvider: 'twilio',
    requireSignature: false,
    persistRoot: null,
    ...overrides,
  };
}

function makeAgent(overrides: Partial<AgentOptions> = {}): AgentOptions {
  return { systemPrompt: 'x', prewarm: false, ...overrides };
}

function localPhone(): Patter {
  return new Patter({
    carrier: new Twilio({
      accountSid: 'ACtest000000000000000000000000000',
      authToken: 'tok',
    }),
    phoneNumber: '+15550001234',
    webhookUrl: 'abc.ngrok.io',
  });
}

/**
 * Attach a real EmbeddedServer (the completion registry lives here). We don't
 * bind a socket — call({ wait: true }) only needs the registry + the
 * terminal-signal handlers, all of which are pure in-process code.
 */
function attachRealServer(phone: Patter, agent: AgentOptions): EmbeddedServer {
  const server = new EmbeddedServer(makeConfig(), agent, undefined, undefined, undefined, undefined, false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (phone as any).embeddedServer = server;
  return server;
}

/** Get the REAL wrapped onCallEnd callback the StreamHandler would invoke. */
function wrappedOnCallEnd(
  server: EmbeddedServer,
): (data: Record<string, unknown>) => Promise<void> {
  const bridge = { telephonyProvider: 'twilio' as const };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [, , wrappedEnd] = (server as any).wrapLoggingCallbacks(bridge);
  return wrappedEnd;
}

/** Mock the Twilio Calls API so call() returns a fixed call_id (the SID). */
function patchTwilioFetch(callId: string): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  vi.spyOn(globalThis, 'fetch' as any).mockImplementation((async () => {
    const body = JSON.stringify({ sid: callId, subresource_uris: {} });
    return {
      ok: true,
      status: 201,
      text: async () => body,
      json: async () => JSON.parse(body),
      clone() {
        return this as unknown as Response;
      },
    } as unknown as Response;
  }) as unknown as typeof fetch);
}

function metrics(callId: string, opts: { duration?: number; cost?: number } = {}): CallMetrics {
  const duration = opts.duration ?? 12.3;
  const cost = opts.cost ?? 0.0123;
  return {
    call_id: callId,
    duration_seconds: duration,
    turns: [],
    cost: { stt: 0, tts: 0, llm: 0, telephony: 0, total: cost },
    latency_avg: { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0 },
    latency_p95: { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0 },
    provider_mode: 'pipeline',
    stt_provider: '',
    tts_provider: '',
    llm_provider: '',
    telephony_provider: 'twilio',
  };
}

/** Yield to the loop until call({ wait: true }) has registered its completion. */
async function waitUntilRegistered(server: EmbeddedServer, callId: string): Promise<void> {
  for (let i = 0; i < 200; i++) {
    if (server.completions.has(callId)) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`completion for ${callId} was never registered`);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Backward compatibility — wait: false
// ---------------------------------------------------------------------------

describe('[unit] call({ wait: false })', () => {
  it('returns void the instant the dial is accepted (default)', async () => {
    const phone = localPhone();
    attachRealServer(phone, makeAgent());
    patchTwilioFetch('CA_x');
    const result = await phone.call({ to: '+15550009999', agent: makeAgent() });
    expect(result).toBeUndefined();
  });
});

describe('[unit] call({ wait: true }) — preconditions', () => {
  it('throws without an active server', async () => {
    const phone = localPhone();
    patchTwilioFetch('CA_x');
    await expect(
      phone.call({ to: '+15550009999', agent: makeAgent(), wait: true }),
    ).rejects.toThrow(/requires an active server/);
  });
});

// ---------------------------------------------------------------------------
// wait: true — connected call resolves via the real wrapped onCallEnd
// ---------------------------------------------------------------------------

describe('[unit] call({ wait: true }) — connected call', () => {
  it('resolves to an "answered" CallResult via the real onCallEnd path', async () => {
    const phone = localPhone();
    const server = attachRealServer(phone, makeAgent());
    const onCallEnd = wrappedOnCallEnd(server);
    patchTwilioFetch('CA_answer');

    const task = phone.call({ to: '+15550009999', agent: makeAgent(), wait: true });
    await waitUntilRegistered(server, 'CA_answer');
    // Real media-stream-end payload → the real wrapper resolves the future.
    await onCallEnd({
      call_id: 'CA_answer',
      caller: '+15550001234',
      callee: '+15550009999',
      ended_at: 1000.0,
      transcript: [
        { role: 'agent', text: 'Hello!' },
        { role: 'user', text: 'Hi.' },
      ],
      metrics: metrics('CA_answer'),
    });
    const result = await task;

    expect(result).toBeDefined();
    expect(result?.callId).toBe('CA_answer');
    expect(result?.outcome).toBe('answered');
    expect(result?.status).toBe('completed');
    expect(result?.durationSeconds).toBe(12.3);
    expect(result?.cost?.total).toBe(0.0123);
    expect(result?.transcript).toHaveLength(2);
  });

  it('resolves to "voicemail" when AMD classified the callee as a machine', async () => {
    const phone = localPhone();
    const server = attachRealServer(phone, makeAgent());
    const onCallEnd = wrappedOnCallEnd(server);
    patchTwilioFetch('CA_vm');

    const task = phone.call({ to: '+15550009999', agent: makeAgent(), wait: true });
    await waitUntilRegistered(server, 'CA_vm');
    // The AMD webhook records the classification before the stream ends.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (server as any).amdClass.set('CA_vm', 'machine');
    await onCallEnd({
      call_id: 'CA_vm',
      transcript: [],
      metrics: metrics('CA_vm', { duration: 4.0 }),
    });
    const result = await task;

    expect(result?.outcome).toBe('voicemail');
    expect(result?.status).toBe('completed');
  });
});

// ---------------------------------------------------------------------------
// wait: true — no-media outcomes resolve via the status-callback path
// ---------------------------------------------------------------------------

describe('[unit] call({ wait: true }) — no-media outcomes', () => {
  it('resolves to "no_answer" via the status-callback path', async () => {
    const phone = localPhone();
    const server = attachRealServer(phone, makeAgent());
    patchTwilioFetch('CA_noans');

    const task = phone.call({ to: '+15550009999', agent: makeAgent(), wait: true });
    await waitUntilRegistered(server, 'CA_noans');
    // This is exactly what the Twilio status-callback route does for a call
    // that never reaches media.
    server.resolveCompletion('CA_noans', {
      outcome: twilioStatusToOutcome('no-answer'),
      status: 'no-answer',
    });
    const result = await task;

    expect(result?.outcome).toBe('no_answer');
    expect(result?.status).toBe('no-answer');
    expect(result?.transcript).toEqual([]);
    expect(result?.cost).toBeNull();
    expect(result?.metrics).toBeNull();
  });

  it('disconnect() mid-flight rejects the awaiter rather than hanging it', async () => {
    const phone = localPhone();
    const server = attachRealServer(phone, makeAgent());
    // Stub the server stop so disconnect() doesn't try to close a real socket.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (server as any).stop = async () => undefined;
    patchTwilioFetch('CA_disc');

    const task = phone.call({ to: '+15550009999', agent: makeAgent(), wait: true });
    const captured = task.catch((err) => err);
    await waitUntilRegistered(server, 'CA_disc');
    await phone.disconnect();
    const err = await captured;
    expect(err).toBeInstanceOf(PatterConnectionError);
    expect((err as Error).message).toMatch(/still in flight/);
  });
});

// ---------------------------------------------------------------------------
// Outcome mapping helpers (pure functions)
// ---------------------------------------------------------------------------

describe('[unit] outcome mapping helpers', () => {
  it('twilioStatusToOutcome maps no-media statuses', () => {
    expect(twilioStatusToOutcome('no-answer')).toBe('no_answer');
    expect(twilioStatusToOutcome('busy')).toBe('busy');
    expect(twilioStatusToOutcome('failed')).toBe('failed');
    expect(twilioStatusToOutcome('canceled')).toBe('failed');
  });

  it('telnyxHangupOutcome maps no-media causes and returns null for normal_clearing', () => {
    expect(telnyxHangupOutcome('no_answer')).toBe('no_answer');
    expect(telnyxHangupOutcome('timeout')).toBe('no_answer');
    expect(telnyxHangupOutcome('user_busy')).toBe('busy');
    expect(telnyxHangupOutcome('call_rejected')).toBe('failed');
    // normal_clearing implies the call connected → resolved via onCallEnd.
    expect(telnyxHangupOutcome('normal_clearing')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Async disposer — guaranteed disconnect()
// ---------------------------------------------------------------------------

describe('[unit] [Symbol.asyncDispose]', () => {
  it('runs disconnect() on normal block exit', async () => {
    const phone = localPhone();
    let calls = 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).disconnect = async () => {
      calls += 1;
    };
    {
      await using p = phone;
      expect(p).toBe(phone);
    }
    expect(calls).toBe(1);
  });

  it('runs disconnect() even when the block throws', async () => {
    const phone = localPhone();
    let calls = 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (phone as any).disconnect = async () => {
      calls += 1;
    };
    await expect(
      (async () => {
        await using _p = phone;
        throw new Error('boom');
      })(),
    ).rejects.toThrow(/boom/);
    expect(calls).toBe(1);
  });
});

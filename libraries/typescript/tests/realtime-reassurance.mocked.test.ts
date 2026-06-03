import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import { buildAIAdapter } from '../src/server';
import type { LocalConfig } from '../src/server';
import { applyToolCallPreambles } from '../src/stream-handler';
import { Realtime as OpenAIRealtime } from '../src/engines/openai';
import { Realtime2 as OpenAIRealtime2 } from '../src/engines/openai-2';

/**
 * [mocked] The only external boundary mocked here is the OpenAI Realtime
 * WebSocket. We inject a fake socket that records every JSON frame the adapter
 * calls `.send(...)` with. Everything else — the `response.create` shape, the
 * absence of a `conversation.item.create` `role:'user'` item, the preamble
 * instruction assembly — is the real adapter code path.
 */

interface CapturedFrame {
  type?: string;
  item?: { role?: string };
  response?: { instructions?: string; modalities?: unknown };
  [k: string]: unknown;
}

/** A minimal `ws`-shaped double that captures `send()` payloads. */
function makeFakeWs(): { send: (raw: string) => void; frames: CapturedFrame[]; readyState: number } {
  const frames: CapturedFrame[] = [];
  return {
    readyState: 1, // OPEN
    send(raw: string): void {
      frames.push(JSON.parse(raw) as CapturedFrame);
    },
    frames,
  };
}

/** Attach a fake ws to the adapter's protected `ws` field. */
function withFakeWs<T extends OpenAIRealtimeAdapter>(adapter: T): {
  adapter: T;
  frames: CapturedFrame[];
} {
  const fake = makeFakeWs();
  (adapter as unknown as { ws: unknown }).ws = fake;
  return { adapter, frames: fake.frames };
}

describe('[mocked] OpenAIRealtimeAdapter.sendReassurance — no phantom user turn', () => {
  it('emits a response.create carrying the filler in instructions and NO role:user item', async () => {
    const { adapter, frames } = withFakeWs(
      new OpenAIRealtimeAdapter('sk_test', 'gpt-realtime', 'alloy', 'You are helpful.'),
    );

    await adapter.sendReassurance('One moment.');

    // The filler must travel as the assistant's own response instruction.
    const responseCreate = frames.find((f) => f.type === 'response.create');
    expect(responseCreate).toBeDefined();
    expect(responseCreate?.response?.instructions).toContain('One moment.');
    // Match the sibling sendFirstMessage shape so the two no-fake-turn paths
    // never drift.
    expect(responseCreate?.response?.modalities).toEqual(['audio', 'text']);

    // CRITICAL: no conversation.item.create with role:'user' (the bug).
    const fakeUserTurn = frames.find(
      (f) => f.type === 'conversation.item.create' && f.item?.role === 'user',
    );
    expect(fakeUserTurn).toBeUndefined();
  });

  it('GA OpenAIRealtime2Adapter overrides sendReassurance with GA wire shape', async () => {
    // The GA adapter overrides sendReassurance with output_modalities +
    // audio.output.voice (GA endpoint rejects the v1 `modalities` key).
    // This test ensures the override is present and uses the correct shape,
    // mirroring Python OpenAIRealtime2Adapter.send_reassurance.
    const { adapter, frames } = withFakeWs(
      new OpenAIRealtime2Adapter('sk_test', 'gpt-realtime-2', 'alloy', 'You are helpful.'),
    );

    await adapter.sendReassurance('Let me check.');

    const responseCreate = frames.find((f) => f.type === 'response.create');
    expect(responseCreate?.response?.instructions).toContain('Let me check.');
    // GA shape: must use output_modalities (not the v1 modalities key).
    expect(responseCreate?.response).toHaveProperty('output_modalities');
    expect((responseCreate?.response as Record<string, unknown>)?.output_modalities).toEqual(['audio']);
    // GA shape: must re-inject audio.output.voice.
    expect((responseCreate?.response as Record<string, unknown>)?.audio).toEqual({
      output: { voice: 'alloy' },
    });
    // GA adapter must NOT use the v1 `modalities` key (OpenAI rejects it).
    expect(responseCreate?.response?.modalities).toBeUndefined();
    // CRITICAL: no conversation.item.create with role:'user'.
    expect(frames.find((f) => f.item?.role === 'user')).toBeUndefined();
  });

  it('contrast: legacy sendText DOES emit a role:user item (proves send_text untouched)', async () => {
    const { adapter, frames } = withFakeWs(
      new OpenAIRealtimeAdapter('sk_test', 'gpt-realtime', 'alloy', 'You are helpful.'),
    );

    await adapter.sendText('hello');

    const userTurn = frames.find(
      (f) => f.type === 'conversation.item.create' && f.item?.role === 'user',
    );
    expect(userTurn).toBeDefined();
  });

  it('no-ops cleanly when the socket is absent (does not throw)', async () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', 'gpt-realtime', 'alloy', 'p');
    await expect(adapter.sendReassurance('One moment.')).resolves.toBeUndefined();
  });
});

describe('[mocked] Realtime session instructions — preamble injection', () => {
  const RESOLVED = 'You are a helpful receptionist.';

  function buildSession(adapter: OpenAIRealtimeAdapter): Record<string, unknown> {
    return (adapter as unknown as { buildSessionConfig(): Record<string, unknown> })
      .buildSessionConfig();
  }

  it('prepends the # Preambles block when toolCallPreambles is true (v1 adapter)', () => {
    // applyToolCallPreambles runs at the stream-handler assembly point; the
    // adapter receives the already-wrapped prompt. Mirror that here.
    const wrapped = applyToolCallPreambles(RESOLVED, true);
    const adapter = new OpenAIRealtimeAdapter('sk_test', 'gpt-realtime', 'alloy', wrapped);
    const instructions = buildSession(adapter).instructions as string;
    expect(instructions.startsWith('# Preambles')).toBe(true);
    expect(instructions.endsWith(RESOLVED)).toBe(true);
    expect(instructions).toContain("I'll check that order now.");
  });

  it('leaves instructions byte-identical to resolvedPrompt when knob is false/undefined', () => {
    const wrapped = applyToolCallPreambles(RESOLVED, false);
    const adapter = new OpenAIRealtimeAdapter('sk_test', 'gpt-realtime', 'alloy', wrapped);
    const instructions = buildSession(adapter).instructions as string;
    expect(instructions).toBe(RESOLVED);
    expect(instructions).not.toContain('# Preambles');
  });

  it('uses a string knob verbatim as the full override', () => {
    const wrapped = applyToolCallPreambles(RESOLVED, '# House style preamble');
    const adapter = new OpenAIRealtimeAdapter('sk_test', 'gpt-realtime', 'alloy', wrapped);
    const instructions = buildSession(adapter).instructions as string;
    expect(instructions.startsWith('# House style preamble')).toBe(true);
    expect(instructions).not.toContain("I'll check that order now.");
  });
});

describe('[mocked] buildAIAdapter per-tool preamble sample-phrase hint', () => {
  const config: LocalConfig = { openaiKey: 'sk_test' } as LocalConfig;

  function toolDescriptions(adapter: OpenAIRealtimeAdapter): Record<string, string> {
    const session = (adapter as unknown as { buildSessionConfig(): Record<string, unknown> })
      .buildSessionConfig();
    const tools = (session.tools ?? []) as Array<{ name: string; description: string }>;
    return Object.fromEntries(tools.map((t) => [t.name, t.description]));
  }

  it('appends a "Preamble sample phrases" hint to a tool with reassurance when knob is on', () => {
    const agent = {
      systemPrompt: 'p',
      engine: new OpenAIRealtime({ apiKey: 'sk_test' }),
      provider: 'openai_realtime' as const,
      toolCallPreambles: true,
      tools: [],
    };
    const toolsOverride = [
      {
        name: 'lookup_order',
        description: 'Look up an order by id.',
        parameters: { type: 'object', properties: {} },
        reassurance: 'Let me check that order for you.',
      },
    ];
    const adapter = buildAIAdapter(
      config,
      agent as never,
      'p',
      toolsOverride as never,
    ) as OpenAIRealtimeAdapter;
    const descs = toolDescriptions(adapter);
    expect(descs.lookup_order).toContain('Preamble sample phrases');
    expect(descs.lookup_order).toContain('Let me check that order for you.');
  });

  it('does NOT append the hint when toolCallPreambles is off (default)', () => {
    const agent = {
      systemPrompt: 'p',
      engine: new OpenAIRealtime({ apiKey: 'sk_test' }),
      provider: 'openai_realtime' as const,
      tools: [],
    };
    const toolsOverride = [
      {
        name: 'lookup_order',
        description: 'Look up an order by id.',
        parameters: { type: 'object', properties: {} },
        reassurance: 'Let me check that order for you.',
      },
    ];
    const adapter = buildAIAdapter(
      config,
      agent as never,
      'p',
      toolsOverride as never,
    ) as OpenAIRealtimeAdapter;
    const descs = toolDescriptions(adapter);
    expect(descs.lookup_order).toBe('Look up an order by id.');
  });

  it('does NOT append the hint to a tool without reassurance even when knob is on', () => {
    const agent = {
      systemPrompt: 'p',
      engine: new OpenAIRealtime2({ apiKey: 'sk_test' }),
      provider: 'openai_realtime' as const,
      toolCallPreambles: true,
      tools: [],
    };
    const toolsOverride = [
      {
        name: 'plain_tool',
        description: 'A plain tool.',
        parameters: { type: 'object', properties: {} },
      },
    ];
    const adapter = buildAIAdapter(
      config,
      agent as never,
      'p',
      toolsOverride as never,
    ) as OpenAIRealtimeAdapter;
    const descs = toolDescriptions(adapter);
    expect(descs.plain_tool).toBe('A plain tool.');
  });
});

describe('[mocked] reassurance early-cancel semantics (setTimeout scheduler)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  /**
   * Reproduces the stream-handler's reassurance scheduler in isolation: a
   * setTimeout(afterMs) that fires `sendReassurance`, cleared when the tool
   * resolves first. We assert the timer/cancel behaviour and the routed method.
   */
  function scheduleReassurance(
    adapter: OpenAIRealtimeAdapter,
    msg: string,
    afterMs: number,
  ): ReturnType<typeof setTimeout> {
    return setTimeout(() => {
      const fire =
        typeof (adapter as { sendReassurance?: unknown }).sendReassurance === 'function'
          ? adapter.sendReassurance(msg)
          : adapter.sendText(msg);
      void fire.catch(() => {
        /* non-fatal */
      });
    }, afterMs);
  }

  it('does not speak when the tool finishes before afterMs (clearTimeout)', () => {
    const { adapter, frames } = withFakeWs(
      new OpenAIRealtimeAdapter('sk_test', 'gpt-realtime', 'alloy', 'p'),
    );
    const timer = scheduleReassurance(adapter, 'One moment.', 1500);
    // Tool returns at 200ms — cancel the timer.
    vi.advanceTimersByTime(200);
    clearTimeout(timer);
    vi.advanceTimersByTime(5000);
    expect(frames.length).toBe(0);
  });

  it('speaks exactly one reassurance frame when afterMs elapses', () => {
    const { adapter, frames } = withFakeWs(
      new OpenAIRealtimeAdapter('sk_test', 'gpt-realtime', 'alloy', 'p'),
    );
    scheduleReassurance(adapter, 'One moment.', 1500);
    vi.advanceTimersByTime(1500);
    const responseCreates = frames.filter((f) => f.type === 'response.create');
    expect(responseCreates.length).toBe(1);
    expect(responseCreates[0].response?.instructions).toContain('One moment.');
    // Still no phantom user turn.
    expect(frames.find((f) => f.item?.role === 'user')).toBeUndefined();
  });
});

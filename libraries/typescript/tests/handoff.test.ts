/**
 * MULTI-AGENT HANDOFF (built-in `handoff_to` tool) — TypeScript parity with
 * `libraries/python/tests/test_handoff.py`.
 *
 * Covers:
 *   - `buildHandoffTool` schema (names surfaced as an enum, deterministic).
 *   - `Patter.agent({ handoffs })` validation + passthrough.
 *   - Pipeline mode: a handoff swaps the LLM loop's system prompt + tool list
 *     for the NEXT turn (recording fake LLM provider asserts the new prompt).
 *   - Realtime mode: a handoff sends the expected `session.update` payload
 *     via the adapter and ALWAYS sends a function result; an unknown handoff
 *     name returns an error envelope (never silence).
 *   - Adapter `updateSession` wire shape (v1-beta + GA discriminator).
 */
import { describe, it, expect, vi } from 'vitest';
import {
  StreamHandler,
  buildHandoffTool,
  applyHandoffTarget,
  handoffHistoryText,
  HANDOFF_TOOL_NAME,
} from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { LLMLoop, DEFAULT_PHONE_PREAMBLE } from '../src/llm-loop';
import type { LLMChunk, LLMProvider } from '../src/llm-loop';
import { TRANSFER_CALL_TOOL, END_CALL_TOOL } from '../src/server';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import { Patter, Twilio, OpenAIRealtime } from '../src/index';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { WebSocket as WSWebSocket } from 'ws';
import type { AgentOptions, ToolDefinition } from '../src/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(),
    close: vi.fn(),
    on: vi.fn(),
    once: vi.fn(),
    readyState: 1,
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makeBridge(): TelephonyBridge {
  return {
    label: 'Twilio',
    telephonyProvider: 'twilio',
    inputWireFormat: 'ulaw_8000',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  } as unknown as TelephonyBridge;
}

function makeDeps(agent: AgentOptions, overrides?: Partial<StreamHandlerDeps>): StreamHandlerDeps {
  return {
    config: { openaiKey: 'sk-test' },
    agent,
    bridge: makeBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn(),
    sanitizeVariables: vi.fn((raw: Record<string, unknown>) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl: string) => tpl),
    ...overrides,
  } as unknown as StreamHandlerDeps;
}

function makeHandler(agent: AgentOptions, overrides?: Partial<StreamHandlerDeps>): StreamHandler {
  return new StreamHandler(makeDeps(agent, overrides), makeMockWs(), '+15551234567', '+15559876543');
}

/** Fake adapter mirroring Python's `handler._adapter = AsyncMock()`. */
function makeFakeAdapter(): { updateSession: ReturnType<typeof vi.fn>; sendFunctionResult: ReturnType<typeof vi.fn> } {
  return {
    updateSession: vi.fn().mockResolvedValue(undefined),
    sendFunctionResult: vi.fn().mockResolvedValue(undefined),
  };
}

/** Fake LLMProvider that records the messages/tools of every stream call. */
class RecordingProvider implements LLMProvider {
  calls: Array<[Array<Record<string, unknown>>, Array<Record<string, unknown>> | null | undefined]> = [];

  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    this.calls.push([messages.map((m) => ({ ...m })), tools]);
    yield { type: 'text', content: 'ok' };
    yield { type: 'done' };
  }
}

// ---------------------------------------------------------------------------
// buildHandoffTool — schema
// ---------------------------------------------------------------------------

describe('buildHandoffTool', () => {
  it('builds the schema with names surfaced as an enum', () => {
    const tool = buildHandoffTool(['billing', 'support']);
    expect(tool.name).toBe(HANDOFF_TOOL_NAME);
    expect(HANDOFF_TOOL_NAME).toBe('handoff_to');
    const props = (tool.parameters as { properties: Record<string, { enum?: string[] }> }).properties;
    expect(props.name.enum).toEqual(['billing', 'support']);
    expect(props.reason).toBeDefined();
    expect((tool.parameters as { required: string[] }).required).toEqual(['name']);
    expect(tool.description).toContain('billing, support');
  });

  it('sorts names for a deterministic schema', () => {
    const a = buildHandoffTool(['zeta', 'alpha']);
    const b = buildHandoffTool(['alpha', 'zeta']);
    expect(a).toEqual(b);
    const props = (a.parameters as { properties: Record<string, { enum?: string[] }> }).properties;
    expect(props.name.enum).toEqual(['alpha', 'zeta']);
  });
});

describe('handoffHistoryText', () => {
  it('formats with and without a reason', () => {
    expect(handoffHistoryText('billing', '')).toBe(
      "[handoff] Conversation handed to agent 'billing'",
    );
    expect(handoffHistoryText('billing', 'invoice')).toBe(
      "[handoff] Conversation handed to agent 'billing' — invoice",
    );
  });
});

describe('applyHandoffTarget', () => {
  it('swaps conversational config and keeps call-start audio config', () => {
    const current: AgentOptions = {
      systemPrompt: 'Triage.',
      voice: 'alloy',
      model: 'gpt-realtime-mini',
      language: 'en',
      handoffs: { billing: { systemPrompt: 'Billing.' } as AgentOptions },
    };
    const target: AgentOptions = {
      systemPrompt: 'Billing.',
      voice: 'echo',
      model: 'gpt-realtime-2',
      tools: [{ name: 'lookup_invoice', description: '', parameters: {} }] as ToolDefinition[],
    };
    const merged = applyHandoffTarget(current, target);
    expect(merged.systemPrompt).toBe('Billing.');
    expect(merged.tools).toBe(target.tools);
    expect(merged.handoffs).toBeUndefined(); // target has no onward handoffs
    // Audio infrastructure established at call start is retained.
    expect(merged.voice).toBe('alloy');
    expect(merged.model).toBe('gpt-realtime-mini');
    expect(merged.language).toBe('en');
    // Inputs never mutated.
    expect(current.systemPrompt).toBe('Triage.');
  });
});

// ---------------------------------------------------------------------------
// Patter.agent — handoffs validation
// ---------------------------------------------------------------------------

describe('Patter.agent handoffs validation', () => {
  function makePhone(): Patter {
    return new Patter({
      carrier: new Twilio({ accountSid: 'AC' + '0'.repeat(32), authToken: 'token' }),
      phoneNumber: '+15550000000',
      webhookUrl: 'example.ngrok.io',
    });
  }

  function makeEngine(): OpenAIRealtime {
    return new OpenAIRealtime({ apiKey: 'sk-test' });
  }

  it('stores handoffs on the agent and defaults to undefined', () => {
    const phone = makePhone();
    const billing = phone.agent({ systemPrompt: 'You handle billing.', engine: makeEngine() });
    const triage = phone.agent({
      systemPrompt: 'You triage calls.',
      engine: makeEngine(),
      handoffs: { billing },
    });
    expect(triage.handoffs).toEqual({ billing });
    // Default stays undefined — zero behavior change for existing agents.
    expect(billing.handoffs).toBeUndefined();
  });

  it('rejects non-object handoffs', () => {
    const phone = makePhone();
    expect(() =>
      phone.agent({
        systemPrompt: 'x',
        engine: makeEngine(),
        handoffs: ['billing'] as unknown as Record<string, AgentOptions>,
      }),
    ).toThrow(/handoffs must be an object/);
  });

  it('rejects non-object handoff values', () => {
    const phone = makePhone();
    expect(() =>
      phone.agent({
        systemPrompt: 'x',
        engine: makeEngine(),
        handoffs: { billing: 'not-an-agent' as unknown as AgentOptions },
      }),
    ).toThrow(/must be an agent options object/);
  });

  it('rejects empty handoff names', () => {
    const phone = makePhone();
    const billing = phone.agent({ systemPrompt: 'You handle billing.', engine: makeEngine() });
    expect(() =>
      phone.agent({ systemPrompt: 'x', engine: makeEngine(), handoffs: { '': billing } }),
    ).toThrow(/non-empty strings/);
  });
});

// ---------------------------------------------------------------------------
// LLMLoop.updateAgent — next turn runs with the new prompt + tools
// ---------------------------------------------------------------------------

describe('LLMLoop.updateAgent', () => {
  it('swaps prompt and tools for the next turn', async () => {
    const provider = new RecordingProvider();
    const loop = new LLMLoop(
      '',
      'fake',
      'You are agent A.',
      [{ name: 'tool_a', description: '', parameters: {} }] as ToolDefinition[],
      provider,
    );

    for await (const _ of loop.run('hi', [], { call_id: 'c1' })) void _;
    const [firstMessages, firstTools] = provider.calls[0];
    expect(firstMessages[0].role).toBe('system');
    expect(String(firstMessages[0].content)).toContain('You are agent A.');
    expect((firstTools ?? []).map((t) => (t.function as { name: string }).name)).toEqual(['tool_a']);

    loop.updateAgent({
      systemPrompt: 'You are agent B.',
      tools: [{ name: 'tool_b', description: '', parameters: {} }] as ToolDefinition[],
    });

    for await (const _ of loop.run('hi again', [], { call_id: 'c1' })) void _;
    const [secondMessages, secondTools] = provider.calls[1];
    expect(String(secondMessages[0].content)).toContain('You are agent B.');
    expect(String(secondMessages[0].content)).not.toContain('You are agent A.');
    // Phone preamble still applied on the swapped prompt.
    expect(String(secondMessages[0].content).startsWith(DEFAULT_PHONE_PREAMBLE)).toBe(true);
    expect((secondTools ?? []).map((t) => (t.function as { name: string }).name)).toEqual(['tool_b']);
  });

  it('respects disablePhonePreamble', async () => {
    const provider = new RecordingProvider();
    const loop = new LLMLoop('', 'fake', 'A', null, provider);
    loop.updateAgent({ systemPrompt: 'Verbatim.', disablePhonePreamble: true });
    for await (const _ of loop.run('hi', [], { call_id: 'c1' })) void _;
    expect(provider.calls[0][0][0].content).toBe('Verbatim.');
  });

  it('skips system history entries (handoff markers) when replaying', async () => {
    const provider = new RecordingProvider();
    const loop = new LLMLoop('', 'fake', 'A', null, provider);
    const history = [
      { role: 'user', text: 'hello' },
      { role: 'system', text: "[handoff] Conversation handed to agent 'b'" },
      { role: 'assistant', text: 'hi there' },
    ];
    for await (const _ of loop.run('next', history, { call_id: 'c1' })) void _;
    const roles = provider.calls[0][0].map((m) => m.role);
    expect(roles).toEqual(['system', 'user', 'assistant', 'user']);
    expect(
      provider.calls[0][0].every((m) => !String(m.content ?? '').includes('[handoff]')),
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Pipeline handler — performHandoff
// ---------------------------------------------------------------------------

type PrivateHandler = {
  performHandoff(name: string, reason: string): Promise<string>;
  handleHandoffFunctionCall(fc: { call_id: string; name: string; arguments: string }): Promise<void>;
  buildPipelineLlmTools(): ToolDefinition[];
  currentAgent: AgentOptions;
  history: { entries: Array<{ role: string; text: string }> };
  llmLoop: { updateAgent: ReturnType<typeof vi.fn> } | null;
  adapter: unknown;
};

describe('pipeline performHandoff', () => {
  const billingTool: ToolDefinition = { name: 'lookup_invoice', description: '', parameters: {} };

  function makeAgents(): { triage: AgentOptions; billing: AgentOptions } {
    const billing: AgentOptions = {
      systemPrompt: 'You are the billing specialist.',
      provider: 'pipeline',
      tools: [billingTool],
    };
    const triage: AgentOptions = {
      systemPrompt: 'You are the triage agent.',
      provider: 'pipeline',
      handoffs: { billing },
    };
    return { triage, billing };
  }

  it('swaps prompt and tools for the next turn, preserving history', async () => {
    const { triage } = makeAgents();
    const handler = makeHandler(triage) as unknown as PrivateHandler;
    const llmLoop = { updateAgent: vi.fn() };
    handler.llmLoop = llmLoop;

    const result = JSON.parse(await handler.performHandoff('billing', 'billing question'));

    expect(result).toEqual({ status: 'handed_off', to: 'billing' });
    // Agent reference swapped (shared AgentOptions replaced, never mutated).
    expect(handler.currentAgent.systemPrompt).toBe('You are the billing specialist.');
    expect(triage.systemPrompt).toBe('You are the triage agent.'); // original untouched
    // LLM loop got the new prompt + rebuilt tool list for the NEXT turn.
    expect(llmLoop.updateAgent).toHaveBeenCalledTimes(1);
    const update = llmLoop.updateAgent.mock.calls[0][0] as {
      systemPrompt: string;
      tools: ToolDefinition[];
    };
    expect(update.systemPrompt).toBe('You are the billing specialist.');
    expect(update.tools.map((t) => t.name)).toEqual(['lookup_invoice', 'transfer_call', 'end_call']);
    // History records the handoff as a system-style entry.
    expect(
      handler.history.entries.some(
        (e) => e.role === 'system' && e.text.includes("handed to agent 'billing'"),
      ),
    ).toBe(true);
  });

  it('chained handoffs follow the target map', async () => {
    const support: AgentOptions = { systemPrompt: 'Support.', provider: 'pipeline' };
    const billing: AgentOptions = {
      systemPrompt: 'Billing.',
      provider: 'pipeline',
      handoffs: { support },
    };
    const triage: AgentOptions = {
      systemPrompt: 'Triage.',
      provider: 'pipeline',
      handoffs: { billing },
    };
    const handler = makeHandler(triage) as unknown as PrivateHandler;
    const llmLoop = { updateAgent: vi.fn() };
    handler.llmLoop = llmLoop;

    await handler.performHandoff('billing', '');

    const update = llmLoop.updateAgent.mock.calls[0][0] as { tools: ToolDefinition[] };
    const handoffTools = update.tools.filter((t) => t.name === HANDOFF_TOOL_NAME);
    expect(handoffTools).toHaveLength(1);
    const props = (handoffTools[0].parameters as { properties: Record<string, { enum?: string[] }> })
      .properties;
    expect(props.name.enum).toEqual(['support']);
  });

  it('rejects an unknown target with an error envelope and no swap', async () => {
    const { triage } = makeAgents();
    const handler = makeHandler(triage) as unknown as PrivateHandler;
    const llmLoop = { updateAgent: vi.fn() };
    handler.llmLoop = llmLoop;

    const result = JSON.parse(await handler.performHandoff('sales', ''));

    expect(result.error).toContain("Unknown handoff agent 'sales'");
    expect(result.available).toEqual(['billing']);
    expect(llmLoop.updateAgent).not.toHaveBeenCalled();
    expect(handler.currentAgent.systemPrompt).toBe('You are the triage agent.');
    expect(handler.history.entries.some((e) => e.role === 'system')).toBe(false);
  });

  it('injects handoff_to into the combined pipeline tools with a live handler', async () => {
    const { triage } = makeAgents();
    const handler = makeHandler(triage) as unknown as PrivateHandler;

    const tools = handler.buildPipelineLlmTools();
    expect(tools.map((t) => t.name)).toEqual(['transfer_call', 'end_call', HANDOFF_TOOL_NAME]);
    const handoffTool = tools[tools.length - 1];
    expect(typeof handoffTool.handler).toBe('function');
    // The handler closure dispatches into performHandoff (error envelope
    // proves the round trip — no LLM loop is wired in this minimal handler).
    const result = JSON.parse(
      await (handoffTool.handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
        { name: 'nope' },
        {},
      ),
    );
    expect(result.error).toContain('Unknown handoff agent');
  });
});

// ---------------------------------------------------------------------------
// Realtime handler — handleHandoffFunctionCall
// ---------------------------------------------------------------------------

describe('realtime handleHandoffFunctionCall', () => {
  function makeRealtimeAgents(): { triage: AgentOptions; billing: AgentOptions } {
    const billing: AgentOptions = {
      systemPrompt: 'You are the billing specialist.',
      provider: 'openai_realtime',
      voice: 'echo',
      tools: [{ name: 'lookup_invoice', description: 'd', parameters: {} }] as ToolDefinition[],
    };
    const triage: AgentOptions = {
      systemPrompt: 'You are the triage agent.',
      provider: 'openai_realtime',
      voice: 'alloy',
      handoffs: { billing },
    };
    return { triage, billing };
  }

  it('sends session.update with the target config, then the function result', async () => {
    const { triage } = makeRealtimeAgents();
    const handler = makeHandler(triage) as unknown as PrivateHandler;
    const adapter = makeFakeAdapter();
    handler.adapter = adapter;

    await handler.handleHandoffFunctionCall({
      call_id: 'fc-1',
      name: HANDOFF_TOOL_NAME,
      arguments: JSON.stringify({ name: 'billing', reason: 'invoice question' }),
    });

    // session.update carries the target's prompt + rebuilt tool surface.
    expect(adapter.updateSession).toHaveBeenCalledTimes(1);
    const update = adapter.updateSession.mock.calls[0][0] as {
      instructions: string;
      tools: Array<{ name: string }>;
    };
    expect(update.instructions).toBe('You are the billing specialist.');
    expect(update.tools.map((t) => t.name)).toEqual(['lookup_invoice', 'transfer_call', 'end_call']);

    // A function result is ALWAYS sent — success envelope here.
    expect(adapter.sendFunctionResult).toHaveBeenCalledTimes(1);
    const [fcCallId, resultStr] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    expect(fcCallId).toBe('fc-1');
    expect(JSON.parse(resultStr)).toEqual({ status: 'handed_off', to: 'billing' });

    // Handler agent reference swapped; voice intentionally unchanged.
    expect(handler.currentAgent.systemPrompt).toBe('You are the billing specialist.');
    expect(handler.currentAgent.voice).toBe('alloy');

    // History records the handoff with the reason.
    expect(
      handler.history.entries.some(
        (e) =>
          e.role === 'system' &&
          e.text.includes("handed to agent 'billing'") &&
          e.text.includes('invoice question'),
      ),
    ).toBe(true);
  });

  it('session.update precedes the function result', async () => {
    const { triage } = makeRealtimeAgents();
    const handler = makeHandler(triage) as unknown as PrivateHandler;
    const order: string[] = [];
    handler.adapter = {
      updateSession: vi.fn().mockImplementation(async () => order.push('update')),
      sendFunctionResult: vi.fn().mockImplementation(async () => order.push('result')),
    };

    await handler.handleHandoffFunctionCall({
      call_id: 'fc-1',
      name: HANDOFF_TOOL_NAME,
      arguments: JSON.stringify({ name: 'billing' }),
    });
    // The function result triggers the model's next response, which must
    // already run under the new instructions.
    expect(order).toEqual(['update', 'result']);
  });

  it('unknown name sends an error envelope and never updates the session', async () => {
    const { triage } = makeRealtimeAgents();
    const handler = makeHandler(triage) as unknown as PrivateHandler;
    const adapter = makeFakeAdapter();
    handler.adapter = adapter;

    await handler.handleHandoffFunctionCall({
      call_id: 'fc-2',
      name: HANDOFF_TOOL_NAME,
      arguments: JSON.stringify({ name: 'sales' }),
    });

    expect(adapter.updateSession).not.toHaveBeenCalled();
    expect(adapter.sendFunctionResult).toHaveBeenCalledTimes(1);
    const [, resultStr] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    const result = JSON.parse(resultStr);
    expect(result.error).toContain("Unknown handoff agent 'sales'");
    expect(result.available).toEqual(['billing']);
    expect(handler.currentAgent.systemPrompt).toBe('You are the triage agent.');
  });

  it('malformed arguments send an error envelope', async () => {
    const { triage } = makeRealtimeAgents();
    const handler = makeHandler(triage) as unknown as PrivateHandler;
    const adapter = makeFakeAdapter();
    handler.adapter = adapter;

    await handler.handleHandoffFunctionCall({
      call_id: 'fc-3',
      name: HANDOFF_TOOL_NAME,
      arguments: '{not json',
    });

    expect(adapter.sendFunctionResult).toHaveBeenCalledTimes(1);
    const [, resultStr] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    expect(JSON.parse(resultStr).error).toBeDefined();
  });

  it('advertises chained targets after the handoff', async () => {
    const support: AgentOptions = { systemPrompt: 'Support.', provider: 'openai_realtime' };
    const billing: AgentOptions = {
      systemPrompt: 'Billing.',
      provider: 'openai_realtime',
      handoffs: { support },
    };
    const triage: AgentOptions = {
      systemPrompt: 'Triage.',
      provider: 'openai_realtime',
      handoffs: { billing },
    };
    const handler = makeHandler(triage) as unknown as PrivateHandler;
    const adapter = makeFakeAdapter();
    handler.adapter = adapter;

    await handler.handleHandoffFunctionCall({
      call_id: 'fc-4',
      name: HANDOFF_TOOL_NAME,
      arguments: JSON.stringify({ name: 'billing' }),
    });
    const update = adapter.updateSession.mock.calls[0][0] as {
      tools: Array<{ name: string; parameters: { properties: Record<string, { enum?: string[] }> } }>;
    };
    const handoffTools = update.tools.filter((t) => t.name === HANDOFF_TOOL_NAME);
    expect(handoffTools).toHaveLength(1);
    expect(handoffTools[0].parameters.properties.name.enum).toEqual(['support']);
  });
});

// ---------------------------------------------------------------------------
// Adapter updateSession — wire shape (v1-beta + GA)
// ---------------------------------------------------------------------------

describe('adapter updateSession wire shape', () => {
  type WireTool = { name: string; description: string; parameters: Record<string, unknown>; strict?: boolean };

  it('v1-beta sends a flat partial session (no discriminator)', async () => {
    const adapter = new OpenAIRealtimeAdapter('sk-test', undefined, undefined, 'old');
    const send = vi.fn();
    (adapter as unknown as { ws: { send: typeof send } }).ws = { send };

    await adapter.updateSession({
      instructions: 'new instructions',
      tools: [TRANSFER_CALL_TOOL, END_CALL_TOOL] as unknown as WireTool[],
    });

    expect(send).toHaveBeenCalledTimes(1);
    const sent = JSON.parse(send.mock.calls[0][0] as string);
    expect(sent.type).toBe('session.update');
    expect(sent.session.type).toBeUndefined(); // v1-beta shape — no discriminator
    expect(sent.session.instructions).toBe('new instructions');
    expect(sent.session.tools.map((t: { name: string }) => t.name)).toEqual([
      'transfer_call',
      'end_call',
    ]);
    expect(sent.session.tools.every((t: { type: string }) => t.type === 'function')).toBe(true);
    // Adapter state updated so reconnect paths rebuild with the new config.
    expect((adapter as unknown as { instructions: string }).instructions).toBe('new instructions');
  });

  it('GA includes the mandatory type: realtime discriminator', async () => {
    const adapter = new OpenAIRealtime2Adapter('sk-test', undefined, undefined, 'old');
    const send = vi.fn();
    (adapter as unknown as { ws: { send: typeof send } }).ws = { send };

    await adapter.updateSession({ instructions: 'new instructions' });

    const sent = JSON.parse(send.mock.calls[0][0] as string);
    expect(sent.type).toBe('session.update');
    expect(sent.session.type).toBe('realtime'); // GA requires the discriminator
    expect(sent.session.instructions).toBe('new instructions');
    expect(sent.session.tools).toBeUndefined(); // partial update — only what changed
  });

  it('is a no-op without fields and safe without a socket', async () => {
    const adapter = new OpenAIRealtimeAdapter('sk-test');
    const send = vi.fn();
    (adapter as unknown as { ws: { send: typeof send } }).ws = { send };
    await adapter.updateSession({});
    expect(send).not.toHaveBeenCalled();

    (adapter as unknown as { ws: null }).ws = null;
    // No socket — state still updated, no crash.
    await adapter.updateSession({ instructions: 'x' });
    expect((adapter as unknown as { instructions: string }).instructions).toBe('x');
  });
});

// ---------------------------------------------------------------------------
// Dispatch guard — built-in branch requires configured handoffs
// ---------------------------------------------------------------------------

describe('handoff dispatch guard', () => {
  it('an agent without handoffs keeps the built-in branch disabled', () => {
    const agent: AgentOptions = { systemPrompt: 'x', provider: 'openai_realtime' };
    const handler = makeHandler(agent) as unknown as PrivateHandler;
    expect(handler.currentAgent.handoffs).toBeUndefined();
  });
});

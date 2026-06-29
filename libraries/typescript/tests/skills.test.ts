/**
 * SKILLS primitive (built-in `use_skill` tool, progressive disclosure) —
 * TypeScript parity with `libraries/python/tests/test_skills.py`.
 *
 * Covers:
 *   - `buildUseSkillTool` schema — DISCOVERY surfaces only skill names +
 *     descriptions as an enum, NOT the full instructions.
 *   - `applySkillActivation` — ACTIVATION layers instructions into the prompt
 *     and merges the skill's tools (additive, deduped).
 *   - `assertNoSkillConflicts` — reserved / cross-skill / agent-tool collisions.
 *   - `Patter.agent({ skills })` validation + passthrough.
 *   - Pipeline mode: `use_skill` is advertised; activating a skill layers the
 *     instructions, makes the skill's tool invocable, and a skill tool is NOT
 *     in the combined tool list before activation.
 *   - Realtime mode: `use_skill` activation sends `session.update` (now
 *     carrying the skill's tools) + ALWAYS a function result; unknown /
 *     already-active skills return envelopes; malformed args never wedge.
 */
import { describe, it, expect, vi } from 'vitest';
import { StreamHandler } from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import {
  USE_SKILL_TOOL_NAME,
  applySkillActivation,
  assertNoSkillConflicts,
  buildUseSkillTool,
  skillActivationBlock,
} from '../src/skills';
import { Patter, Twilio, OpenAIRealtime } from '../src/index';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import { tool } from '../src/public-api';
import type { WebSocket as WSWebSocket } from 'ws';
import type { AgentOptions, Skill, ToolDefinition } from '../src/types';

// ---------------------------------------------------------------------------
// Helpers (mirroring handoff.test.ts harness)
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

function makeHandler(agent: AgentOptions): StreamHandler {
  return new StreamHandler(makeDeps(agent), makeMockWs(), '+15551234567', '+15559876543');
}

function makeFakeAdapter(): { updateSession: ReturnType<typeof vi.fn>; sendFunctionResult: ReturnType<typeof vi.fn> } {
  return {
    updateSession: vi.fn().mockResolvedValue(undefined),
    sendFunctionResult: vi.fn().mockResolvedValue(undefined),
  };
}

const REFUND_TOOL: ToolDefinition = {
  name: 'process_refund',
  description: 'Issue a refund for an order.',
  parameters: { type: 'object', properties: {} },
  handler: async () => 'refunded',
};
const SKILL_INSTRUCTIONS =
  'STEP 1: verify the order id. STEP 2: confirm the amount. STEP 3: call ' +
  'process_refund. Never refund more than $500 without escalation.';

function refundSkill(): Skill {
  return {
    name: 'refunds',
    description: 'Process customer refunds end to end.',
    instructions: SKILL_INSTRUCTIONS,
    tools: [REFUND_TOOL] as unknown as Skill['tools'],
  };
}

type PrivateHandler = {
  performSkillActivation(name: string): Promise<string>;
  handleUseSkillFunctionCall(fc: { call_id: string; name: string; arguments: string }): Promise<void>;
  buildPipelineLlmTools(): ToolDefinition[];
  currentAgent: AgentOptions;
  history: { entries: Array<{ role: string; text: string }> };
  llmLoop: { updateAgent: ReturnType<typeof vi.fn> } | null;
  adapter: unknown;
};

// ---------------------------------------------------------------------------
// buildUseSkillTool — DISCOVERY layer
// ---------------------------------------------------------------------------

describe('buildUseSkillTool', () => {
  it('surfaces names + descriptions as an enum, never the instructions', () => {
    const t = buildUseSkillTool([refundSkill()]);
    expect(t.name).toBe(USE_SKILL_TOOL_NAME);
    expect(USE_SKILL_TOOL_NAME).toBe('use_skill');
    const props = (t.parameters as { properties: Record<string, { enum?: string[] }>; required: string[] });
    expect(props.properties.skill_name.enum).toEqual(['refunds']);
    expect(props.required).toEqual(['skill_name']);
    // Discovery surfaces the description (WHEN to use it)...
    expect(t.description).toContain('Process customer refunds end to end.');
    // ...but NEVER the full instructions or the skill tool name.
    expect(t.description).not.toContain(SKILL_INSTRUCTIONS);
    expect(t.description).not.toContain('process_refund');
  });

  it('sorts skill names for a deterministic schema', () => {
    const a: Skill = { name: 'zeta', description: 'z', instructions: 'iz' };
    const b: Skill = { name: 'alpha', description: 'a', instructions: 'ia' };
    const t1 = buildUseSkillTool([a, b]);
    const t2 = buildUseSkillTool([b, a]);
    expect(t1).toEqual(t2);
    expect((t1.parameters as { properties: Record<string, { enum?: string[] }> }).properties.skill_name.enum).toEqual([
      'alpha',
      'zeta',
    ]);
  });
});

// ---------------------------------------------------------------------------
// applySkillActivation — ACTIVATION layer
// ---------------------------------------------------------------------------

describe('applySkillActivation', () => {
  it('layers instructions into the prompt and merges the skill tools', () => {
    const agent: AgentOptions = {
      systemPrompt: 'You are a support agent.',
      tools: [{ name: 'lookup_order', description: '', parameters: {} }] as ToolDefinition[],
    };
    const activated = applySkillActivation(agent, refundSkill());

    expect(activated.systemPrompt).toContain('You are a support agent.');
    expect(activated.systemPrompt).toContain('# Skill: refunds');
    expect(activated.systemPrompt).toContain(SKILL_INSTRUCTIONS);
    expect(activated.systemPrompt.startsWith('You are a support agent.')).toBe(true);
    const names = (activated.tools as ToolDefinition[]).map((t) => t.name);
    expect(names).toEqual(['lookup_order', 'process_refund']);
    // Frozen-by-convention — original untouched.
    expect(agent.systemPrompt).toBe('You are a support agent.');
    expect((agent.tools as ToolDefinition[]).map((t) => t.name)).toEqual(['lookup_order']);
  });

  it('dedupes a skill tool whose name already exists on the agent', () => {
    const agent: AgentOptions = {
      systemPrompt: 'x',
      tools: [{ name: 'process_refund', description: 'existing', parameters: {} }] as ToolDefinition[],
    };
    const activated = applySkillActivation(agent, refundSkill());
    expect((activated.tools as ToolDefinition[]).map((t) => t.name)).toEqual(['process_refund']);
  });

  it('renders the activation block', () => {
    expect(skillActivationBlock({ name: 'refunds', description: 'd', instructions: 'DO X' })).toBe(
      '# Skill: refunds\n\nDO X',
    );
  });
});

// ---------------------------------------------------------------------------
// assertNoSkillConflicts
// ---------------------------------------------------------------------------

describe('assertNoSkillConflicts', () => {
  it('rejects a skill tool named like a reserved built-in', () => {
    const bad: Skill = {
      name: 'x',
      description: 'd',
      instructions: 'i',
      tools: [{ name: 'end_call', description: '', parameters: {} }] as unknown as Skill['tools'],
    };
    expect(() => assertNoSkillConflicts([bad])).toThrow(/reserved built-in tool name/);
  });

  it('rejects a skill tool named use_skill', () => {
    const bad: Skill = {
      name: 'x',
      description: 'd',
      instructions: 'i',
      tools: [{ name: 'use_skill', description: '', parameters: {} }] as unknown as Skill['tools'],
    };
    expect(() => assertNoSkillConflicts([bad])).toThrow(/reserved built-in tool name/);
  });

  it('rejects a collision with a top-level agent tool', () => {
    expect(() => assertNoSkillConflicts([refundSkill()], [{ name: 'process_refund' }])).toThrow(/top-level/);
  });

  it('rejects duplicate skill names', () => {
    const a: Skill = { name: 'dup', description: 'd', instructions: 'i' };
    const b: Skill = { name: 'dup', description: 'd2', instructions: 'i2' };
    expect(() => assertNoSkillConflicts([a, b])).toThrow(/Duplicate skill name/);
  });

  it('rejects the same tool name declared by two skills', () => {
    const a: Skill = {
      name: 'a',
      description: 'd',
      instructions: 'i',
      tools: [{ name: 'shared', description: '', parameters: {} }] as unknown as Skill['tools'],
    };
    const b: Skill = {
      name: 'b',
      description: 'd',
      instructions: 'i',
      tools: [{ name: 'shared', description: '', parameters: {} }] as unknown as Skill['tools'],
    };
    expect(() => assertNoSkillConflicts([a, b])).toThrow(/more than one skill/);
  });

  it('passes for a clean configuration', () => {
    expect(() => assertNoSkillConflicts([refundSkill()], [{ name: 'lookup_order' }])).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Patter.agent — skills validation + passthrough
// ---------------------------------------------------------------------------

describe('Patter.agent skills validation', () => {
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

  it('stores skills on the agent and defaults to undefined', () => {
    const phone = makePhone();
    const skill: Skill = {
      name: 'refunds',
      description: 'Handle refunds.',
      instructions: SKILL_INSTRUCTIONS,
      tools: [tool({ name: 'process_refund', description: 'Refund', handler: async () => 'ok' })],
    };
    const agent = phone.agent({ systemPrompt: 'Support.', engine: makeEngine(), skills: [skill] });
    expect(agent.skills).toHaveLength(1);
    expect(agent.skills![0].name).toBe('refunds');
    const plain = phone.agent({ systemPrompt: 'x', engine: makeEngine() });
    expect(plain.skills).toBeUndefined();
  });

  it('rejects a skill tool colliding with a reserved built-in at build time', () => {
    const phone = makePhone();
    const skill: Skill = {
      name: 'x',
      description: 'd',
      instructions: 'i',
      tools: [tool({ name: 'transfer_call', description: 'bad', handler: async () => 'ok' })],
    };
    expect(() => phone.agent({ systemPrompt: 'x', engine: makeEngine(), skills: [skill] })).toThrow(
      /reserved built-in tool name/,
    );
  });

  it('rejects a skill without a name', () => {
    const phone = makePhone();
    const skill = { description: 'd', instructions: 'i' } as unknown as Skill;
    expect(() => phone.agent({ systemPrompt: 'x', engine: makeEngine(), skills: [skill] })).toThrow(
      /non-empty name/,
    );
  });
});

// ---------------------------------------------------------------------------
// Pipeline handler — performSkillActivation
// ---------------------------------------------------------------------------

describe('pipeline performSkillActivation', () => {
  it('advertises use_skill but NOT the skill tool before activation', () => {
    const agent: AgentOptions = {
      systemPrompt: 'Support.',
      provider: 'pipeline',
      skills: [refundSkill()],
    };
    const handler = makeHandler(agent) as unknown as PrivateHandler;
    const names = handler.buildPipelineLlmTools().map((t) => t.name);
    expect(names).toContain(USE_SKILL_TOOL_NAME);
    expect(names).not.toContain('process_refund');
  });

  it('layers the prompt and unlocks the skill tool on activation', async () => {
    const agent: AgentOptions = {
      systemPrompt: 'You are a support agent.',
      provider: 'pipeline',
      skills: [refundSkill()],
    };
    const handler = makeHandler(agent) as unknown as PrivateHandler;
    const llmLoop = { updateAgent: vi.fn() };
    handler.llmLoop = llmLoop;

    const result = JSON.parse(await handler.performSkillActivation('refunds'));
    expect(result).toEqual({ status: 'skill_activated', skill: 'refunds' });
    expect(handler.currentAgent.systemPrompt).toContain('# Skill: refunds');
    expect(handler.currentAgent.systemPrompt).toContain(SKILL_INSTRUCTIONS);
    expect(llmLoop.updateAgent).toHaveBeenCalledTimes(1);
    const update = llmLoop.updateAgent.mock.calls[0][0] as { systemPrompt: string; tools: ToolDefinition[] };
    const toolNames = update.tools.map((t) => t.name);
    expect(toolNames).toContain('process_refund');
    expect(toolNames).toContain(USE_SKILL_TOOL_NAME);
    expect(
      handler.history.entries.some((e) => e.role === 'system' && e.text.includes("Activated skill 'refunds'")),
    ).toBe(true);
  });

  it('is idempotent — re-activating does not double-layer', async () => {
    const agent: AgentOptions = { systemPrompt: 'Support.', provider: 'pipeline', skills: [refundSkill()] };
    const handler = makeHandler(agent) as unknown as PrivateHandler;
    handler.llmLoop = { updateAgent: vi.fn() };

    await handler.performSkillActivation('refunds');
    const promptAfterFirst = handler.currentAgent.systemPrompt;
    const second = JSON.parse(await handler.performSkillActivation('refunds'));
    expect(second).toEqual({ status: 'already_active', skill: 'refunds' });
    expect(handler.currentAgent.systemPrompt).toBe(promptAfterFirst);
    expect(handler.currentAgent.systemPrompt.match(/# Skill: refunds/g)).toHaveLength(1);
  });

  it('rejects an unknown skill with an envelope and no swap', async () => {
    const agent: AgentOptions = { systemPrompt: 'Support.', provider: 'pipeline', skills: [refundSkill()] };
    const handler = makeHandler(agent) as unknown as PrivateHandler;
    const llmLoop = { updateAgent: vi.fn() };
    handler.llmLoop = llmLoop;

    const result = JSON.parse(await handler.performSkillActivation('nope'));
    expect(result.error).toContain("Unknown skill 'nope'");
    expect(result.available).toEqual(['refunds']);
    expect(llmLoop.updateAgent).not.toHaveBeenCalled();
    expect(handler.currentAgent.systemPrompt).not.toContain('# Skill:');
  });

  it('round-trips through the use_skill handler closure', async () => {
    const agent: AgentOptions = { systemPrompt: 'Support.', provider: 'pipeline', skills: [refundSkill()] };
    const handler = makeHandler(agent) as unknown as PrivateHandler;
    handler.llmLoop = { updateAgent: vi.fn() };

    const tools = handler.buildPipelineLlmTools();
    const useSkill = tools.find((t) => t.name === USE_SKILL_TOOL_NAME)!;
    expect(typeof useSkill.handler).toBe('function');
    const result = JSON.parse(
      await (useSkill.handler as (a: Record<string, unknown>, c: Record<string, unknown>) => Promise<string>)(
        { skill_name: 'refunds' },
        {},
      ),
    );
    expect(result).toEqual({ status: 'skill_activated', skill: 'refunds' });
  });
});

// ---------------------------------------------------------------------------
// Realtime handler — handleUseSkillFunctionCall
// ---------------------------------------------------------------------------

describe('realtime handleUseSkillFunctionCall', () => {
  function makeRealtimeAgent(): AgentOptions {
    return {
      systemPrompt: 'You are a support agent.',
      provider: 'openai_realtime',
      skills: [refundSkill()],
    };
  }

  it('sends session.update with the skill tools, then the function result', async () => {
    const handler = makeHandler(makeRealtimeAgent()) as unknown as PrivateHandler;
    const adapter = makeFakeAdapter();
    handler.adapter = adapter;

    await handler.handleUseSkillFunctionCall({
      call_id: 'fc-1',
      name: USE_SKILL_TOOL_NAME,
      arguments: JSON.stringify({ skill_name: 'refunds' }),
    });

    expect(adapter.updateSession).toHaveBeenCalledTimes(1);
    const update = adapter.updateSession.mock.calls[0][0] as {
      instructions: string;
      tools: Array<{ name: string }>;
    };
    expect(update.instructions).toContain('# Skill: refunds');
    expect(update.instructions).toContain(SKILL_INSTRUCTIONS);
    const toolNames = update.tools.map((t) => t.name);
    expect(toolNames).toContain('process_refund');
    expect(toolNames).toContain(USE_SKILL_TOOL_NAME);
    expect(toolNames).toContain('transfer_call');
    expect(toolNames).toContain('end_call');

    expect(adapter.sendFunctionResult).toHaveBeenCalledTimes(1);
    const [fcId, resultStr] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    expect(fcId).toBe('fc-1');
    expect(JSON.parse(resultStr)).toEqual({ status: 'skill_activated', skill: 'refunds' });
  });

  it('session.update precedes the function result', async () => {
    const handler = makeHandler(makeRealtimeAgent()) as unknown as PrivateHandler;
    const order: string[] = [];
    handler.adapter = {
      updateSession: vi.fn().mockImplementation(async () => order.push('update')),
      sendFunctionResult: vi.fn().mockImplementation(async () => order.push('result')),
    };
    await handler.handleUseSkillFunctionCall({
      call_id: 'fc-1',
      name: USE_SKILL_TOOL_NAME,
      arguments: JSON.stringify({ skill_name: 'refunds' }),
    });
    expect(order).toEqual(['update', 'result']);
  });

  it('unknown skill sends an envelope and never updates the session', async () => {
    const handler = makeHandler(makeRealtimeAgent()) as unknown as PrivateHandler;
    const adapter = makeFakeAdapter();
    handler.adapter = adapter;

    await handler.handleUseSkillFunctionCall({
      call_id: 'fc-2',
      name: USE_SKILL_TOOL_NAME,
      arguments: JSON.stringify({ skill_name: 'sales' }),
    });
    expect(adapter.updateSession).not.toHaveBeenCalled();
    const [, resultStr] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    const result = JSON.parse(resultStr);
    expect(result.error).toContain("Unknown skill 'sales'");
    expect(result.available).toEqual(['refunds']);
    expect(handler.currentAgent.systemPrompt).not.toContain('# Skill:');
  });

  it('already-active skill is an idempotent no-op (no second update)', async () => {
    const handler = makeHandler(makeRealtimeAgent()) as unknown as PrivateHandler;
    const adapter = makeFakeAdapter();
    handler.adapter = adapter;
    const fc = {
      call_id: 'fc-1',
      name: USE_SKILL_TOOL_NAME,
      arguments: JSON.stringify({ skill_name: 'refunds' }),
    };
    await handler.handleUseSkillFunctionCall(fc);
    adapter.updateSession.mockClear();
    adapter.sendFunctionResult.mockClear();
    await handler.handleUseSkillFunctionCall(fc);
    expect(adapter.updateSession).not.toHaveBeenCalled();
    const [, resultStr] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    expect(JSON.parse(resultStr)).toEqual({ status: 'already_active', skill: 'refunds' });
  });

  it('malformed arguments send an envelope and never update the session', async () => {
    const handler = makeHandler(makeRealtimeAgent()) as unknown as PrivateHandler;
    const adapter = makeFakeAdapter();
    handler.adapter = adapter;
    await handler.handleUseSkillFunctionCall({
      call_id: 'fc-3',
      name: USE_SKILL_TOOL_NAME,
      arguments: '{not json',
    });
    expect(adapter.updateSession).not.toHaveBeenCalled();
    const [, resultStr] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    expect(JSON.parse(resultStr).error).toBeDefined();
  });
});

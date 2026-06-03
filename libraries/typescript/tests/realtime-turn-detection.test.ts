import { describe, it, expect } from 'vitest';
import { Realtime as OpenAIRealtime } from '../src/engines/openai';
import { Realtime2 as OpenAIRealtime2 } from '../src/engines/openai-2';
import {
  OpenAIRealtimeAdapter,
  buildTurnDetection,
} from '../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import { buildAIAdapter, type LocalConfig } from '../src/server';
import type { AgentOptions, RealtimeTurnDetection } from '../src/types';

const CONFIG: LocalConfig = {
  phoneNumber: '+15555550100',
  webhookUrl: 'https://example.com/voice',
};

describe('[unit] RealtimeTurnDetection interface', () => {
  it('is readonly at compile time', () => {
    const td: RealtimeTurnDetection = { type: 'server_vad', threshold: 0.6 };
    // @ts-expect-error — fields are readonly.
    td.threshold = 0.7;
    expect(td.threshold).toBe(0.7); // assignment still happens at runtime
  });
});

describe('[unit] engine markers carry the new Realtime knobs', () => {
  it('OpenAIRealtime carries noiseReduction + turnDetection', () => {
    const engine = new OpenAIRealtime({
      apiKey: 'sk-test',
      noiseReduction: 'far_field',
      turnDetection: { type: 'server_vad', threshold: 0.6 },
    });
    expect(engine.noiseReduction).toBe('far_field');
    expect(engine.turnDetection).toEqual({ type: 'server_vad', threshold: 0.6 });
  });

  it('OpenAIRealtime2 carries noiseReduction + turnDetection', () => {
    const engine = new OpenAIRealtime2({
      apiKey: 'sk-test',
      noiseReduction: 'far_field',
      turnDetection: { type: 'semantic_vad', eagerness: 'low' },
    });
    expect(engine.noiseReduction).toBe('far_field');
    expect(engine.turnDetection).toEqual({ type: 'semantic_vad', eagerness: 'low' });
  });

  it('leaves both undefined by default (backward compat)', () => {
    const engine = new OpenAIRealtime2({ apiKey: 'sk-test' });
    expect(engine.noiseReduction).toBeUndefined();
    expect(engine.turnDetection).toBeUndefined();
  });

  it('rejects an invalid noiseReduction value in the engine constructor', () => {
    expect(
      () =>
        new OpenAIRealtime2({
          apiKey: 'sk-test',
          // @ts-expect-error — intentionally invalid for the runtime guard.
          noiseReduction: 'mid_field',
        }),
    ).toThrow(/noiseReduction/);
  });
});

describe('[unit] buildAIAdapter wires the knobs onto the adapter options', () => {
  it('forwards engine-level noiseReduction + turnDetection to the GA adapter', () => {
    const engine = new OpenAIRealtime2({
      apiKey: 'sk-test',
      noiseReduction: 'far_field',
      turnDetection: { type: 'server_vad', threshold: 0.6 },
    });
    const agent: AgentOptions = { systemPrompt: 'You are helpful.', engine };
    const adapter = buildAIAdapter(CONFIG, agent) as OpenAIRealtime2Adapter;
    expect(adapter).toBeInstanceOf(OpenAIRealtime2Adapter);
    const opts = (adapter as unknown as {
      options: { noiseReduction?: string; turnDetection?: RealtimeTurnDetection };
    }).options;
    expect(opts.noiseReduction).toBe('far_field');
    expect(opts.turnDetection).toEqual({ type: 'server_vad', threshold: 0.6 });
  });

  it('lets explicit agent options override the engine marker (agent wins)', () => {
    const engine = new OpenAIRealtime2({ apiKey: 'sk-test', noiseReduction: 'far_field' });
    const agent: AgentOptions = {
      systemPrompt: 'You are helpful.',
      engine,
      openaiRealtimeNoiseReduction: 'near_field',
    };
    const adapter = buildAIAdapter(CONFIG, agent) as OpenAIRealtime2Adapter;
    const opts = (adapter as unknown as { options: { noiseReduction?: string } }).options;
    expect(opts.noiseReduction).toBe('near_field');
  });

  it('forwards agent.realtimeTurnDetection to the v1 adapter', () => {
    const engine = new OpenAIRealtime({ apiKey: 'sk-test' });
    const agent: AgentOptions = {
      systemPrompt: 'You are helpful.',
      engine,
      realtimeTurnDetection: { type: 'semantic_vad', eagerness: 'low' },
    };
    const adapter = buildAIAdapter(CONFIG, agent) as OpenAIRealtimeAdapter;
    expect(adapter).toBeInstanceOf(OpenAIRealtimeAdapter);
    expect(adapter).not.toBeInstanceOf(OpenAIRealtime2Adapter);
    const opts = (adapter as unknown as { options: { turnDetection?: RealtimeTurnDetection } }).options;
    expect(opts.turnDetection).toEqual({ type: 'semantic_vad', eagerness: 'low' });
  });

  it('leaves adapter options unset when neither agent nor engine specify them (backward compat)', () => {
    const engine = new OpenAIRealtime2({ apiKey: 'sk-test' });
    const agent: AgentOptions = { systemPrompt: 'You are helpful.', engine };
    const adapter = buildAIAdapter(CONFIG, agent) as OpenAIRealtime2Adapter;
    const opts = (adapter as unknown as {
      options: { noiseReduction?: string; turnDetection?: RealtimeTurnDetection };
    }).options;
    expect(opts.noiseReduction).toBeUndefined();
    expect(opts.turnDetection).toBeUndefined();
  });
});

describe('[unit] RealtimeTurnDetection runtime validation (parity with Python __post_init__)', () => {
  it('rejects a bad type in the engine marker constructor', () => {
    expect(
      () =>
        new OpenAIRealtime2({
          apiKey: 'sk-test',
          // @ts-expect-error — intentionally invalid for the runtime guard.
          turnDetection: { type: 'magic_vad' },
        }),
    ).toThrow(/RealtimeTurnDetection\.type must be 'server_vad' or 'semantic_vad'/);
  });

  it('rejects a bad eagerness value', () => {
    expect(
      () =>
        new OpenAIRealtime({
          apiKey: 'sk-test',
          // @ts-expect-error — intentionally invalid for the runtime guard.
          turnDetection: { type: 'semantic_vad', eagerness: 'turbo' },
        }),
    ).toThrow(/eagerness must be one of low\|medium\|high\|auto/);
  });

  it('rejects eagerness on server_vad (only valid for semantic_vad)', () => {
    expect(
      () =>
        new OpenAIRealtime2({
          apiKey: 'sk-test',
          turnDetection: { type: 'server_vad', eagerness: 'low' },
        }),
    ).toThrow(/eagerness is only valid when type='semantic_vad'/);
  });

  it('buildTurnDetection also validates (covers the agent-only path that skips the engine marker)', () => {
    expect(() =>
      buildTurnDetection(
        // @ts-expect-error — intentionally invalid for the runtime guard.
        { type: 'magic_vad' },
        { defaultType: 'server_vad', defaultSilenceMs: 300, includeResponseGating: true },
      ),
    ).toThrow(/RealtimeTurnDetection\.type must be/);
  });

  it('accepts valid configs without throwing', () => {
    expect(
      () =>
        new OpenAIRealtime2({
          apiKey: 'sk-test',
          turnDetection: { type: 'semantic_vad', eagerness: 'low' },
        }),
    ).not.toThrow();
    expect(
      () =>
        new OpenAIRealtime2({
          apiKey: 'sk-test',
          turnDetection: { type: 'server_vad', threshold: 0.7 },
        }),
    ).not.toThrow();
  });
});

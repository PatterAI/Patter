/**
 * [unit] Generic ``CustomLLM`` provider.
 *
 * ``CustomLLM`` is the canonical "Custom LLM" name for the OpenAI-compatible
 * engine: one provider that works with Hermes, OpenClaw, Ollama/vLLM/LM
 * Studio, or any other endpoint speaking the OpenAI Chat Completions
 * protocol. The Hermes / OpenClaw presets subclass the same engine.
 *
 * Also covers ``sessionKeyFrom`` on the GENERIC provider (hoisted from the
 * Hermes preset so any header-scoped runtime gets per-caller memory).
 *
 * Parity with Python tests/test_llm_custom.py.
 */

import { describe, it, expect } from 'vitest';
import { CustomLLM, custom, HermesLLM } from '../../src/index';
import {
  OpenAICompatibleLLMProvider,
  hashCaller,
} from '../../src/llm/openai-compatible';
import type { SessionContext } from '../../src/types';

const BASE = { baseUrl: 'http://127.0.0.1:9000/v1', model: 'my-agent' };

type WithFactory = {
  sessionKeyFactory?: (ctx: SessionContext) => string | undefined;
};

describe('[unit] CustomLLM surface', () => {
  it('is the generic OpenAI-compatible engine', () => {
    expect(new CustomLLM(BASE)).toBeInstanceOf(OpenAICompatibleLLMProvider);
  });

  it('has its own provider key', () => {
    expect(CustomLLM.providerKey).toBe('custom');
  });

  it('namespace and named export are the same class', () => {
    expect(custom.LLM).toBe(CustomLLM);
  });

  it('old constructor shape still works (new fields are opt-in)', () => {
    const llm = new CustomLLM(BASE);
    expect(llm.model).toBe('my-agent');
  });

  it('accepts a keyless local gateway', () => {
    const llm = new CustomLLM({
      baseUrl: 'http://127.0.0.1:11434/v1',
      model: 'llama3.1',
    });
    expect(llm.model).toBe('llama3.1');
  });
});

describe('[unit] generic sessionKeyFrom', () => {
  it("the 'caller_hash' selector derives a per-caller key", () => {
    const llm = new CustomLLM({
      ...BASE,
      sessionKeyHeader: 'X-My-Memory-Key',
      sessionKeyFrom: 'caller_hash',
    }) as unknown as WithFactory;
    const caller = '+15551110000';
    const key = llm.sessionKeyFactory?.({ callerHash: hashCaller(caller) });
    expect(key).toBe(`patter-caller-${hashCaller(caller)}`);
  });

  it('the selector returns undefined without a caller hash', () => {
    const llm = new CustomLLM({
      ...BASE,
      sessionKeyHeader: 'X-My-Memory-Key',
      sessionKeyFrom: 'caller_hash',
    }) as unknown as WithFactory;
    expect(llm.sessionKeyFactory?.({})).toBeUndefined();
  });

  it('an explicit factory wins over the selector', () => {
    const llm = new CustomLLM({
      ...BASE,
      sessionKeyHeader: 'X-My-Memory-Key',
      sessionKeyFrom: 'caller_hash',
      sessionKeyFactory: () => 'explicit-key',
    }) as unknown as WithFactory;
    expect(llm.sessionKeyFactory?.({ callerHash: 'abc' })).toBe('explicit-key');
  });

  it('an invalid selector throws (runtime guard for dynamic JS callers)', () => {
    expect(
      () =>
        new CustomLLM({
          ...BASE,
          sessionKeyFrom: 'raw_number' as unknown as 'caller_hash',
        }),
    ).toThrow(/sessionKeyFrom/);
  });

  it('the Hermes preset delegates to the generic selector (regression)', () => {
    const llm = new HermesLLM({
      sessionKeyFrom: 'caller_hash',
    }) as unknown as WithFactory;
    const caller = '+15551110000';
    expect(llm.sessionKeyFactory?.({ callerHash: hashCaller(caller) })).toBe(
      `patter-caller-${hashCaller(caller)}`,
    );
  });

  it('the Hermes preset still rejects an invalid selector', () => {
    expect(
      () =>
        new HermesLLM({
          sessionKeyFrom: 'raw_number' as unknown as 'caller_hash',
        }),
    ).toThrow(/sessionKeyFrom/);
  });
});

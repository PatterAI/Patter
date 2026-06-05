/**
 * Tests for the ``hermes`` / ``openclaw`` / ``openaiCompatible`` namespace
 * objects (Feature #6), mirroring the Python ``from getpatter.llm import
 * hermes`` ergonomics.
 *
 * Real construction throughout — no mocks. Proves ``new hermes.LLM()`` builds
 * the SAME class as the existing ``HermesLLM`` named export, and that the named
 * exports still work alongside the namespaces.
 */

import { describe, expect, it } from 'vitest';
import {
  hermes,
  openclaw,
  openaiCompatible,
  HermesLLM,
  OpenClawLLM,
  OpenAICompatibleLLM,
} from '../src';

describe('[unit] LLM namespace exports', () => {
  it('hermes.LLM constructs the same class as the HermesLLM named export', () => {
    const fromNamespace = new hermes.LLM();
    expect(fromNamespace).toBeInstanceOf(HermesLLM);
    // Same constructor identity — the namespace re-exports the class, not a copy.
    expect(hermes.LLM).toBe(HermesLLM);
    expect(fromNamespace.model).toBe('hermes-agent');
  });

  it('openclaw.LLM constructs the same class as the OpenClawLLM named export', () => {
    const fromNamespace = new openclaw.LLM({ agent: 'x' });
    expect(fromNamespace).toBeInstanceOf(OpenClawLLM);
    expect(openclaw.LLM).toBe(OpenClawLLM);
    expect(fromNamespace.model).toBe('openclaw/x');
  });

  it('openaiCompatible.LLM constructs the same class as the named export', () => {
    const fromNamespace = new openaiCompatible.LLM({
      baseUrl: 'http://127.0.0.1:11434/v1',
      model: 'llama3.1',
    });
    expect(fromNamespace).toBeInstanceOf(OpenAICompatibleLLM);
    expect(openaiCompatible.LLM).toBe(OpenAICompatibleLLM);
    expect(fromNamespace.model).toBe('llama3.1');
  });

  it('openclaw.LLM enforces the same agent-id validation as the named export', () => {
    expect(() => new openclaw.LLM({ agent: 'a b' })).toThrow(/agent id/i);
  });
});

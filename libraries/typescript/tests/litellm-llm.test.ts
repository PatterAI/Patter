/**
 * Tests for the LiteLLM LLM provider (proxy mode).
 */

import { describe, expect, it } from 'vitest';

describe('LiteLLM LLM provider', () => {
  it('uses gpt-4o-mini as default model', async () => {
    const { LLM } = await import('../src/llm/litellm');
    const llm = new LLM({ baseUrl: 'http://localhost:4000/v1' });
    expect(llm.model).toBe('gpt-4o-mini');
  });

  it('honours explicit model override', async () => {
    const { LLM } = await import('../src/llm/litellm');
    const llm = new LLM({
      model: 'anthropic/claude-sonnet-4-6',
      baseUrl: 'http://localhost:4000/v1',
    });
    expect(llm.model).toBe('anthropic/claude-sonnet-4-6');
  });

  it('has litellm as providerKey', async () => {
    const { LLM } = await import('../src/llm/litellm');
    expect(LLM.providerKey).toBe('litellm');
  });

  it('accepts custom baseUrl', async () => {
    const { LLM } = await import('../src/llm/litellm');
    const llm = new LLM({
      baseUrl: 'https://my-proxy.example.com/v1',
      model: 'gpt-4o',
    });
    expect(llm.model).toBe('gpt-4o');
  });
});

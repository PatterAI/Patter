import { describe, it, expect } from 'vitest';
import { Patter } from '../src/client';
import { Twilio } from '../src/index';
import {
  applyToolCallPreambles,
  DEFAULT_TOOL_CALL_PREAMBLE_BLOCK,
} from '../src/stream-handler';

function makePatter(): Patter {
  return new Patter({
    carrier: new Twilio({ accountSid: 'AC_test', authToken: 'tok_test' }),
    openaiKey: 'sk_test',
    phoneNumber: '+15550000000',
    webhookUrl: 'abc.ngrok.io',
  });
}

/**
 * [unit] Pure helper — no WebSocket, no mocks. These are the cleanest
 * RED→GREEN tests: if `applyToolCallPreambles` is replaced with a no-op the
 * truthy cases below fail, and if it is replaced with an unconditional prepend
 * the falsy cases fail.
 */
describe('[unit] applyToolCallPreambles', () => {
  const PROMPT = 'You are a helpful receptionist.';

  it('returns the prompt byte-identical when knob is undefined (default)', () => {
    expect(applyToolCallPreambles(PROMPT, undefined)).toBe(PROMPT);
  });

  it('returns the prompt byte-identical when knob is false', () => {
    expect(applyToolCallPreambles(PROMPT, false)).toBe(PROMPT);
  });

  it('returns an empty prompt byte-identical when knob is false', () => {
    expect(applyToolCallPreambles('', false)).toBe('');
  });

  it('prepends the default block followed by a blank line when knob is true', () => {
    const out = applyToolCallPreambles(PROMPT, true);
    expect(out).toBe(`${DEFAULT_TOOL_CALL_PREAMBLE_BLOCK}\n\n${PROMPT}`);
    expect(out.startsWith('# Preambles')).toBe(true);
    // Verbatim OpenAI-approved action phrasing must survive in the block.
    expect(out).toContain("I'll check that order now.");
  });

  it('returns just the default block when knob is true and prompt is empty', () => {
    expect(applyToolCallPreambles('', true)).toBe(DEFAULT_TOOL_CALL_PREAMBLE_BLOCK);
  });

  it('uses a string knob verbatim as a full override (no default block text)', () => {
    const out = applyToolCallPreambles(PROMPT, 'CUSTOM PREAMBLE BLOCK');
    expect(out).toBe(`CUSTOM PREAMBLE BLOCK\n\n${PROMPT}`);
    expect(out.startsWith('CUSTOM PREAMBLE BLOCK')).toBe(true);
    expect(out).not.toContain("I'll check that order now.");
    expect(out).not.toContain('# Preambles');
  });

  it('returns just the override string when prompt is empty', () => {
    expect(applyToolCallPreambles('', 'CUSTOM')).toBe('CUSTOM');
  });
});

describe('[unit] AgentOptions.toolCallPreambles backward compat', () => {
  it('old shape (no kwarg) leaves toolCallPreambles undefined through agent()', () => {
    const patter = makePatter();
    const agent = patter.agent({ systemPrompt: 'x' });
    expect(agent.toolCallPreambles).toBeUndefined();
    // The system prompt is preserved unchanged — agent() must not inject the
    // block; that happens later at Realtime-instruction assembly.
    expect(agent.systemPrompt).toBe('x');
  });

  it('passes a true value through agent() verbatim', () => {
    const patter = makePatter();
    const agent = patter.agent({ systemPrompt: 'x', toolCallPreambles: true });
    expect(agent.toolCallPreambles).toBe(true);
  });

  it('passes a string override through agent() verbatim', () => {
    const patter = makePatter();
    const agent = patter.agent({
      systemPrompt: 'x',
      toolCallPreambles: '# Custom',
    });
    expect(agent.toolCallPreambles).toBe('# Custom');
  });
});

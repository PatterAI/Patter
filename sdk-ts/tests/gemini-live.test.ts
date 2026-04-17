import { describe, it, expect } from 'vitest';
import {
  GeminiLiveAdapter,
  GEMINI_DEFAULT_INPUT_SR,
  GEMINI_DEFAULT_OUTPUT_SR,
} from '../src/providers/gemini-live';

describe('GeminiLiveAdapter', () => {
  it('initializes with required api key and default options', () => {
    const adapter = new GeminiLiveAdapter('fake-key');
    expect(adapter).toBeDefined();
    expect(adapter.outputSampleRate).toBe(GEMINI_DEFAULT_OUTPUT_SR);
  });

  it('respects custom sample rates and model', () => {
    const adapter = new GeminiLiveAdapter('fake-key', {
      model: 'gemini-2.5-flash',
      voice: 'Aoede',
      inputSampleRate: 24000,
      outputSampleRate: 48000,
      temperature: 0.2,
    });
    expect(adapter).toBeDefined();
    expect(adapter.outputSampleRate).toBe(48000);
  });

  it('accepts tools array', () => {
    const adapter = new GeminiLiveAdapter('fake-key', {
      tools: [{ name: 't', description: 'desc', parameters: { type: 'object' } }],
    });
    expect(adapter).toBeDefined();
  });

  it('sendAudio does not throw when not connected', () => {
    const adapter = new GeminiLiveAdapter('fake-key');
    expect(() => adapter.sendAudio(Buffer.from('test'))).not.toThrow();
  });

  it('cancelResponse does not throw when not connected', () => {
    const adapter = new GeminiLiveAdapter('fake-key');
    expect(() => adapter.cancelResponse()).not.toThrow();
  });

  it('close is idempotent', async () => {
    const adapter = new GeminiLiveAdapter('fake-key');
    await adapter.close();
    await adapter.close();
    expect(true).toBe(true);
  });

  it('exports stable sample rate constants', () => {
    expect(GEMINI_DEFAULT_INPUT_SR).toBe(16000);
    expect(GEMINI_DEFAULT_OUTPUT_SR).toBe(24000);
  });

  it('emits events to registered handlers', async () => {
    const adapter = new GeminiLiveAdapter('fake-key');
    const received: Array<{ type: string; data: unknown }> = [];
    adapter.onEvent(async (type, data) => {
      received.push({ type, data });
    });

    // Reach into the private emit method so we can verify fan-out without a
    // real session. This is a unit test — integration tests exercise the
    // real genai session.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (adapter as any).emit('audio', Buffer.from('ab'));
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (adapter as any).emit('response_done', null);
    expect(received.map((r) => r.type)).toEqual(['audio', 'response_done']);
  });
});

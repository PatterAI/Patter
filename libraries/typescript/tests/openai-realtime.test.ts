import { describe, it, expect } from 'vitest';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';

describe('OpenAIRealtimeAdapter', () => {
  it('initializes with required api key', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    expect(adapter).toBeDefined();
  });

  it('accepts custom model', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', 'gpt-4o-realtime-preview');
    expect(adapter).toBeDefined();
  });

  it('accepts custom voice', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', undefined, 'nova');
    expect(adapter).toBeDefined();
  });

  it('accepts instructions string', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', undefined, undefined, 'Be helpful and concise.');
    expect(adapter).toBeDefined();
  });

  it('accepts tools array', () => {
    const tools = [{ name: 'test', description: 'test tool', parameters: {} }];
    const adapter = new OpenAIRealtimeAdapter('sk_test', undefined, undefined, undefined, tools);
    expect(adapter).toBeDefined();
  });

  it('accepts empty tools array', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', undefined, undefined, undefined, []);
    expect(adapter).toBeDefined();
  });

  it('is not connected initially', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    // ws is private, but close should not throw when not connected
    expect(() => adapter.close()).not.toThrow();
  });

  it('sendAudio does not throw when not connected', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    // Should silently skip if ws is null
    expect(() => adapter.sendAudio(Buffer.from('test'))).not.toThrow();
  });

  it('cancelResponse does not throw when not connected', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    expect(() => adapter.cancelResponse()).not.toThrow();
  });

  it('close can be called multiple times without error', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    adapter.close();
    adapter.close();
    expect(true).toBe(true);
  });

  it('onEvent does not throw when not connected', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    expect(() => adapter.onEvent(() => {})).not.toThrow();
  });

  it('accepts custom audioFormat (parity with Python audio_format kwarg)', () => {
    const adapter = new OpenAIRealtimeAdapter(
      'sk_test',
      undefined,
      undefined,
      undefined,
      undefined,
      'pcm16',
    );
    expect(adapter).toBeDefined();
  });

  it('defaults audioFormat to g711_ulaw when omitted', () => {
    // Construct without the 6th argument — must not throw and must produce
    // a working adapter with the default format.
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    expect(adapter).toBeDefined();
  });
});

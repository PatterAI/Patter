import { describe, it, expect } from 'vitest';
import {
  UltravoxRealtimeAdapter,
  ULTRAVOX_DEFAULT_API_BASE,
  ULTRAVOX_DEFAULT_SR,
} from '../src/providers/ultravox-realtime';

describe('UltravoxRealtimeAdapter', () => {
  it('initializes with default options', () => {
    const adapter = new UltravoxRealtimeAdapter('fake-key');
    expect(adapter).toBeDefined();
    expect(adapter.running).toBe(false);
  });

  it('respects custom options', () => {
    const adapter = new UltravoxRealtimeAdapter('fake-key', {
      model: 'fixie-ai/ultravox-v0_4',
      voice: 'en_male_brian',
      instructions: 'be brief',
      language: 'it',
      sampleRate: 8000,
    });
    expect(adapter).toBeDefined();
  });

  it('sendAudio does not throw when not connected', () => {
    const adapter = new UltravoxRealtimeAdapter('fake-key');
    expect(() => adapter.sendAudio(Buffer.from('ab'))).not.toThrow();
  });

  it('cancelResponse does not throw when not connected', () => {
    const adapter = new UltravoxRealtimeAdapter('fake-key');
    expect(() => adapter.cancelResponse()).not.toThrow();
  });

  it('close is idempotent', async () => {
    const adapter = new UltravoxRealtimeAdapter('fake-key');
    await adapter.close();
    await adapter.close();
    expect(true).toBe(true);
  });

  it('exports stable constants', () => {
    expect(ULTRAVOX_DEFAULT_SR).toBe(16000);
    expect(ULTRAVOX_DEFAULT_API_BASE).toMatch(/^https:\/\//);
  });

  it('handleMessage translates transcript events', async () => {
    const adapter = new UltravoxRealtimeAdapter('fake-key');
    const events: Array<{ type: string; data: unknown }> = [];
    adapter.onEvent(async (type, data) => {
      events.push({ type, data });
    });

    const userJson = JSON.stringify({
      type: 'transcript',
      role: 'user',
      text: 'hello',
      final: true,
    });
    const toolJson = JSON.stringify({
      type: 'client_tool_invocation',
      invocationId: 'inv-1',
      toolName: 'weather',
      parameters: { city: 'Rome' },
    });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (adapter as any).handleMessage(Buffer.from(userJson), false);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (adapter as any).handleMessage(Buffer.from(toolJson), false);

    expect(events[0].type).toBe('transcript_input');
    expect(events[0].data).toBe('hello');
    expect(events[1].type).toBe('function_call');
    const fc = events[1].data as { call_id: string; name: string; arguments: string };
    expect(fc.call_id).toBe('inv-1');
    expect(fc.name).toBe('weather');
    expect(JSON.parse(fc.arguments)).toEqual({ city: 'Rome' });
  });

  it('handleMessage forwards binary frames as audio', async () => {
    const adapter = new UltravoxRealtimeAdapter('fake-key');
    const events: Array<{ type: string; data: unknown }> = [];
    adapter.onEvent(async (type, data) => {
      events.push({ type, data });
    });

    const pcm = Buffer.from([0, 1, 2, 3]);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (adapter as any).handleMessage(pcm, true);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('audio');
    expect(Buffer.isBuffer(events[0].data)).toBe(true);
    expect((events[0].data as Buffer).equals(pcm)).toBe(true);
  });
});

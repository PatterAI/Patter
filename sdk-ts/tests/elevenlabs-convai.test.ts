import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ElevenLabsConvAIAdapter } from '../src/providers/elevenlabs-convai';

// Mock the 'ws' module so no real network connections are made
vi.mock('ws', () => {
  const EventEmitter = require('events');

  class MockWebSocket extends EventEmitter {
    static OPEN = 1;
    readyState = MockWebSocket.OPEN;
    sent: string[] = [];

    send(data: string) {
      this.sent.push(data);
    }

    close() {
      this.readyState = 3; // CLOSED
      this.emit('close');
    }
  }

  return { default: MockWebSocket };
});

describe('ElevenLabsConvAIAdapter', () => {
  it('initializes with defaults', () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    expect(adapter).toBeDefined();
  });

  it('initializes with all options', () => {
    const adapter = new ElevenLabsConvAIAdapter(
      'el_key',
      'agent_123',
      'some_voice_id',
      'Ciao!',
    );
    expect(adapter).toBeDefined();
  });

  it('connect() sends conversation_initiation_client_data with voice_id', async () => {
    const WS = (await import('ws')).default as unknown as { new (...args: unknown[]): {
      readyState: number;
      sent: string[];
      send: (d: string) => void;
      close: () => void;
      emit: (event: string, ...args: unknown[]) => void;
      on: (event: string, cb: (...args: unknown[]) => void) => void;
      once: (event: string, cb: (...args: unknown[]) => void) => void;
    }};

    const adapter = new ElevenLabsConvAIAdapter('el_key', '', 'my_voice');

    // Trigger the open event asynchronously
    const connectPromise = adapter.connect();
    // Grab the singleton instance that was created — the mock emits 'open' lazily
    // We emit 'open' on the mock after connect() starts
    const instance = (adapter as unknown as { ws: { emit: (e: string) => void; sent: string[] } }).ws;
    instance.emit('open');

    await connectPromise;

    const initMsg = JSON.parse(instance.sent[0]) as {
      type: string;
      conversation_config_override: { tts: { voice_id: string } };
    };
    expect(initMsg.type).toBe('conversation_initiation_client_data');
    expect(initMsg.conversation_config_override.tts.voice_id).toBe('my_voice');
  });

  it('connect() includes agent first_message when provided', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key', '', undefined, 'Hello!');

    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as { ws: { emit: (e: string) => void; sent: string[] } }).ws;
    instance.emit('open');
    await connectPromise;

    const initMsg = JSON.parse(instance.sent[0]) as {
      conversation_config_override: { agent?: { first_message?: string } };
    };
    expect(initMsg.conversation_config_override.agent?.first_message).toBe('Hello!');
  });

  it('connect() does NOT include agent block when no first_message', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');

    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as { ws: { emit: (e: string) => void; sent: string[] } }).ws;
    instance.emit('open');
    await connectPromise;

    const initMsg = JSON.parse(instance.sent[0]) as {
      conversation_config_override: { agent?: unknown };
    };
    expect(initMsg.conversation_config_override.agent).toBeUndefined();
  });

  it('sendAudio() sends base64-encoded audio message', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as { ws: { emit: (e: string) => void; sent: string[] } }).ws;
    instance.emit('open');
    await connectPromise;

    const audioBytes = Buffer.from('hello audio', 'utf-8');
    adapter.sendAudio(audioBytes);

    const audioMsg = JSON.parse(instance.sent[1]) as { type: string; audio: string };
    expect(audioMsg.type).toBe('audio');
    expect(audioMsg.audio).toBe(audioBytes.toString('base64'));
  });

  it('onEvent() routes audio events', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as {
      ws: {
        emit: (e: string, ...args: unknown[]) => void;
        sent: string[];
        on: (e: string, cb: (...args: unknown[]) => void) => void;
      };
    }).ws;
    instance.emit('open');
    await connectPromise;

    const events: Array<{ type: string; data: unknown }> = [];
    adapter.onEvent((type, data) => events.push({ type, data }));

    const fakeAudioB64 = Buffer.from('pcm-data').toString('base64');
    instance.emit('message', JSON.stringify({ type: 'audio', audio: fakeAudioB64 }));

    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('audio');
    expect((events[0].data as Buffer).toString()).toBe('pcm-data');
  });

  it('onEvent() routes user_transcript events', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as {
      ws: { emit: (e: string, ...args: unknown[]) => void; sent: string[] };
    }).ws;
    instance.emit('open');
    await connectPromise;

    const events: Array<{ type: string; data: unknown }> = [];
    adapter.onEvent((type, data) => events.push({ type, data }));

    instance.emit('message', JSON.stringify({ type: 'user_transcript', text: 'hi there' }));

    expect(events[0]).toEqual({ type: 'transcript_input', data: 'hi there' });
  });

  it('onEvent() routes agent_response events', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as {
      ws: { emit: (e: string, ...args: unknown[]) => void; sent: string[] };
    }).ws;
    instance.emit('open');
    await connectPromise;

    const events: Array<{ type: string; data: unknown }> = [];
    adapter.onEvent((type, data) => events.push({ type, data }));

    instance.emit('message', JSON.stringify({ type: 'agent_response', text: 'Hello, how can I help?' }));

    expect(events[0]).toEqual({ type: 'transcript_output', data: 'Hello, how can I help?' });
  });

  it('onEvent() routes interruption events', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as {
      ws: { emit: (e: string, ...args: unknown[]) => void; sent: string[] };
    }).ws;
    instance.emit('open');
    await connectPromise;

    const events: Array<{ type: string; data: unknown }> = [];
    adapter.onEvent((type, data) => events.push({ type, data }));

    instance.emit('message', JSON.stringify({ type: 'interruption' }));

    expect(events[0]).toEqual({ type: 'interruption', data: null });
  });

  it('close() nullifies ws and callback', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as { ws: { emit: (e: string) => void } }).ws;
    instance.emit('open');
    await connectPromise;

    adapter.onEvent(() => {});
    adapter.close();

    expect((adapter as unknown as { ws: unknown }).ws).toBeNull();
    expect((adapter as unknown as { eventCallback: unknown }).eventCallback).toBeNull();
  });

  it('sendAudio() is a no-op when ws is null', () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    // ws is null — should not throw
    expect(() => adapter.sendAudio(Buffer.from('test'))).not.toThrow();
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ElevenLabsConvAIAdapter } from '../src/providers/elevenlabs-convai';

// Mock the 'ws' module so no real network connections are made
vi.mock('ws', () => {
  const EventEmitter = require('events');

  class MockWebSocket extends EventEmitter {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    readyState = MockWebSocket.OPEN;
    sent: string[] = [];

    send(data: string) {
      this.sent.push(data);
    }

    close() {
      this.readyState = MockWebSocket.CLOSED;
      this.emit('close', 1000, Buffer.from(''));
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

  it('connect() includes agent.language from constructor default', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');

    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as { ws: { emit: (e: string) => void; sent: string[] } }).ws;
    instance.emit('open');
    await connectPromise;

    const initMsg = JSON.parse(instance.sent[0]) as {
      conversation_config_override: { agent?: { language?: string; first_message?: string } };
    };
    // Default language is plumbed through (previously stored but never sent).
    expect(initMsg.conversation_config_override.agent?.language).toBe('it');
    expect(initMsg.conversation_config_override.agent?.first_message).toBeUndefined();
  });

  it('sendAudio() sends user_audio_chunk payload', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as { ws: { emit: (e: string) => void; sent: string[] } }).ws;
    instance.emit('open');
    await connectPromise;

    const audioBytes = Buffer.from('hello audio', 'utf-8');
    adapter.sendAudio(audioBytes);

    const audioMsg = JSON.parse(instance.sent[1]) as { user_audio_chunk: string };
    expect(audioMsg.user_audio_chunk).toBe(audioBytes.toString('base64'));
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

  it('onEvent() routes agent_response events and emits response_start (not response_done)', async () => {
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

    // agent_response now yields two events and does NOT immediately close the
    // turn with response_done (that's gated on silence or interruption).
    expect(events[0]).toEqual({ type: 'transcript_output', data: 'Hello, how can I help?' });
    expect(events[1].type).toBe('response_start');
    expect(events.find((e) => e.type === 'response_done')).toBeUndefined();
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
    await adapter.close();

    expect((adapter as unknown as { ws: unknown }).ws).toBeNull();
    expect((adapter as unknown as { eventCallback: unknown }).eventCallback).toBeNull();
  });

  it('sendAudio() is a no-op when ws is null', () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    // ws is null — should not throw
    expect(() => adapter.sendAudio(Buffer.from('test'))).not.toThrow();
  });

  it('replies to ping with pong carrying event_id', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as {
      ws: { emit: (e: string, ...args: unknown[]) => void; sent: string[] };
    }).ws;
    instance.emit('open');
    await connectPromise;

    instance.emit(
      'message',
      JSON.stringify({ type: 'ping', ping_event: { event_id: 'abc-123', ping_ms: 0 } }),
    );

    // Pong should be sent synchronously when ping_ms is 0.
    const pong = instance.sent.find((s) => s.includes('"pong"'));
    expect(pong).toBeDefined();
    expect(JSON.parse(pong!)).toEqual({ type: 'pong', event_id: 'abc-123' });
  });

  it('captures conversation_id from conversation_initiation_metadata', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as {
      ws: { emit: (e: string, ...args: unknown[]) => void; sent: string[] };
    }).ws;
    instance.emit('open');
    await connectPromise;

    instance.emit(
      'message',
      JSON.stringify({
        type: 'conversation_initiation_metadata',
        conversation_initiation_metadata_event: {
          conversation_id: 'conv_abc',
          agent_output_audio_format: 'pcm_16000',
          user_input_audio_format: 'pcm_8000',
        },
      }),
    );

    expect(adapter.conversationId).toBe('conv_abc');
    expect(adapter.agentOutputAudioFormat).toBe('pcm_16000');
    expect(adapter.userInputAudioFormat).toBe('pcm_8000');
  });

  it('error events route to the error callback (not just logs)', async () => {
    const adapter = new ElevenLabsConvAIAdapter('el_key');
    const connectPromise = adapter.connect();
    const instance = (adapter as unknown as {
      ws: { emit: (e: string, ...args: unknown[]) => void; sent: string[] };
    }).ws;
    instance.emit('open');
    await connectPromise;

    const events: Array<{ type: string; data: unknown }> = [];
    adapter.onEvent((type, data) => events.push({ type, data }));

    instance.emit('message', JSON.stringify({ type: 'error', message: 'boom' }));

    const errEvt = events.find((e) => e.type === 'error');
    expect(errEvt).toBeDefined();
    expect(errEvt!.data).toBe('boom');
  });

  // ----------------------------------------------------------------
  // Telephony factories — native μ-law 8 kHz negotiation
  // ----------------------------------------------------------------

  describe('telephony factories', () => {
    it('forTwilio() negotiates ulaw_8000 on both directions', () => {
      const adapter = ElevenLabsConvAIAdapter.forTwilio('el_key', 'agent_123');
      expect(adapter.outputAudioFormat).toBe('ulaw_8000');
      expect(adapter.inputAudioFormat).toBe('ulaw_8000');
    });

    it('forTwilio() respects optional overrides', () => {
      const adapter = ElevenLabsConvAIAdapter.forTwilio('el_key', 'agent_xyz', {
        voiceId: 'custom_voice',
        language: 'en',
        firstMessage: 'Hi!',
      });
      expect(adapter.outputAudioFormat).toBe('ulaw_8000');
      expect(adapter.inputAudioFormat).toBe('ulaw_8000');
    });

    it('forTelnyx() negotiates ulaw_8000 on both directions', () => {
      const adapter = ElevenLabsConvAIAdapter.forTelnyx('el_key', 'agent_456');
      expect(adapter.outputAudioFormat).toBe('ulaw_8000');
      expect(adapter.inputAudioFormat).toBe('ulaw_8000');
    });

    it('bare constructor leaves audio formats undefined (server PCM16 default)', () => {
      const adapter = new ElevenLabsConvAIAdapter('el_key', 'agent_x');
      expect(adapter.outputAudioFormat).toBeUndefined();
      expect(adapter.inputAudioFormat).toBeUndefined();
    });

    it('forTwilio() sends ulaw_8000 in conversation_config_override on connect()', async () => {
      const adapter = ElevenLabsConvAIAdapter.forTwilio('el_key', 'agent_x');
      const connectPromise = adapter.connect();
      const instance = (adapter as unknown as {
        ws: { emit: (e: string) => void; sent: string[] };
      }).ws;
      instance.emit('open');
      await connectPromise;

      const initMsg = JSON.parse(instance.sent[0]) as {
        conversation_config_override: {
          tts: { voice_id: string; output_format?: string };
          asr?: { input_format?: string };
        };
      };
      expect(initMsg.conversation_config_override.tts.output_format).toBe('ulaw_8000');
      expect(initMsg.conversation_config_override.asr?.input_format).toBe('ulaw_8000');
    });
  });
});

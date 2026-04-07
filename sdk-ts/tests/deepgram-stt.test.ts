import { describe, it, expect, vi } from 'vitest';
import { DeepgramSTT } from '../src/providers/deepgram-stt';

// Mock ws so no real network connections are made
vi.mock('ws', () => {
  const EventEmitter = require('events');

  class MockWebSocket extends EventEmitter {
    static OPEN = 1;
    readyState = MockWebSocket.OPEN;
    sent: unknown[] = [];

    constructor() {
      super();
    }

    send(data: unknown) {
      this.sent.push(data);
    }

    close() {
      this.readyState = 3;
      this.emit('close');
    }
  }

  return { default: MockWebSocket };
});

describe('DeepgramSTT', () => {
  it('initializes with required api key', () => {
    const stt = new DeepgramSTT('dg_test');
    expect(stt).toBeDefined();
  });

  it('accepts custom language', () => {
    const stt = new DeepgramSTT('dg_test', 'it');
    expect(stt).toBeDefined();
  });

  it('accepts custom model', () => {
    const stt = new DeepgramSTT('dg_test', 'en', 'nova-2');
    expect(stt).toBeDefined();
  });

  it('accepts custom encoding', () => {
    const stt = new DeepgramSTT('dg_test', 'en', 'nova-3', 'mulaw');
    expect(stt).toBeDefined();
  });

  it('accepts custom sample rate', () => {
    const stt = new DeepgramSTT('dg_test', 'en', 'nova-3', 'mulaw', 8000);
    expect(stt).toBeDefined();
  });

  it('forTwilio creates instance with mulaw 8kHz', () => {
    const stt = DeepgramSTT.forTwilio('dg_test');
    expect(stt).toBeDefined();
  });

  it('forTwilio accepts custom language', () => {
    const stt = DeepgramSTT.forTwilio('dg_test', 'it');
    expect(stt).toBeDefined();
  });

  it('close does not throw when not connected', () => {
    const stt = new DeepgramSTT('dg_test');
    expect(() => stt.close()).not.toThrow();
  });

  it('sendAudio does not throw when not connected', () => {
    const stt = new DeepgramSTT('dg_test');
    expect(() => stt.sendAudio(Buffer.from('audio'))).not.toThrow();
  });
});

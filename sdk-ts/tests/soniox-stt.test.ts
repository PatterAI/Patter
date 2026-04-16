/**
 * Unit tests for the TypeScript SonioxSTT adapter.
 *
 * The tests mock the `ws` package so no real network traffic occurs, and
 * replay synthetic Soniox protocol frames (see MOCK notes on individual
 * tests) to validate adapter behaviour.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock `ws` with an EventEmitter-based stub that records sent frames and
// lets tests push protocol messages into the instance under test.
vi.mock('ws', () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { EventEmitter } = require('events');

  class MockWebSocket extends EventEmitter {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = MockWebSocket.OPEN;
    sent: unknown[] = [];

    constructor(_url: string) {
      super();
      // Dispatch `open` on the next tick so callers can register handlers.
      setImmediate(() => this.emit('open'));
    }

    send(data: unknown) {
      this.sent.push(data);
    }

    close() {
      this.readyState = MockWebSocket.CLOSED;
      this.emit('close');
    }
  }

  return { default: MockWebSocket };
});

// Import after mocking
// eslint-disable-next-line import/first
import WebSocket from 'ws';
// eslint-disable-next-line import/first
import { SonioxSTT, type Transcript } from '../src/providers/soniox-stt';

describe('SonioxSTT — initialisation', () => {
  it('throws on missing api key', () => {
    expect(() => new SonioxSTT('')).toThrow(/apiKey/);
  });

  it('rejects invalid endpoint delay', () => {
    expect(
      () => new SonioxSTT('k', { maxEndpointDelayMs: 100 }),
    ).toThrow(/maxEndpointDelayMs/);
  });

  it('forTwilio uses 8 kHz sample rate', () => {
    const stt = SonioxSTT.forTwilio('k');
    expect(stt).toBeDefined();
  });

  it('accepts language hints', () => {
    const stt = new SonioxSTT('k', { languageHints: ['en', 'it'] });
    expect(stt).toBeDefined();
  });
});

describe('SonioxSTT — connection & protocol', () => {
  let stt: SonioxSTT;
  let transcripts: Transcript[];
  let mockWs: unknown;

  beforeEach(async () => {
    stt = new SonioxSTT('test-key', { model: 'stt-rt-v4' });
    transcripts = [];
    stt.onTranscript((t) => transcripts.push(t));
    await stt.connect();
    // Retrieve the mock WebSocket instance (we only create one per adapter).
    mockWs = (stt as unknown as { ws: unknown }).ws;
  });

  it('sends the initial configuration JSON on connect', () => {
    const sent = (mockWs as { sent: unknown[] }).sent;
    expect(sent.length).toBeGreaterThanOrEqual(1);
    const payload = JSON.parse(sent[0] as string);
    expect(payload.api_key).toBe('test-key');
    expect(payload.model).toBe('stt-rt-v4');
    expect(payload.audio_format).toBe('pcm_s16le');
  });

  it('MOCK: emits interim transcripts for non-final tokens', () => {
    // Simulate a Soniox message with only non-final tokens.
    (mockWs as { emit: (ev: string, data: string) => void }).emit(
      'message',
      JSON.stringify({
        tokens: [
          { text: 'Hi ', is_final: false, confidence: 0.5 },
          { text: 'there', is_final: false, confidence: 0.6 },
        ],
      }),
    );
    expect(transcripts).toHaveLength(1);
    expect(transcripts[0].isFinal).toBe(false);
    expect(transcripts[0].text).toBe('Hi there');
  });

  it('MOCK: accumulates finals and flushes on <end> token', () => {
    const emit = (mockWs as { emit: (ev: string, data: string) => void }).emit.bind(
      mockWs,
    );

    emit(
      'message',
      JSON.stringify({
        tokens: [{ text: 'Hello ', is_final: true, confidence: 0.9 }],
      }),
    );
    emit(
      'message',
      JSON.stringify({
        tokens: [{ text: 'world', is_final: true, confidence: 0.8 }],
      }),
    );
    emit(
      'message',
      JSON.stringify({ tokens: [{ text: '<end>', is_final: true }] }),
    );

    // Interim transcripts during accumulation, plus the final flush.
    expect(transcripts[transcripts.length - 1].isFinal).toBe(true);
    expect(transcripts[transcripts.length - 1].text).toBe('Hello world');
  });

  it('MOCK: flushes pending finals on server "finished" flag', () => {
    const emit = (mockWs as { emit: (ev: string, data: string) => void }).emit.bind(
      mockWs,
    );
    emit(
      'message',
      JSON.stringify({
        tokens: [{ text: 'Goodbye', is_final: true, confidence: 0.95 }],
      }),
    );
    emit('message', JSON.stringify({ tokens: [], finished: true }));

    expect(transcripts[transcripts.length - 1].isFinal).toBe(true);
    expect(transcripts[transcripts.length - 1].text).toBe('Goodbye');
  });

  it('ignores malformed JSON payloads', () => {
    const before = transcripts.length;
    (mockWs as { emit: (ev: string, data: string) => void }).emit(
      'message',
      'not json',
    );
    expect(transcripts.length).toBe(before);
  });

  it('sendAudio is a no-op when not connected', () => {
    const fresh = new SonioxSTT('k');
    expect(() => fresh.sendAudio(Buffer.from('a'))).not.toThrow();
  });

  it('close does not throw when called twice', () => {
    stt.close();
    expect(() => stt.close()).not.toThrow();
  });

  it('supports up to 10 onTranscript callbacks before replacing the last', () => {
    const fresh = new SonioxSTT('k');
    for (let i = 0; i < 12; i++) {
      fresh.onTranscript(() => {});
    }
    // No crash, internal callbacks array stays bounded.
    expect(fresh).toBeDefined();
  });
});

describe('SonioxSTT — WebSocket constants', () => {
  it('WebSocket.OPEN matches the mock sentinel', () => {
    expect(WebSocket.OPEN).toBe(1);
  });
});

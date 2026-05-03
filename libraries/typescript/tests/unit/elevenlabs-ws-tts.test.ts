/**
 * Tests for ElevenLabsWebSocketTTS — construction, factories, URL build,
 * and a minimal end-to-end run with a mocked WebSocket.
 *
 * Heavier WS lifecycle coverage (reconnect, 5-context limit, ULAW binary
 * frames, isFinal terminator semantics) lives in the parity / integration
 * suite — this file keeps unit tests fast and synchronous.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock the ``ws`` module BEFORE importing the class under test so the
// `import WebSocket from 'ws'` line picks up our fake. Use a stand-alone
// EventEmitter polyfill defined inside the factory so vitest's hoisting
// doesn't reach for an outer import.
vi.mock('ws', () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { EventEmitter } = require('events');
  class FakeWebSocket extends EventEmitter {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = FakeWebSocket.CONNECTING;
    sent: string[] = [];
    constructor(public url: string, public opts?: unknown) {
      super();
      // Track every constructed instance so individual tests can drive them.
      (FakeWebSocket as unknown as { instances: FakeWebSocket[] }).instances.push(this);
      setImmediate(() => {
        this.readyState = FakeWebSocket.OPEN;
        this.emit('open');
      });
    }
    send(data: string): void {
      this.sent.push(data);
    }
    close(): void {
      this.readyState = 3;
      this.emit('close');
    }
  }
  (FakeWebSocket as unknown as { instances: FakeWebSocket[] }).instances = [];
  return { default: FakeWebSocket };
});

import { ElevenLabsWebSocketTTS } from '../../src/providers/elevenlabs-ws-tts';
import WebSocketDefault from 'ws';

const FakeWS = WebSocketDefault as unknown as { instances: { sent: string[]; emit: (e: string, p?: unknown) => void; close: () => void }[] };

beforeEach(() => {
  FakeWS.instances.length = 0;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ElevenLabsWebSocketTTS — construction', () => {
  it('creates with required apiKey', () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'el-key' });
    expect(tts.apiKey).toBe('el-key');
    expect(tts.modelId).toBe('eleven_flash_v2_5');
    expect(tts.outputFormat).toBe('pcm_16000');
    expect(tts.autoMode).toBe(true);
  });

  it('accepts custom voiceId, modelId, outputFormat', () => {
    const tts = new ElevenLabsWebSocketTTS({
      apiKey: 'k',
      voiceId: 'custom-voice-id',
      modelId: 'eleven_turbo_v2_5',
      outputFormat: 'ulaw_8000',
    });
    expect(tts.modelId).toBe('eleven_turbo_v2_5');
    expect(tts.outputFormat).toBe('ulaw_8000');
  });

  it('rejects eleven_v3 (not supported on stream-input)', () => {
    expect(() =>
      new ElevenLabsWebSocketTTS({ apiKey: 'k', modelId: 'eleven_v3' }),
    ).toThrow(/eleven_v3 is not supported/);
  });

  it('forTwilio sets ulaw_8000 + low-bandwidth voice settings', () => {
    const tts = ElevenLabsWebSocketTTS.forTwilio({ apiKey: 'k' });
    expect(tts.outputFormat).toBe('ulaw_8000');
    expect(tts.voiceSettings).toEqual({
      stability: 0.6,
      similarity_boost: 0.75,
      use_speaker_boost: false,
    });
  });

  it('forTelnyx sets pcm_16000', () => {
    const tts = ElevenLabsWebSocketTTS.forTelnyx({ apiKey: 'k' });
    expect(tts.outputFormat).toBe('pcm_16000');
  });

  it('forTwilio with explicit voiceSettings preserves them', () => {
    const tts = ElevenLabsWebSocketTTS.forTwilio({
      apiKey: 'k',
      voiceSettings: { stability: 0.9, similarity_boost: 0.5 },
    });
    expect(tts.voiceSettings).toEqual({ stability: 0.9, similarity_boost: 0.5 });
  });
});

describe('ElevenLabsWebSocketTTS — URL build', () => {
  it('includes model_id, output_format, inactivity_timeout, auto_mode', () => {
    const tts = new ElevenLabsWebSocketTTS({
      apiKey: 'k',
      voiceId: 'voice-123',
      modelId: 'eleven_flash_v2_5',
      outputFormat: 'pcm_16000',
      autoMode: true,
    });
    const url: string = (tts as unknown as { buildUrl(): string }).buildUrl();
    expect(url).toContain('voice-123');
    expect(url).toContain('model_id=eleven_flash_v2_5');
    expect(url).toContain('output_format=pcm_16000');
    expect(url).toContain('inactivity_timeout=60');
    expect(url).toContain('auto_mode=true');
  });

  it('omits auto_mode when disabled', () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k', autoMode: false });
    const url: string = (tts as unknown as { buildUrl(): string }).buildUrl();
    expect(url).not.toContain('auto_mode');
  });

  it('includes language_code when set', () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k', languageCode: 'it' });
    const url: string = (tts as unknown as { buildUrl(): string }).buildUrl();
    expect(url).toContain('language_code=it');
  });
});

describe('ElevenLabsWebSocketTTS — carrier auto-flip', () => {
  it('twilio carrier flips default format to ulaw_8000', () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k' });
    expect(tts.outputFormat).toBe('pcm_16000');
    tts.setTelephonyCarrier('twilio');
    expect(tts.outputFormat).toBe('ulaw_8000');
  });

  it('twilio carrier auto-flip is reflected in WS connect URL', () => {
    // End-to-end: after carrier auto-flip, the URL must request ulaw_8000
    // so ElevenLabs encodes server-side and we skip client-side transcode.
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k', voiceId: 'v1' });
    tts.setTelephonyCarrier('twilio');
    const url: string = (tts as unknown as { buildUrl(): string }).buildUrl();
    expect(url).toContain('output_format=ulaw_8000');
  });

  it('telnyx carrier keeps default pcm_16000', () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k' });
    tts.setTelephonyCarrier('telnyx');
    expect(tts.outputFormat).toBe('pcm_16000');
  });

  it('explicit outputFormat is respected over carrier hint', () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k', outputFormat: 'pcm_16000' });
    tts.setTelephonyCarrier('twilio');
    expect(tts.outputFormat).toBe('pcm_16000');
  });

  it('explicit ulaw_8000 is preserved on telnyx carrier hint', () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k', outputFormat: 'ulaw_8000' });
    tts.setTelephonyCarrier('telnyx');
    expect(tts.outputFormat).toBe('ulaw_8000');
  });

  it('forTwilio factory counts as explicit (carrier hint is no-op)', () => {
    const tts = ElevenLabsWebSocketTTS.forTwilio({ apiKey: 'k' });
    tts.setTelephonyCarrier('telnyx');
    expect(tts.outputFormat).toBe('ulaw_8000');
  });

  it('unknown carrier is a no-op', () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k' });
    tts.setTelephonyCarrier('custom');
    expect(tts.outputFormat).toBe('pcm_16000');
    tts.setTelephonyCarrier('');
    expect(tts.outputFormat).toBe('pcm_16000');
  });
});

describe('ElevenLabsWebSocketTTS — synthesizeStream protocol', () => {
  it('sends init, text+flush at start; EOS only at finally; yields decoded audio', async () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k' });

    const collected: Buffer[] = [];
    const done = (async () => {
      for await (const buf of tts.synthesizeStream('Hello world')) collected.push(buf);
    })();

    // After WS opens, only init + text+flush should have been sent. EOS is
    // deferred to ``finally`` to avoid truncating tail audio under auto_mode.
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));

    const ws = FakeWS.instances[0];
    expect(ws).toBeDefined();
    const sentDuringSynth = (ws as unknown as { sent: string[] }).sent.slice();
    expect(sentDuringSynth).toHaveLength(2);
    const [init, payload] = sentDuringSynth.map((s) => JSON.parse(s));
    expect(init).toMatchObject({ text: ' ' });
    expect(payload).toEqual({ text: 'Hello world ', flush: true });

    const audioBytes = Buffer.from([0x00, 0x01, 0x02, 0x03]);
    ws.emit('message', Buffer.from(JSON.stringify({ audio: audioBytes.toString('base64') })));
    ws.emit('message', Buffer.from(JSON.stringify({ isFinal: true })));

    await done;
    expect(Buffer.concat(collected)).toEqual(audioBytes);

    // EOS is sent in finally — verify it landed AFTER the audio drain.
    const sentAfterFinally = (ws as unknown as { sent: string[] }).sent;
    expect(sentAfterFinally).toHaveLength(3);
    expect(JSON.parse(sentAfterFinally[2])).toEqual({ text: '' });
  });

  it('terminates on socket close even without isFinal', async () => {
    const tts = new ElevenLabsWebSocketTTS({ apiKey: 'k' });

    const collected: Buffer[] = [];
    const done = (async () => {
      for await (const buf of tts.synthesizeStream('hi')) collected.push(buf);
    })();

    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
    FakeWS.instances[0].emit('close');

    await done;
    expect(collected).toEqual([]);
  });
});

import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { decode, encode } from '@msgpack/msgpack';
import { WebSocketServer, type WebSocket as WSSocket } from 'ws';
import type { AddressInfo } from 'node:net';
import {
  FishAudioWebSocketTTS,
  WS_SUPPORTED_MODELS,
  decodeServerFrame,
  type MsgpackCodec,
} from '../../src/providers/fish-audio-ws-tts';
import { WebSocketTTS as FishAudioPipelineWSTTS } from '../../src/tts/fish-audio';

/**
 * [mocked] Fish Audio WebSocket TTS.
 *
 * Runs against a **real in-process `ws` server** on 127.0.0.1 speaking the real
 * MessagePack framing — only Fish's synthesis is simulated. Handshake headers,
 * frame encode/decode, protocol ordering, audio reassembly, error surfacing and
 * the stall timeout all execute for real over a real socket.
 */

const codec: MsgpackCodec = { encode, decode };

describe('[mocked] Fish Audio WebSocket TTS — live protocol', () => {
  let wss: WebSocketServer;
  let url: string;

  const recorder = {
    headers: {} as Record<string, string | string[] | undefined>,
    events: [] as Array<Record<string, unknown>>,
    audioFrames: [] as number[][],
    finishReason: 'stop',
    finishMessage: undefined as string | undefined,
    stall: false,
  };

  beforeAll(async () => {
    wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
    wss.on('connection', (socket: WSSocket, req) => {
      recorder.headers = req.headers;
      socket.on('message', (raw: Buffer) => {
        const event = decode(new Uint8Array(raw)) as Record<string, unknown>;
        recorder.events.push(event);
        if (event.event !== 'stop') return;
        if (recorder.stall) return; // never answer — exercises the frame timeout
        for (const frame of recorder.audioFrames) {
          socket.send(encode({ event: 'audio', audio: Uint8Array.from(frame) }));
        }
        const finish: Record<string, unknown> = {
          event: 'finish',
          reason: recorder.finishReason,
        };
        if (recorder.finishMessage !== undefined) finish.message = recorder.finishMessage;
        socket.send(encode(finish));
      });
    });
    await new Promise<void>((resolve) => wss.on('listening', () => resolve()));
    url = `ws://127.0.0.1:${(wss.address() as AddressInfo).port}`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve) => wss.close(() => resolve()));
  });

  beforeEach(() => {
    recorder.headers = {};
    recorder.events = [];
    recorder.audioFrames = [
      [170, 187],
      [204, 221],
    ];
    recorder.finishReason = 'stop';
    recorder.finishMessage = undefined;
    recorder.stall = false;
  });

  const ws = (opts = {}): FishAudioWebSocketTTS =>
    new FishAudioWebSocketTTS('fish-test-key', { wsUrl: url, ...opts });

  async function collect(tts: FishAudioWebSocketTTS, text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of tts.synthesizeStream(text)) chunks.push(chunk);
    return Buffer.concat(chunks);
  }

  it('reassembles audio frames in order', async () => {
    expect(Array.from(await collect(ws(), 'ciao'))).toEqual([170, 187, 204, 221]);
  });

  it('sends start → text → flush → stop', async () => {
    await collect(ws(), 'ciao mondo');
    expect(recorder.events.map((e) => e.event)).toEqual(['start', 'text', 'flush', 'stop']);
    expect(recorder.events[1].text).toBe('ciao mondo');
  });

  it('carries the config on the start frame with an empty text', async () => {
    await collect(ws({ voice: 'ref-1', sampleRate: 8000 }), 'ciao');
    const request = recorder.events[0].request as Record<string, unknown>;
    expect(request).toMatchObject({
      text: '',
      format: 'pcm',
      sample_rate: 8000,
      reference_id: 'ref-1',
    });
  });

  it('sends Bearer auth and the model header on the handshake', async () => {
    await collect(ws({ model: 's1' }), 'ciao');
    expect(recorder.headers.authorization).toBe('Bearer fish-test-key');
    expect(recorder.headers.model).toBe('s1');
  });

  it('throws when the server finishes with reason=error', async () => {
    recorder.finishReason = 'error';
    recorder.finishMessage = 'voice model not found';
    await expect(collect(ws(), 'ciao')).rejects.toThrow(/voice model not found/);
  });

  it('throws instead of hanging when the server stalls', async () => {
    recorder.stall = true;
    await expect(collect(ws({ frameTimeoutMs: 200 }), 'ciao')).rejects.toThrow(/stalled/);
  });
});

describe('[unit] Fish Audio WebSocket TTS — model guard', () => {
  it('rejects s2.1-pro up front and points at the HTTP adapter', () => {
    expect(() => new FishAudioWebSocketTTS('k', { model: 's2.1-pro' })).toThrow(
      /FishAudioTTS/,
    );
    expect(() => new FishAudioWebSocketTTS('k', { model: 's2.1-pro' })).toThrow(
      /\/v1\/tts\/live/,
    );
  });

  it('rejects s2.1-pro-free too', () => {
    expect(() => new FishAudioWebSocketTTS('k', { model: 's2.1-pro-free' })).toThrow();
  });

  it.each(WS_SUPPORTED_MODELS)('accepts the socket-supported model %s', (model) => {
    expect(new FishAudioWebSocketTTS('k', { model }).model).toBe(model);
  });

  it('defaults to s2-pro, the fastest socket model', () => {
    expect(new FishAudioWebSocketTTS('k').model).toBe('s2-pro');
  });

  it('shares the HTTP adapter pricing key', () => {
    expect(FishAudioWebSocketTTS.providerKey).toBe('fish_audio');
  });

  it('falls back to FISH_AUDIO_API_KEY on the pipeline subclass', () => {
    const prev = process.env.FISH_AUDIO_API_KEY;
    process.env.FISH_AUDIO_API_KEY = 'env-key';
    try {
      expect(new FishAudioPipelineWSTTS().model).toBe('s2-pro');
    } finally {
      if (prev === undefined) delete process.env.FISH_AUDIO_API_KEY;
      else process.env.FISH_AUDIO_API_KEY = prev;
    }
  });
});

describe('[unit] Fish Audio WebSocket TTS — frame decoding', () => {
  it('skips log and unknown frames without ending the stream', () => {
    for (const frame of [
      Buffer.from(encode({ event: 'log', message: 'warming up' })),
      Buffer.from(encode({ event: 'some-future-event' })),
      Buffer.from(encode([1, 2, 3])),
    ]) {
      expect(decodeServerFrame(frame, codec)).toEqual({ audio: null, done: false });
    }
  });

  it('ignores text frames rather than crashing the stream', () => {
    expect(decodeServerFrame('unexpected text', codec)).toEqual({
      audio: null,
      done: false,
    });
  });

  it('throws on an undecodable frame', () => {
    // 0x81 announces a 1-pair map and then ends — a truncated frame.
    expect(() => decodeServerFrame(Buffer.from([0x81]), codec)).toThrow(/undecodable/);
  });

  it('rejects an oversized audio frame', () => {
    const huge = Buffer.from(
      encode({ event: 'audio', audio: new Uint8Array(1024 * 1024 + 1) }),
    );
    expect(() => decodeServerFrame(huge, codec)).toThrow(/sanity limit/);
  });

  it('skips an audio frame that carries no bytes', () => {
    const frame = Buffer.from(encode({ event: 'audio', audio: null }));
    expect(decodeServerFrame(frame, codec)).toEqual({ audio: null, done: false });
  });

  it('reports finish as terminal', () => {
    const frame = Buffer.from(encode({ event: 'finish', reason: 'stop' }));
    expect(decodeServerFrame(frame, codec)).toEqual({ audio: null, done: true });
  });
});

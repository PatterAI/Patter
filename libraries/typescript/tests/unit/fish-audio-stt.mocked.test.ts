import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import {
  DEFAULT_BUFFER_SIZE,
  FishAudioSTT,
  type Transcript,
} from '../../src/providers/fish-audio-stt';
import { STT as FishAudioPipelineSTT } from '../../src/stt/fish-audio';

/**
 * [mocked] Fish Audio batch STT (ASR).
 *
 * Runs against a **real in-process HTTP server** on 127.0.0.1 that receives the
 * real multipart body the adapter uploads — only Fish's transcription is
 * simulated. Buffering, WAV framing, multipart field names, tail padding,
 * transcript mapping and the error path all execute for real over a real socket.
 */

/** One second of 16 kHz PCM16 = 32000 bytes. */
const BYTES_PER_SECOND = 16000 * 2;

interface Upload {
  authorization: string | undefined;
  raw: Buffer;
}

/** Locate the embedded WAV and parse the fields we care about. */
function parseWav(raw: Buffer): {
  channels: number;
  sampleRate: number;
  bitsPerSample: number;
  dataBytes: number;
} {
  const start = raw.indexOf(Buffer.from('RIFF'));
  if (start < 0) throw new Error('no RIFF header in the uploaded body');
  return {
    channels: raw.readUInt16LE(start + 22),
    sampleRate: raw.readUInt32LE(start + 24),
    bitsPerSample: raw.readUInt16LE(start + 34),
    dataBytes: raw.readUInt32LE(start + 40),
  };
}

function fieldValue(raw: Buffer, name: string): string | null {
  const body = raw.toString('latin1');
  const marker = `name="${name}"`;
  const at = body.indexOf(marker);
  if (at < 0) return null;
  const valueStart = body.indexOf('\r\n\r\n', at);
  if (valueStart < 0) return null;
  const valueEnd = body.indexOf('\r\n', valueStart + 4);
  return body.slice(valueStart + 4, valueEnd < 0 ? undefined : valueEnd);
}

describe('[mocked] Fish Audio STT — upload shape + buffering', () => {
  let server: http.Server;
  let base: string;

  const recorder = {
    uploads: [] as Upload[],
    status: 200,
    payload: {} as Record<string, unknown>,
    errorBody: '',
  };

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      const body: Buffer[] = [];
      req.on('data', (c: Buffer) => body.push(c));
      req.on('end', () => {
        recorder.uploads.push({
          authorization: req.headers.authorization,
          raw: Buffer.concat(body),
        });
        if (recorder.status !== 200) {
          res.writeHead(recorder.status);
          res.end(recorder.errorBody);
          return;
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(recorder.payload));
      });
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/asr`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  beforeEach(() => {
    recorder.uploads = [];
    recorder.status = 200;
    recorder.errorBody = '';
    recorder.payload = { text: 'ciao mondo', duration: 2.0 };
  });

  const stt = (language?: string, opts = {}): FishAudioSTT =>
    new FishAudioSTT('fish-test-key', language, { baseUrl: base, ...opts });

  async function run(
    adapter: FishAudioSTT,
    audio: Buffer,
  ): Promise<Transcript[]> {
    const received: Transcript[] = [];
    adapter.onTranscript((t) => received.push(t));
    await adapter.connect();
    adapter.sendAudio(audio);
    await adapter.close();
    return received;
  }

  it('uploads a full window and emits a final transcript', async () => {
    const got = await run(stt('en'), Buffer.alloc(DEFAULT_BUFFER_SIZE));
    expect(got.map((t) => t.text)).toEqual(['ciao mondo']);
    expect(got[0].isFinal).toBe(true);
    expect(recorder.uploads).toHaveLength(1);
  });

  it('uploads valid 16 kHz mono PCM16 WAV', async () => {
    await run(stt('en'), Buffer.alloc(DEFAULT_BUFFER_SIZE));
    expect(parseWav(recorder.uploads[0].raw)).toEqual({
      channels: 1,
      sampleRate: 16000,
      bitsPerSample: 16,
      dataBytes: DEFAULT_BUFFER_SIZE,
    });
  });

  it("uses Fish's documented multipart field names", async () => {
    await run(stt('it'), Buffer.alloc(DEFAULT_BUFFER_SIZE));
    const { raw, authorization } = recorder.uploads[0];
    expect(authorization).toBe('Bearer fish-test-key');
    expect(raw.toString('latin1')).toContain('name="audio"');
    expect(raw.toString('latin1')).toContain('filename="audio.wav"');
    expect(fieldValue(raw, 'language')).toBe('it');
    expect(fieldValue(raw, 'ignore_timestamps')).toBe('true');
  });

  it('forwards ignoreTimestamps: false', async () => {
    await run(stt('en', { ignoreTimestamps: false }), Buffer.alloc(DEFAULT_BUFFER_SIZE));
    expect(fieldValue(recorder.uploads[0].raw, 'ignore_timestamps')).toBe('false');
  });

  it('omits language entirely when unset so Fish auto-detects', async () => {
    await run(stt(undefined), Buffer.alloc(DEFAULT_BUFFER_SIZE));
    expect(fieldValue(recorder.uploads[0].raw, 'language')).toBeNull();
  });

  it('does not upload a partial window before the threshold', async () => {
    const adapter = stt('en');
    await adapter.connect();
    adapter.sendAudio(Buffer.alloc(DEFAULT_BUFFER_SIZE / 4));
    expect(recorder.uploads).toHaveLength(0);
    await adapter.close();
  });

  it("pads a short tail with silence up to Fish's 1 s minimum", async () => {
    // 50 ms of audio — far below the documented floor. Dropping it would lose
    // the last words of an utterance, so it is padded instead.
    await run(stt('en'), Buffer.alloc(1600));
    expect(recorder.uploads).toHaveLength(1);
    expect(parseWav(recorder.uploads[0].raw).dataBytes).toBe(BYTES_PER_SECOND);
  });

  it('does not pad a tail already over the minimum', async () => {
    const tail = BYTES_PER_SECOND + 4000;
    await run(stt('en'), Buffer.alloc(tail));
    expect(parseWav(recorder.uploads[0].raw).dataBytes).toBe(tail);
  });

  it('emits one transcript per window', async () => {
    const adapter = stt('en');
    const received: Transcript[] = [];
    adapter.onTranscript((t) => received.push(t));
    await adapter.connect();
    adapter.sendAudio(Buffer.alloc(DEFAULT_BUFFER_SIZE));
    adapter.sendAudio(Buffer.alloc(DEFAULT_BUFFER_SIZE));
    await adapter.close();
    expect(received).toHaveLength(2);
    expect(recorder.uploads).toHaveLength(2);
  });

  it('honours a custom buffer size', async () => {
    await run(
      stt('en', { bufferSize: BYTES_PER_SECOND }),
      Buffer.alloc(BYTES_PER_SECOND * 2),
    );
    expect(recorder.uploads).toHaveLength(1);
  });

  it('surfaces segments when the caller asked for timestamps', async () => {
    recorder.payload = {
      text: 'ciao mondo',
      duration: 2.0,
      segments: [
        { text: 'ciao', start: 0, end: 0.4 },
        { text: 'mondo', start: 0.4, end: 1.1 },
      ],
    };
    const got = await run(
      stt('it', { ignoreTimestamps: false }),
      Buffer.alloc(DEFAULT_BUFFER_SIZE),
    );
    expect(got[0].segments).toHaveLength(2);
    expect(got[0].segments?.[0].text).toBe('ciao');
  });

  it('omits segments when Fish returns none', async () => {
    const got = await run(stt('en'), Buffer.alloc(DEFAULT_BUFFER_SIZE));
    expect(got[0].segments).toBeUndefined();
  });

  it('logs a server error and emits nothing — the call must survive', async () => {
    recorder.status = 402;
    recorder.errorBody = 'insufficient credit';
    expect(await run(stt('en'), Buffer.alloc(DEFAULT_BUFFER_SIZE))).toEqual([]);
  });

  it('drops an empty transcript', async () => {
    recorder.payload = { text: '   ', duration: 2.0 };
    expect(await run(stt('en'), Buffer.alloc(DEFAULT_BUFFER_SIZE))).toEqual([]);
  });

  it('does not throw when the host is unreachable', async () => {
    const adapter = new FishAudioSTT('k', 'en', { baseUrl: 'http://127.0.0.1:1/v1/asr' });
    expect(await run(adapter, Buffer.alloc(DEFAULT_BUFFER_SIZE))).toEqual([]);
  });
});

describe('[unit] Fish Audio STT — construction', () => {
  it('requires an apiKey', () => {
    expect(() => new FishAudioSTT('')).toThrow(/apiKey is required/);
  });

  it('exposes the stable pricing key', () => {
    expect(FishAudioSTT.providerKey).toBe('fish_audio_stt');
  });

  it('clones without sharing buffer state', () => {
    const original = new FishAudioSTT('k', 'en');
    const copy = original.clone();
    expect(copy).not.toBe(original);
    expect(copy).toBeInstanceOf(FishAudioSTT);
  });

  it('forTwilio matches the default construction', () => {
    expect(FishAudioSTT.forTwilio('k', 'fr')).toBeInstanceOf(FishAudioSTT);
  });

  it('defaults the window to two seconds, clear of the 1 s floor', () => {
    expect(DEFAULT_BUFFER_SIZE).toBe(BYTES_PER_SECOND * 2);
  });

  it('falls back to FISH_AUDIO_API_KEY on the pipeline subclass', () => {
    const prev = process.env.FISH_AUDIO_API_KEY;
    process.env.FISH_AUDIO_API_KEY = 'env-key';
    try {
      expect(() => new FishAudioPipelineSTT()).not.toThrow();
    } finally {
      if (prev === undefined) delete process.env.FISH_AUDIO_API_KEY;
      else process.env.FISH_AUDIO_API_KEY = prev;
    }
  });

  it('raises a pointed error when no key is available', () => {
    const prev = process.env.FISH_AUDIO_API_KEY;
    delete process.env.FISH_AUDIO_API_KEY;
    try {
      expect(() => new FishAudioPipelineSTT()).toThrow(/FISH_AUDIO_API_KEY/);
    } finally {
      if (prev !== undefined) process.env.FISH_AUDIO_API_KEY = prev;
    }
  });
});

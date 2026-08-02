import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import {
  FishAudioFormat,
  FishAudioLatency,
  FishAudioModel,
  FishAudioTTS,
} from '../../src/providers/fish-audio-tts';
import { TTS as FishAudioPipelineTTS } from '../../src/tts/fish-audio';

/**
 * [mocked] Fish Audio HTTP TTS.
 *
 * Runs against a **real in-process HTTP server** on 127.0.0.1 — only Fish's
 * synthesis is simulated. Header assembly, JSON payload construction, prosody
 * nesting, chunked response streaming, error surfacing, the warmup request and
 * the env-var fallback all execute for real over a real socket.
 */
describe('[mocked] Fish Audio TTS — request + streaming', () => {
  let server: http.Server;
  let base: string;

  const recorder = {
    ttsHeaders: {} as http.IncomingHttpHeaders,
    ttsBody: {} as Record<string, unknown>,
    modelHits: 0,
    modelUrl: '',
    status: 200,
    errorBody: '',
    chunks: [] as number[][],
  };

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      if (req.method === 'GET' && req.url?.startsWith('/model')) {
        recorder.modelHits += 1;
        recorder.modelUrl = req.url;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ items: [], total: 0 }));
        return;
      }
      const body: Buffer[] = [];
      req.on('data', (c: Buffer) => body.push(c));
      req.on('end', () => {
        recorder.ttsHeaders = req.headers;
        recorder.ttsBody = JSON.parse(Buffer.concat(body).toString('utf8'));
        if (recorder.status !== 200) {
          res.writeHead(recorder.status);
          res.end(recorder.errorBody);
          return;
        }
        res.writeHead(200, {
          'Content-Type': 'application/octet-stream',
          'Transfer-Encoding': 'chunked',
        });
        for (const chunk of recorder.chunks) res.write(Buffer.from(chunk));
        res.end();
      });
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  beforeEach(() => {
    recorder.ttsHeaders = {};
    recorder.ttsBody = {};
    recorder.modelHits = 0;
    recorder.modelUrl = '';
    recorder.status = 200;
    recorder.errorBody = '';
    recorder.chunks = [
      [1, 2],
      [3, 4],
    ];
  });

  const tts = (opts = {}): FishAudioTTS =>
    new FishAudioTTS('fish-test-key', { baseUrl: `${base}/v1/tts`, ...opts });

  it('streams every chunk the server emits', async () => {
    const audio = await tts().synthesize('ciao');
    expect(Array.from(audio)).toEqual([1, 2, 3, 4]);
  });

  it('sends the model in a header, never in the body', async () => {
    await tts({ model: FishAudioModel.S2_PRO }).synthesize('ciao');
    expect(recorder.ttsHeaders.model).toBe('s2-pro');
    expect(recorder.ttsHeaders.authorization).toBe('Bearer fish-test-key');
    expect(recorder.ttsHeaders['content-type']).toBe('application/json');
    expect(recorder.ttsBody).not.toHaveProperty('model');
  });

  it('defaults to s2.1-pro / pcm @ 16 kHz / balanced latency', async () => {
    await tts().synthesize('hello');
    expect(recorder.ttsHeaders.model).toBe('s2.1-pro');
    expect(recorder.ttsBody).toEqual({
      text: 'hello',
      format: 'pcm',
      latency: 'balanced',
      sample_rate: 16000,
    });
  });

  it('omits unset knobs so Fish applies its own documented defaults', async () => {
    await tts().synthesize('hello');
    for (const absent of [
      'temperature',
      'top_p',
      'chunk_length',
      'normalize',
      'prosody',
      'reference_id',
      'max_new_tokens',
    ]) {
      expect(recorder.ttsBody).not.toHaveProperty(absent);
    }
  });

  it('maps a string voice to reference_id', async () => {
    await tts({ voice: 'ref-abc123' }).synthesize('hello');
    expect(recorder.ttsBody.reference_id).toBe('ref-abc123');
  });

  it('maps an array voice to multi-speaker reference_ids', async () => {
    await tts({ voice: ['spk-a', 'spk-b'] }).synthesize('<|speaker:0|>Hi<|speaker:1|>Yo');
    expect(recorder.ttsBody.reference_id).toEqual(['spk-a', 'spk-b']);
  });

  it('collapses flat speed/volume/loudness into a nested prosody object', async () => {
    await tts({ speed: 1.2, volume: -3, normalizeLoudness: false }).synthesize('hi');
    expect(recorder.ttsBody.prosody).toEqual({
      speed: 1.2,
      volume: -3,
      normalize_loudness: false,
    });
  });

  it('sends sample_rate for pcm/wav but not for mp3/opus', async () => {
    for (const [format, expected] of [
      [FishAudioFormat.PCM, true],
      [FishAudioFormat.WAV, true],
      [FishAudioFormat.MP3, false],
      [FishAudioFormat.OPUS, false],
    ] as const) {
      await tts({ format, sampleRate: 24000 }).synthesize('hi');
      expect(Object.hasOwn(recorder.ttsBody, 'sample_rate'), `format=${format}`).toBe(
        expected,
      );
    }
  });

  it('forwards the latency mode', async () => {
    await tts({ latency: FishAudioLatency.LOW }).synthesize('hi');
    expect(recorder.ttsBody.latency).toBe('low');
  });

  it('forwards the advanced sampling knobs under their snake_case names', async () => {
    await tts({
      temperature: 0.4,
      topP: 0.9,
      chunkLength: 200,
      minChunkLength: 20,
      normalize: false,
      maxNewTokens: 512,
      repetitionPenalty: 1.1,
      conditionOnPreviousChunks: false,
      earlyStopThreshold: 0.5,
    }).synthesize('hi');
    expect(recorder.ttsBody).toMatchObject({
      temperature: 0.4,
      top_p: 0.9,
      chunk_length: 200,
      min_chunk_length: 20,
      normalize: false,
      max_new_tokens: 512,
      repetition_penalty: 1.1,
      condition_on_previous_chunks: false,
      early_stop_threshold: 0.5,
    });
  });

  it('throws with the status and body on a non-200', async () => {
    recorder.status = 402;
    recorder.errorBody = '{"detail":"insufficient credit"}';
    await expect(tts().synthesize('hi')).rejects.toThrow(/Fish Audio TTS error 402/);
  });

  it('warms up against the free model listing, never the billed endpoint', async () => {
    await tts().warmup();
    expect(recorder.modelHits).toBe(1);
    expect(recorder.modelUrl).toBe('/model?page_size=1');
    expect(recorder.ttsBody).toEqual({});
  });

  it('never throws from warmup when the host is unreachable', async () => {
    const unreachable = new FishAudioTTS('k', { baseUrl: 'http://127.0.0.1:1/v1/tts' });
    await expect(unreachable.warmup()).resolves.toBeUndefined();
  });
});

describe('[unit] Fish Audio TTS — construction + format declaration', () => {
  it('declares the configured sample rate as its source format', () => {
    expect(new FishAudioTTS('k').sourceAudioFormat()).toEqual({
      encoding: 'pcm_s16le',
      sampleRate: 16000,
    });
    expect(new FishAudioTTS('k', { sampleRate: 24000 }).sourceAudioFormat()).toEqual({
      encoding: 'pcm_s16le',
      sampleRate: 24000,
    });
  });

  it('forTwilio requests 8 kHz PCM so the pipeline skips the resample', () => {
    const t = FishAudioTTS.forTwilio('k');
    expect(t.format).toBe('pcm');
    expect(t.sourceAudioFormat()).toEqual({ encoding: 'pcm_s16le', sampleRate: 8000 });
  });

  it('forTelnyx keeps the 16 kHz pipeline rate', () => {
    expect(FishAudioTTS.forTelnyx('k').sourceAudioFormat()).toEqual({
      encoding: 'pcm_s16le',
      sampleRate: 16000,
    });
  });

  it('carrier factories preserve the other options', () => {
    const t = FishAudioTTS.forTwilio('k', { model: 's2-pro', voice: 'ref-1' });
    expect(t.model).toBe('s2-pro');
    expect(t.voice).toBe('ref-1');
  });

  it('requires an apiKey', () => {
    expect(() => new FishAudioTTS('')).toThrow(/apiKey is required/);
  });

  it('exposes the stable pricing key', () => {
    expect(FishAudioTTS.providerKey).toBe('fish_audio');
  });
});

describe('[unit] Fish Audio pipeline TTS — env fallback', () => {
  it('falls back to FISH_AUDIO_API_KEY', () => {
    const prev = process.env.FISH_AUDIO_API_KEY;
    process.env.FISH_AUDIO_API_KEY = 'env-key';
    try {
      expect(() => new FishAudioPipelineTTS()).not.toThrow();
    } finally {
      if (prev === undefined) delete process.env.FISH_AUDIO_API_KEY;
      else process.env.FISH_AUDIO_API_KEY = prev;
    }
  });

  it('raises a pointed error when no key is available', () => {
    const prev = process.env.FISH_AUDIO_API_KEY;
    delete process.env.FISH_AUDIO_API_KEY;
    try {
      expect(() => new FishAudioPipelineTTS()).toThrow(/FISH_AUDIO_API_KEY/);
    } finally {
      if (prev !== undefined) process.env.FISH_AUDIO_API_KEY = prev;
    }
  });

  it('keeps the carrier factories on the pipeline subclass', () => {
    expect(FishAudioPipelineTTS.forTwilio({ apiKey: 'k' }).sourceAudioFormat()).toEqual({
      encoding: 'pcm_s16le',
      sampleRate: 8000,
    });
    expect(FishAudioPipelineTTS.forTelnyx({ apiKey: 'k' }).sourceAudioFormat()).toEqual({
      encoding: 'pcm_s16le',
      sampleRate: 16000,
    });
  });
});

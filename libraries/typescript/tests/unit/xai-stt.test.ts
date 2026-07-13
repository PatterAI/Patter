import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

/**
 * xAI STT — streaming WebSocket adapter + batch REST helper.
 *
 * The `ws` module is mocked so `connect()` opens no real socket; the batch
 * helper's `fetch` boundary is stubbed. Everything inward (URL/query assembly,
 * transcript.created gating, is_final/speech_final mapping, finalize, multipart
 * ordering, response parsing) runs for real.
 */

vi.mock('ws', () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const EventEmitter = require('events');
  class MockWebSocket extends EventEmitter {
    static OPEN = 1;
    static CLOSED = 3;
    static instances: MockWebSocket[] = [];
    readyState = MockWebSocket.OPEN;
    sent: Array<string | Buffer> = [];
    url: string;
    options: { headers?: Record<string, string> };
    constructor(url: string, options?: { headers?: Record<string, string> }) {
      super();
      this.url = url;
      this.options = options ?? {};
      MockWebSocket.instances.push(this);
    }
    send(data: string | Buffer): void {
      this.sent.push(data);
    }
    close(): void {
      this.readyState = MockWebSocket.CLOSED;
    }
  }
  return { default: MockWebSocket };
});

import { XaiSTT, xaiTranscribe, XAI_STT_WS_URL } from '../../src/providers/xai-stt';

const CLOSED = 3;

type MockWS = {
  readyState: number;
  sent: Array<string | Buffer>;
  url: string;
  options: { headers?: Record<string, string> };
  emit: (event: string, ...args: unknown[]) => void;
};

async function instances(): Promise<MockWS[]> {
  const mod = (await import('ws')).default as unknown as { instances: MockWS[] };
  return mod.instances;
}

async function latest(): Promise<MockWS> {
  const all = await instances();
  return all[all.length - 1];
}

/** Connect an adapter and resolve once the ready signal is emitted. */
async function connect(stt: XaiSTT): Promise<MockWS> {
  const p = stt.connect();
  const ws = await latest();
  ws.emit('message', JSON.stringify({ type: 'transcript.created' }));
  await p;
  return ws;
}

describe('[unit] XaiSTT streaming', () => {
  beforeEach(async () => {
    (await instances()).length = 0;
  });

  it('builds the query-parameterised URL with a Bearer header and repeated keyterm', async () => {
    const stt = new XaiSTT('xai-test-key', {
      encoding: 'pcm',
      sampleRate: 16000,
      language: 'it',
      endpointingMs: 20,
      smartTurn: 0.5,
      smartTurnTimeoutMs: 2000,
      keyterms: ['Grok', 'xAI'],
      diarize: true,
      fillerWords: true,
    });
    const ws = await connect(stt);

    expect(ws.url.startsWith(`${XAI_STT_WS_URL}?`)).toBe(true);
    const url = new URL(ws.url);
    expect(url.searchParams.get('encoding')).toBe('pcm');
    expect(url.searchParams.get('sample_rate')).toBe('16000');
    expect(url.searchParams.get('interim_results')).toBe('true');
    expect(url.searchParams.get('language')).toBe('it');
    expect(url.searchParams.get('endpointing')).toBe('20');
    expect(url.searchParams.get('smart_turn')).toBe('0.5');
    expect(url.searchParams.get('smart_turn_timeout')).toBe('2000');
    expect(url.searchParams.get('diarize')).toBe('true');
    expect(url.searchParams.get('filler_words')).toBe('true');
    // `keyterm` is REPEATED, one param per term.
    expect(url.searchParams.getAll('keyterm')).toEqual(['Grok', 'xAI']);
    expect(ws.options.headers?.Authorization).toBe('Bearer xai-test-key');
    stt.close();
  });

  it('defaults to pcm @ 16000 with interim_results on (pipeline PCM16 feed)', async () => {
    const stt = new XaiSTT('xai-test-key');
    const ws = await connect(stt);
    const url = new URL(ws.url);
    expect(url.searchParams.get('encoding')).toBe('pcm');
    expect(url.searchParams.get('sample_rate')).toBe('16000');
    expect(url.searchParams.get('interim_results')).toBe('true');
    // No keyterms / diarize / language by default.
    expect(url.searchParams.getAll('keyterm')).toEqual([]);
    expect(url.searchParams.get('diarize')).toBeNull();
    expect(url.searchParams.get('language')).toBeNull();
    stt.close();
  });

  it('does not resolve connect() until transcript.created arrives', async () => {
    const stt = new XaiSTT('xai-test-key');
    let resolved = false;
    const p = stt.connect().then(() => {
      resolved = true;
    });
    const ws = await latest();
    // Flush microtasks — still pending because no transcript.created yet.
    await Promise.resolve();
    expect(resolved).toBe(false);
    ws.emit('message', JSON.stringify({ type: 'transcript.created' }));
    await p;
    expect(resolved).toBe(true);
    stt.close();
  });

  it('maps interim / chunk-final / utterance-final transcripts like Deepgram', async () => {
    const stt = new XaiSTT('xai-test-key');
    const ws = await connect(stt);
    const received: Array<Record<string, unknown>> = [];
    stt.onTranscript((t) => {
      received.push(t as unknown as Record<string, unknown>);
    });

    // Interim: is_final=false → not final.
    ws.emit('message', JSON.stringify({ type: 'transcript.partial', text: 'hel', is_final: false, speech_final: false }));
    // Chunk final: is_final=true, speech_final=false.
    ws.emit('message', JSON.stringify({ type: 'transcript.partial', text: 'hello there', is_final: true, speech_final: false }));
    // Utterance final: both true.
    ws.emit('message', JSON.stringify({ type: 'transcript.partial', text: 'hello there world', is_final: true, speech_final: true }));

    expect(received).toHaveLength(3);
    expect(received[0]).toMatchObject({ text: 'hel', isFinal: false, speechFinal: false, eventType: 'Results' });
    expect(received[1]).toMatchObject({ text: 'hello there', isFinal: true, speechFinal: false });
    expect(received[2]).toMatchObject({ text: 'hello there world', isFinal: true, speechFinal: true });
    stt.close();
  });

  it('maps xAI word entries to Patter word shape (text→word, speaker preserved)', async () => {
    const stt = new XaiSTT('xai-test-key', { diarize: true });
    const ws = await connect(stt);
    const received: Array<{ words?: ReadonlyArray<{ word?: string; speaker?: number }> }> = [];
    stt.onTranscript((t) => received.push(t as never));

    ws.emit('message', JSON.stringify({
      type: 'transcript.partial',
      text: 'hi bob',
      is_final: true,
      speech_final: true,
      words: [
        { text: 'hi', start: 0.1, end: 0.2, speaker: 0 },
        { text: 'bob', start: 0.3, end: 0.5, speaker: 1 },
      ],
    }));

    expect(received[0].words).toEqual([
      { word: 'hi', start: 0.1, end: 0.2, speaker: 0 },
      { word: 'bob', start: 0.3, end: 0.5, speaker: 1 },
    ]);
    stt.close();
  });

  it('finalize() sends {type:"finalize"} and stamps fromFinalize on the next final', async () => {
    const stt = new XaiSTT('xai-test-key');
    const ws = await connect(stt);
    const received: Array<{ fromFinalize?: boolean }> = [];
    stt.onTranscript((t) => received.push(t as never));

    stt.finalize();
    const finalizeFrame = ws.sent.find(
      (s) => typeof s === 'string' && s.includes('finalize'),
    ) as string;
    expect(JSON.parse(finalizeFrame)).toEqual({ type: 'finalize' });

    ws.emit('message', JSON.stringify({ type: 'transcript.partial', text: 'done', is_final: true, speech_final: true }));
    expect(received[0].fromFinalize).toBe(true);
    stt.close();
  });

  it('close() sends {type:"audio.done"} best-effort before closing', async () => {
    const stt = new XaiSTT('xai-test-key');
    const ws = await connect(stt);
    stt.close();
    const doneFrame = ws.sent.find(
      (s) => typeof s === 'string' && s.includes('audio.done'),
    ) as string;
    expect(JSON.parse(doneFrame)).toEqual({ type: 'audio.done' });
    expect(ws.readyState).toBe(CLOSED);
  });

  it('sends raw binary audio frames (no base64) and drops empty frames', async () => {
    const stt = new XaiSTT('xai-test-key');
    const ws = await connect(stt);
    ws.sent.length = 0;
    stt.sendAudio(Buffer.from([1, 2, 3, 4]));
    stt.sendAudio(Buffer.alloc(0)); // dropped
    expect(ws.sent).toHaveLength(1);
    expect(Buffer.isBuffer(ws.sent[0])).toBe(true);
    expect(ws.sent[0]).toEqual(Buffer.from([1, 2, 3, 4]));
    stt.close();
  });

  it('clone() produces a fresh adapter with the same construction args', () => {
    const stt = new XaiSTT('xai-test-key', { language: 'en' });
    const twin = stt.clone();
    expect(twin).not.toBe(stt);
    expect(twin).toBeInstanceOf(XaiSTT);
  });
});

describe('[mocked] xaiTranscribe batch REST helper', () => {
  const ORIGINAL_FETCH = global.fetch;
  let lastUrl: string | null = null;
  let lastHeaders: Record<string, string> | null = null;
  let lastBody: FormData | null = null;

  beforeEach(() => {
    lastUrl = null;
    lastHeaders = null;
    lastBody = null;
    global.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      lastUrl = typeof input === 'string' ? input : input.toString();
      lastHeaders = init?.headers as Record<string, string>;
      lastBody = init?.body as FormData;
      return new Response(
        JSON.stringify({
          text: 'The balance is $167.',
          language: 'English',
          duration: 3.45,
          words: [{ text: 'The', start: 0.24, end: 0.48 }],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    }) as typeof global.fetch;
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
  });

  it('posts multipart with scalar fields first and the file LAST', async () => {
    await xaiTranscribe({
      audio: Buffer.from([0, 1, 2, 3]),
      apiKey: 'xai-test-key',
      language: 'en',
      format: true,
      diarize: true,
      keyterms: ['Grok'],
      filename: 'clip.wav',
    });

    expect(lastUrl).toBe('https://api.x.ai/v1/stt');
    expect(lastHeaders?.Authorization).toBe('Bearer xai-test-key');

    const keys = [...(lastBody as FormData).keys()];
    // Scalars appear before the file; the file is the final field.
    expect(keys[keys.length - 1]).toBe('file');
    expect(keys.slice(0, -1)).toContain('language');
    expect(keys.slice(0, -1)).toContain('format');
    expect(keys.slice(0, -1)).toContain('keyterm');
    const file = (lastBody as FormData).get('file');
    expect(file).toBeInstanceOf(Blob);
  });

  it('parses the response into text/language/duration/words', async () => {
    const result = await xaiTranscribe({ audio: Buffer.from([0]), apiKey: 'xai-test-key' });
    expect(result.text).toBe('The balance is $167.');
    expect(result.language).toBe('English');
    expect(result.duration).toBe(3.45);
    expect(result.words).toEqual([{ text: 'The', start: 0.24, end: 0.48 }]);
  });

  it('sends `url` instead of a file when audio bytes are absent', async () => {
    await xaiTranscribe({ url: 'https://example.com/a.wav', apiKey: 'xai-test-key' });
    const keys = [...(lastBody as FormData).keys()];
    expect(keys).toContain('url');
    expect(keys).not.toContain('file');
  });

  it('throws when neither audio nor url is provided', async () => {
    await expect(xaiTranscribe({ apiKey: 'xai-test-key' })).rejects.toThrow(/audio.*or a `url`/);
  });
});

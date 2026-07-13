/**
 * Tests for the xAI (Grok) voice providers: STT, TTS, and Realtime.
 *
 * Protocol-level unit tests — URL/query construction, session config wire
 * shape, option validation, and pricing entries. No network access.
 */
import { describe, it, expect } from 'vitest';

import {
  DEFAULT_PRICING,
  mergePricing,
  calculateSttCost,
  calculateTtsCost,
  calculateRealtimeMinuteCost,
} from '../src/pricing';
import { XAISTT, XAISTTEncoding, XAISTTSampleRate } from '../src/providers/xai-stt';
import { XAITTS, XAIVoice, XAITTSCodec } from '../src/providers/xai-tts';
import {
  XAIRealtimeAdapter,
  XAIRealtimeAudioFormat,
  XAIRealtimeModel,
} from '../src/providers/xai-realtime';
import { STT as XAISTTShim } from '../src/stt/xai';
import { TTS as XAITTSShim } from '../src/tts/xai';

// Access private members for wire-shape assertions without widening the
// public API surface.
type AnyRecord = Record<string, unknown>;
const priv = (obj: unknown): AnyRecord => obj as unknown as AnyRecord;

describe('XAISTT', () => {
  it('builds the default WS URL with query params', () => {
    const stt = new XAISTT('xai-test');
    const url = (priv(stt).buildWsUrl as () => string).call(stt);
    expect(url.startsWith('wss://api.x.ai/v1/stt?')).toBe(true);
    expect(url).toContain('sample_rate=16000');
    expect(url).toContain('encoding=pcm');
    expect(url).toContain('interim_results=true');
    expect(url).toContain('language=en');
  });

  it('repeats keyterm for each term and supports smart turn', () => {
    const stt = new XAISTT('xai-test', {
      keyterms: ['Patter', 'Grok'],
      smartTurn: 0.7,
      smartTurnTimeoutMs: 3000,
      diarize: true,
      fillerWords: true,
    });
    const url = (priv(stt).buildWsUrl as () => string).call(stt);
    expect(url.match(/keyterm=/g)).toHaveLength(2);
    expect(url).toContain('smart_turn=0.7');
    expect(url).toContain('smart_turn_timeout=3000');
    expect(url).toContain('diarize=true');
    expect(url).toContain('filler_words=true');
  });

  it('omits language when explicitly null', () => {
    const stt = new XAISTT('xai-test', { language: null });
    const url = (priv(stt).buildWsUrl as () => string).call(stt);
    expect(url).not.toContain('language=');
  });

  it('enforces keyterm limits', () => {
    expect(() => new XAISTT('xai-test', { keyterms: ['x'.repeat(51)] })).toThrow();
    expect(
      () => new XAISTT('xai-test', { keyterms: Array.from({ length: 101 }, () => 'k') }),
    ).toThrow();
  });

  it('enforces smartTurn range and non-empty api key', () => {
    expect(() => new XAISTT('xai-test', { smartTurn: 1.5 })).toThrow();
    expect(() => new XAISTT('')).toThrow();
  });

  it('maps transcript.partial events onto the Transcript shape', () => {
    const stt = new XAISTT('xai-test');
    const received: Array<{ text: string; isFinal: boolean; speechFinal?: boolean; confidence: number }> = [];
    stt.onTranscript((t) => {
      received.push(t);
    });
    const handle = priv(stt).handleEvent as (e: AnyRecord) => void;
    handle.call(stt, {
      type: 'transcript.partial',
      text: 'hello wor',
      is_final: false,
      speech_final: false,
    });
    handle.call(stt, {
      type: 'transcript.partial',
      text: 'hello world',
      is_final: true,
      speech_final: true,
      end_of_turn_confidence: 0.983,
    });
    // Empty text and error events are skipped.
    handle.call(stt, { type: 'transcript.partial', text: '  ' });
    handle.call(stt, { type: 'error', message: 'boom' });

    expect(received).toHaveLength(2);
    expect(received[0]).toMatchObject({ text: 'hello wor', isFinal: false, speechFinal: false });
    expect(received[1]).toMatchObject({ text: 'hello world', isFinal: true, speechFinal: true });
    expect(received[1].confidence).toBeCloseTo(0.983);
  });

  it('treats speech_final without is_final as final (defensive)', () => {
    const stt = new XAISTT('xai-test');
    const received: Array<{ isFinal: boolean }> = [];
    stt.onTranscript((t) => {
      received.push(t);
    });
    (priv(stt).handleEvent as (e: AnyRecord) => void).call(stt, {
      type: 'transcript.partial',
      text: 'done',
      is_final: false,
      speech_final: true,
    });
    expect(received[0].isFinal).toBe(true);
  });

  it('shim falls back to XAI_API_KEY and clones with ctor args', () => {
    const prev = process.env.XAI_API_KEY;
    process.env.XAI_API_KEY = 'xai-env';
    try {
      const stt = new XAISTTShim();
      expect(stt).toBeInstanceOf(XAISTT);
      const clone = stt.clone();
      expect(clone).toBeInstanceOf(XAISTTShim);
      delete process.env.XAI_API_KEY;
      expect(() => new XAISTTShim()).toThrow(/XAI_API_KEY/);
    } finally {
      if (prev === undefined) delete process.env.XAI_API_KEY;
      else process.env.XAI_API_KEY = prev;
    }
  });

  it('exposes encoding/sample-rate enums with xAI values', () => {
    expect(XAISTTEncoding.MULAW).toBe('mulaw');
    expect(XAISTTSampleRate.HZ_8000).toBe(8000);
  });
});

describe('XAITTS', () => {
  it('builds the default WS URL with query params', () => {
    const tts = new XAITTS('xai-test');
    const url = (priv(tts).buildWsUrl as () => string).call(tts);
    expect(url.startsWith('wss://api.x.ai/v1/tts?')).toBe(true);
    expect(url).toContain('voice=eve');
    expect(url).toContain('language=en');
    expect(url).toContain('codec=pcm');
    expect(url).toContain('sample_rate=16000');
  });

  it('carries optional params on the URL', () => {
    const tts = new XAITTS('xai-test', {
      voice: XAIVoice.ARA,
      codec: XAITTSCodec.MP3,
      sampleRate: 24000,
      bitRate: 192000,
      speed: 1.2,
      textNormalization: true,
    });
    const url = (priv(tts).buildWsUrl as () => string).call(tts);
    expect(url).toContain('voice=ara');
    expect(url).toContain('codec=mp3');
    expect(url).toContain('bit_rate=192000');
    expect(url).toContain('speed=1.2');
    expect(url).toContain('text_normalization=true');
  });

  it('builds the unary HTTP payload with nested output_format', () => {
    const tts = new XAITTS('xai-test', {
      voice: 'ara',
      codec: 'mp3',
      sampleRate: 44100,
      bitRate: 192000,
    });
    const payload = (priv(tts).buildHttpPayload as (t: string) => AnyRecord).call(tts, 'Ciao!');
    expect(payload.text).toBe('Ciao!');
    expect(payload.voice_id).toBe('ara');
    expect(payload.output_format).toEqual({ codec: 'mp3', sample_rate: 44100, bit_rate: 192000 });
  });

  it('forTwilio pins carrier-native mulaw @ 8 kHz', () => {
    const tts = XAITTS.forTwilio('xai-test');
    expect(tts.sourceAudioFormat()).toEqual({ encoding: 'mulaw', sampleRate: 8000 });
  });

  it('setTelephonyCarrier flips only unpinned formats', () => {
    const unpinned = new XAITTS('xai-test');
    unpinned.setTelephonyCarrier('telnyx');
    expect(unpinned.sourceAudioFormat()).toEqual({ encoding: 'mulaw', sampleRate: 8000 });

    const pinned = new XAITTS('xai-test', { codec: XAITTSCodec.PCM, sampleRate: 24000 });
    pinned.setTelephonyCarrier('twilio');
    expect(pinned.sourceAudioFormat()).toEqual({ encoding: 'pcm_s16le', sampleRate: 24000 });
  });

  it('enforces speed range and text length cap', async () => {
    expect(() => new XAITTS('xai-test', { speed: 2.0 })).toThrow();
    const tts = new XAITTS('xai-test');
    await expect(async () => {
      for await (const _ of tts.synthesizeStream('x'.repeat(15_001))) {
        void _;
      }
    }).rejects.toThrow(/15000/);
  });

  it('has the 26-voice roster with eve as default', () => {
    expect(Object.keys(XAIVoice)).toHaveLength(26);
    expect(XAIVoice.EVE).toBe('eve');
  });

  it('shim factories read XAI_API_KEY from the environment', () => {
    const prev = process.env.XAI_API_KEY;
    process.env.XAI_API_KEY = 'xai-env';
    try {
      const tts = XAITTSShim.forTwilio();
      expect(tts.sourceAudioFormat()).toEqual({ encoding: 'mulaw', sampleRate: 8000 });
      delete process.env.XAI_API_KEY;
      expect(() => new XAITTSShim()).toThrow(/XAI_API_KEY/);
    } finally {
      if (prev === undefined) delete process.env.XAI_API_KEY;
      else process.env.XAI_API_KEY = prev;
    }
  });
});

describe('XAIRealtimeAdapter', () => {
  it('builds the default session config (pcmu, server VAD, eve)', () => {
    const adapter = new XAIRealtimeAdapter('xai-test');
    const config = (priv(adapter).buildSessionConfig as () => AnyRecord).call(adapter);
    expect(config.voice).toBe('eve');
    expect(config.turn_detection).toEqual({ type: 'server_vad' });
    const audio = config.audio as { input: AnyRecord; output: AnyRecord };
    expect(audio.input.format).toEqual({ type: 'audio/pcmu' });
    expect(audio.output.format).toEqual({ type: 'audio/pcmu' });
    expect(config.reasoning).toBeUndefined();
  });

  it('includes the PCM rate only for audio/pcm', () => {
    const adapter = new XAIRealtimeAdapter('xai-test', {
      audioFormat: XAIRealtimeAudioFormat.AUDIO_PCM,
      sampleRate: 16000,
    });
    const config = (priv(adapter).buildSessionConfig as () => AnyRecord).call(adapter);
    const audio = config.audio as { input: { format: AnyRecord } };
    expect(audio.input.format).toEqual({ type: 'audio/pcm', rate: 16000 });
  });

  it('carries transcription biasing, replace map, reasoning and speed', () => {
    const adapter = new XAIRealtimeAdapter('xai-test', {
      languageHint: 'it',
      keyterms: ['Patter'],
      replace: { 'Acme Mobile': 'Acme Mobull' },
      reasoningEffort: 'none',
      outputSpeed: 1.1,
    });
    const config = (priv(adapter).buildSessionConfig as () => AnyRecord).call(adapter);
    const audio = config.audio as {
      input: { transcription: AnyRecord };
      output: AnyRecord;
    };
    expect(audio.input.transcription).toEqual({ language_hint: 'it', keyterms: ['Patter'] });
    expect(audio.output.speed).toBe(1.1);
    expect(config.replace).toEqual({ 'Acme Mobile': 'Acme Mobull' });
    expect(config.reasoning).toEqual({ effort: 'none' });
  });

  it('wraps function tools and passes server-side tools through', () => {
    const adapter = new XAIRealtimeAdapter('xai-test', {
      tools: [
        { name: 'lookup', description: 'd', parameters: { type: 'object' } },
        { type: 'web_search' },
        { type: 'file_search', vector_store_ids: ['c1'] },
      ],
    });
    const config = (priv(adapter).buildSessionConfig as () => AnyRecord).call(adapter);
    const tools = config.tools as AnyRecord[];
    expect(tools[0]).toMatchObject({ type: 'function', name: 'lookup' });
    expect(tools[1]).toEqual({ type: 'web_search' });
    expect(tools[2]).toMatchObject({ type: 'file_search', vector_store_ids: ['c1'] });
  });

  it('builds the model-selecting URL', () => {
    const adapter = new XAIRealtimeAdapter('xai-test', {
      model: XAIRealtimeModel.GROK_VOICE_THINK_FAST_1_0,
    });
    const url = (priv(adapter).buildUrl as () => string).call(adapter);
    expect(url).toBe('wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-1.0');
  });

  it('validates constructor inputs', () => {
    expect(() => new XAIRealtimeAdapter('')).toThrow();
    expect(() => new XAIRealtimeAdapter('xai-test', { outputSpeed: 0.5 })).toThrow();
  });

  it('counts billable client text messages', async () => {
    const adapter = new XAIRealtimeAdapter('xai-test');
    const sent: AnyRecord[] = [];
    priv(adapter).ws = {
      readyState: 1,
      send: (data: string) => sent.push(JSON.parse(data) as AnyRecord),
    };
    await adapter.sendText('ciao');
    expect(adapter.getTextInputMessages()).toBe(1);
    expect(sent[0].type).toBe('conversation.item.create');
    expect(sent[1].type).toBe('response.create');
  });

  it('sends function results in the function_call_output envelope', async () => {
    const adapter = new XAIRealtimeAdapter('xai-test');
    const sent: AnyRecord[] = [];
    priv(adapter).ws = {
      readyState: 1,
      send: (data: string) => sent.push(JSON.parse(data) as AnyRecord),
    };
    await adapter.sendFunctionResult('c1', '{"ok":true}');
    const item = sent[0].item as AnyRecord;
    expect(item.type).toBe('function_call_output');
    expect(item.call_id).toBe('c1');
  });

  it('cancelResponse no-ops when no response is in flight', () => {
    const adapter = new XAIRealtimeAdapter('xai-test');
    const sent: AnyRecord[] = [];
    priv(adapter).ws = {
      readyState: 1,
      send: (data: string) => sent.push(JSON.parse(data) as AnyRecord),
    };
    adapter.cancelResponse();
    expect(sent).toHaveLength(0);
  });
});

describe('xAI pricing', () => {
  it('matches the published xAI rates', () => {
    // Streaming STT $0.20/hr; TTS $15/1M chars; realtime $0.05/min +
    // $0.004 per client text message.
    expect(DEFAULT_PRICING.xai_stt.unit).toBe('minute');
    expect(DEFAULT_PRICING.xai_stt.price).toBeCloseTo(0.2 / 60, 6);
    expect(DEFAULT_PRICING.xai_tts.price).toBeCloseTo(0.015, 6);
    expect(DEFAULT_PRICING.xai_realtime.price).toBeCloseTo(0.05, 6);
    expect(DEFAULT_PRICING.xai_realtime.text_input_per_message).toBeCloseTo(0.004, 6);
  });

  it('calculates costs for the three modes', () => {
    const pricing = mergePricing();
    expect(calculateSttCost('xai_stt', 3600, pricing)).toBeCloseTo(0.2, 6);
    expect(calculateTtsCost('xai_tts', 1_000_000, pricing)).toBeCloseTo(15.0, 6);
    // 10 minutes + 3 text messages: 10*0.05 + 3*0.004 = 0.512
    expect(calculateRealtimeMinuteCost('xai_realtime', 600, pricing, 3)).toBeCloseTo(0.512, 6);
    expect(calculateRealtimeMinuteCost('unknown', 600, pricing)).toBe(0);
  });

  it('provider keys match the pricing entries', () => {
    expect(DEFAULT_PRICING[XAISTT.providerKey]).toBeDefined();
    expect(DEFAULT_PRICING[XAITTS.providerKey]).toBeDefined();
    expect(DEFAULT_PRICING[XAIRealtimeAdapter.providerKey]).toBeDefined();
  });
});

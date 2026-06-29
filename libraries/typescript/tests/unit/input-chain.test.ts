/**
 * Unit tests for the ``InputProcessingChain`` (pipeline-stages slice 1) and
 * the StreamHandler wiring that finally makes ``agent.audioFilter`` a live
 * parameter (parity with Python ``test_input_chain.py``).
 *
 * Covers:
 *   1. Stage ORDER — the filter runs AFTER AEC and BEFORE VAD (recording fakes).
 *   2. Fail-open filter wrapper — a rejecting filter degrades to passthrough
 *      with exactly ONE warning and frames keep flowing.
 *   3. Decode parity — mulaw decode + stateful resample matches a manual
 *      reference across chunk boundaries.
 *   4. Handler regression — ``handleAudio`` now feeds the FILTERED bytes to
 *      STT; previously ``audioFilter`` was accepted, documented, and never
 *      invoked.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { InputProcessingChain } from '../../src/services/input-chain';
import { mulawToPcm16, createResampler8kTo16k } from '../../src/audio/transcoding';
import { StreamHandler } from '../../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { getLogger, setLogger, type Logger } from '../../src/logger';
import type { AudioFilter, VADEvent, VADProvider, AgentOptions } from '../../src/types';
import { fakeMulawBuffer } from '../setup';
import type { WebSocket as WSWebSocket } from 'ws';

// Capture the process-wide default logger once so afterEach can restore it.
const originalLogger = getLogger();

function silentLogger(): Logger {
  return { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} };
}

// ---------------------------------------------------------------------------
// Recording fakes
// ---------------------------------------------------------------------------

class RecordingAec {
  readonly seen: Buffer[] = [];
  constructor(private readonly order: string[]) {}
  processNearEnd(pcm: Buffer): Buffer {
    this.order.push('aec');
    this.seen.push(pcm);
    return Buffer.concat([Buffer.from('AEC:'), pcm]);
  }
}

class RecordingFilter implements AudioFilter {
  readonly seen: Array<{ pcm: Buffer; sampleRate: number }> = [];
  constructor(private readonly order: string[]) {}
  async process(pcmChunk: Buffer, sampleRate: number): Promise<Buffer> {
    this.order.push('filter');
    this.seen.push({ pcm: pcmChunk, sampleRate });
    return Buffer.concat([Buffer.from('FLT:'), pcmChunk]);
  }
  async close(): Promise<void> {
    /* no-op */
  }
}

class RecordingVad implements VADProvider {
  readonly seen: Array<{ pcm: Buffer; sampleRate: number }> = [];
  private readonly events: Array<VADEvent | null>;
  constructor(private readonly order: string[], events: Array<VADEvent | null> = []) {
    this.events = [...events];
  }
  async processFrame(pcmChunk: Buffer, sampleRate: number): Promise<VADEvent | null> {
    this.order.push('vad');
    this.seen.push({ pcm: pcmChunk, sampleRate });
    return this.events.length > 0 ? this.events.shift()! : null;
  }
  async close(): Promise<void> {
    /* no-op */
  }
}

class RaisingFilter implements AudioFilter {
  calls = 0;
  async process(_pcmChunk: Buffer, _sampleRate: number): Promise<Buffer> {
    this.calls += 1;
    throw new Error('frame size mismatch: expected 160 samples, got 320');
  }
  async close(): Promise<void> {
    /* no-op */
  }
}

function makeChain(opts: {
  aec?: RecordingAec | null;
  audioFilter?: AudioFilter | null;
  vad?: VADProvider | null;
}): InputProcessingChain {
  return new InputProcessingChain({
    resampler: createResampler8kTo16k(),
    getAec: () => opts.aec ?? null,
    getAudioFilter: () => opts.audioFilter ?? null,
    getVad: () => opts.vad ?? null,
  });
}

/** Reference for "what the pre-extraction handler fed downstream". */
function decodeReference(mulaw: Buffer): Buffer {
  return createResampler8kTo16k().process(mulawToPcm16(mulaw));
}

afterEach(() => {
  setLogger(originalLogger);
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Stage order — AEC -> audioFilter -> VAD
// ---------------------------------------------------------------------------

describe('InputProcessingChain — stage order', () => {
  it('runs the filter AFTER AEC and BEFORE VAD', async () => {
    const order: string[] = [];
    const aec = new RecordingAec(order);
    const filter = new RecordingFilter(order);
    const vad = new RecordingVad(order);
    const chain = makeChain({ aec, audioFilter: filter, vad });
    const mulaw = fakeMulawBuffer(20);
    const decoded = decodeReference(mulaw);

    const frame = await chain.process(mulaw);

    expect(order).toEqual(['aec', 'filter', 'vad']);
    // The filter saw the AEC output (not the raw decode) ...
    expect(filter.seen[0].pcm.equals(Buffer.concat([Buffer.from('AEC:'), decoded]))).toBe(true);
    expect(filter.seen[0].sampleRate).toBe(16000);
    // ... and the VAD saw the FILTERED audio.
    expect(
      vad.seen[0].pcm.equals(
        Buffer.concat([Buffer.from('FLT:AEC:'), decoded]),
      ),
    ).toBe(true);
    // The frame handed downstream (self-hearing gate / STT) is the filtered one.
    expect(frame.pcm16k.equals(Buffer.concat([Buffer.from('FLT:AEC:'), decoded]))).toBe(true);
  });

  it('applies the filter without AEC or VAD configured', async () => {
    const order: string[] = [];
    const filter = new RecordingFilter(order);
    const chain = makeChain({ audioFilter: filter });
    const mulaw = fakeMulawBuffer(20);
    const decoded = decodeReference(mulaw);

    const frame = await chain.process(mulaw);

    expect(order).toEqual(['filter']);
    expect(frame.pcm16k.equals(Buffer.concat([Buffer.from('FLT:'), decoded]))).toBe(true);
    expect(frame.vadEvent).toBeNull();
    expect(frame.vadConfigured).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Fail-open filter wrapper — passthrough + warn-once
// ---------------------------------------------------------------------------

describe('InputProcessingChain — filter fail-open', () => {
  it('passes audio through unfiltered when the filter throws', async () => {
    setLogger(silentLogger());
    const filter = new RaisingFilter();
    const chain = makeChain({ audioFilter: filter });
    const mulaw = fakeMulawBuffer(20);

    const frame = await chain.process(mulaw);

    expect(frame.pcm16k.equals(decodeReference(mulaw))).toBe(true);
    expect(filter.calls).toBe(1);
  });

  it('warns exactly once across repeated failures and keeps attempting', async () => {
    const warn = vi.fn();
    const debug = vi.fn();
    setLogger({ ...silentLogger(), warn, debug });
    const filter = new RaisingFilter();
    const chain = makeChain({ audioFilter: filter });
    const mulaw = fakeMulawBuffer(20);
    // Sequential reference: the chain's resampler is stateful across frames.
    const reference = createResampler8kTo16k();

    for (let i = 0; i < 5; i += 1) {
      const frame = await chain.process(mulaw);
      expect(frame.pcm16k.equals(reference.process(mulawToPcm16(mulaw)))).toBe(true);
    }

    const filterWarns = warn.mock.calls.filter((c) => String(c[0]).includes('audioFilter'));
    expect(filterWarns).toHaveLength(1);
    // The filter keeps being attempted (transient failures may recover) and
    // subsequent failures are demoted to DEBUG.
    expect(filter.calls).toBe(5);
    const filterDebugs = debug.mock.calls.filter((c) => String(c[0]).includes('audioFilter'));
    expect(filterDebugs).toHaveLength(4);
  });

  it('treats a non-Buffer return as a failure (passthrough + warn)', async () => {
    const warn = vi.fn();
    setLogger({ ...silentLogger(), warn });
    const badFilter = {
      process: async () => 'not a buffer' as unknown as Buffer,
      close: async () => undefined,
    } as AudioFilter;
    const chain = makeChain({ audioFilter: badFilter });
    const mulaw = fakeMulawBuffer(20);

    const frame = await chain.process(mulaw);

    expect(frame.pcm16k.equals(decodeReference(mulaw))).toBe(true);
    expect(warn.mock.calls.some((c) => String(c[0]).includes('audioFilter'))).toBe(true);
  });

  it('still feeds VAD (with the unfiltered frame) after a filter failure', async () => {
    setLogger(silentLogger());
    const order: string[] = [];
    const vad = new RecordingVad(order, [{ type: 'speech_start' }]);
    const chain = makeChain({ audioFilter: new RaisingFilter(), vad });
    const mulaw = fakeMulawBuffer(20);

    const frame = await chain.process(mulaw);

    expect(vad.seen[0].pcm.equals(decodeReference(mulaw))).toBe(true);
    expect(frame.vadEvent?.type).toBe('speech_start');
    expect(frame.vadConfigured).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Decode parity + VAD plumbing
// ---------------------------------------------------------------------------

describe('InputProcessingChain — decode parity', () => {
  it('matches the stateful mulaw->PCM16 reference across chunk boundaries', async () => {
    const chain = makeChain({});
    const chunkA = Buffer.from(Array.from({ length: 160 }, (_, i) => i % 256));
    const chunkB = Buffer.from(Array.from({ length: 160 }, (_, i) => 255 - (i % 256)));

    const outA = (await chain.process(chunkA)).pcm16k;
    const outB = (await chain.process(chunkB)).pcm16k;

    const reference = createResampler8kTo16k();
    const refA = reference.process(mulawToPcm16(chunkA));
    const refB = reference.process(mulawToPcm16(chunkB));
    expect(outA.equals(refA)).toBe(true);
    expect(outB.equals(refB)).toBe(true);
  });

  it('returns the VAD event and disables VAD for the call after a VAD throw', async () => {
    const warn = vi.fn();
    setLogger({ ...silentLogger(), warn });
    const order: string[] = [];
    let shouldThrow = false;
    const vad: VADProvider = {
      processFrame: async (_pcm: Buffer, _sr: number) => {
        if (shouldThrow) throw new Error('onnx blew up');
        order.push('vad');
        return { type: 'speech_start' } as VADEvent;
      },
      close: async () => undefined,
    };
    const chain = makeChain({ vad });

    const first = await chain.process(fakeMulawBuffer(20));
    expect(first.vadEvent?.type).toBe('speech_start');
    expect(first.vadConfigured).toBe(true);
    expect(chain.isVadDisabled()).toBe(false);

    shouldThrow = true;
    const second = await chain.process(fakeMulawBuffer(20));
    expect(second.vadEvent).toBeNull();
    expect(chain.isVadDisabled()).toBe(true);
    expect(warn.mock.calls.some((c) => String(c[0]).includes('disabling VAD'))).toBe(true);

    // Subsequent frames skip VAD entirely but the self-hearing gate still
    // applies (vadConfigured stays true — parity with the handler gate).
    shouldThrow = false;
    const third = await chain.process(fakeMulawBuffer(20));
    expect(third.vadEvent).toBeNull();
    expect(third.vadConfigured).toBe(true);
  });

  it('resolves AEC/VAD through late-bound getters (post-construction install)', async () => {
    const order: string[] = [];
    const holder: { aec: RecordingAec | null; vad: RecordingVad | null } = {
      aec: null,
      vad: null,
    };
    const chain = new InputProcessingChain({
      resampler: createResampler8kTo16k(),
      getAec: () => holder.aec,
      getAudioFilter: () => null,
      getVad: () => holder.vad,
    });
    const mulaw = fakeMulawBuffer(20);

    const before = await chain.process(mulaw);
    expect(before.vadConfigured).toBe(false);

    holder.aec = new RecordingAec(order);
    holder.vad = new RecordingVad(order);
    const after = await chain.process(mulaw);

    expect(order).toEqual(['aec', 'vad']);
    expect(after.vadConfigured).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Handler wiring — agent.audioFilter finally transforms the STT bytes
// ---------------------------------------------------------------------------

function makeMockBridge(): TelephonyBridge {
  return {
    label: 'TestBridge',
    telephonyProvider: 'twilio',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  };
}

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(),
    close: vi.fn(),
    on: vi.fn(),
    once: vi.fn(),
    readyState: 1,
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makePipelineHandler(agentOverrides: Partial<AgentOptions> = {}): StreamHandler {
  const deps: StreamHandlerDeps = {
    config: {},
    agent: {
      systemPrompt: 'Test agent',
      provider: 'pipeline',
      ...agentOverrides,
    },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(null),
    sanitizeVariables: vi.fn((raw: Record<string, unknown>) => raw as Record<string, string>),
    resolveVariables: vi.fn((tpl: string) => tpl),
  } as unknown as StreamHandlerDeps;
  return new StreamHandler(deps, makeMockWs(), '+15551110000', '+15552220000');
}

describe('StreamHandler — agent.audioFilter wiring (dead-parameter regression)', () => {
  it('feeds the FILTERED bytes to STT', async () => {
    const order: string[] = [];
    const filter = new RecordingFilter(order);
    const handler = makePipelineHandler({ audioFilter: filter });
    const sttSendAudio = vi.fn();
    (handler as unknown as { stt: unknown }).stt = {
      sendAudio: sttSendAudio,
      finalize: vi.fn(),
    };
    const mulaw = fakeMulawBuffer(20);
    const decoded = decodeReference(mulaw);

    await handler.handleAudio(mulaw);

    // The filter ran on the decoded inbound frame ...
    expect(filter.seen).toHaveLength(1);
    expect(filter.seen[0].pcm.equals(decoded)).toBe(true);
    expect(filter.seen[0].sampleRate).toBe(16000);
    // ... and STT received the TRANSFORMED bytes, not the raw decode.
    expect(sttSendAudio).toHaveBeenCalledTimes(1);
    const sent = sttSendAudio.mock.calls[0][0] as Buffer;
    expect(sent.equals(Buffer.concat([Buffer.from('FLT:'), decoded]))).toBe(true);
  });

  it('falls back to unfiltered audio when the filter keeps throwing (call stays alive)', async () => {
    const warn = vi.fn();
    setLogger({ ...silentLogger(), warn });
    const filter = new RaisingFilter();
    const handler = makePipelineHandler({ audioFilter: filter });
    const sttSendAudio = vi.fn();
    (handler as unknown as { stt: unknown }).stt = {
      sendAudio: sttSendAudio,
      finalize: vi.fn(),
    };
    const mulaw = fakeMulawBuffer(20);
    // Sequential reference: the handler's resampler is stateful across frames.
    const reference = createResampler8kTo16k();

    await handler.handleAudio(mulaw);
    await handler.handleAudio(mulaw);

    expect(sttSendAudio).toHaveBeenCalledTimes(2);
    for (const call of sttSendAudio.mock.calls) {
      // Unfiltered passthrough: exactly the decoded/resampled bytes.
      expect((call[0] as Buffer).equals(reference.process(mulawToPcm16(mulaw)))).toBe(true);
    }
    const filterWarns = warn.mock.calls.filter((c) => String(c[0]).includes('audioFilter'));
    expect(filterWarns).toHaveLength(1);
  });

  it('keeps the byte path identical when no filter is configured (regression guard)', async () => {
    const handler = makePipelineHandler();
    const sttSendAudio = vi.fn();
    (handler as unknown as { stt: unknown }).stt = {
      sendAudio: sttSendAudio,
      finalize: vi.fn(),
    };
    const mulaw = fakeMulawBuffer(20);

    await handler.handleAudio(mulaw);

    expect(sttSendAudio).toHaveBeenCalledTimes(1);
    expect((sttSendAudio.mock.calls[0][0] as Buffer).equals(decodeReference(mulaw))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// New APM stages — high-pass / DC-block, AGC, inbound sample-rate validation
// ---------------------------------------------------------------------------

const FULL_SCALE = 32768;

function sineFrames(dbfs: number, freqHz: number, frames: number, n = 320): Buffer[] {
  const amp = FULL_SCALE * 10 ** (dbfs / 20) * Math.SQRT2;
  const out: Buffer[] = [];
  let idx = 0;
  for (let f = 0; f < frames; f += 1) {
    const buf = Buffer.alloc(n * 2);
    for (let i = 0; i < n; i += 1) {
      buf.writeInt16LE(Math.round(amp * Math.sin((2 * Math.PI * freqHz * idx) / 16000)), i * 2);
      idx += 1;
    }
    out.push(buf);
  }
  return out;
}

function rmsOf(pcm: Buffer): number {
  const n = pcm.length / 2;
  if (n === 0) return 0;
  let acc = 0;
  for (let i = 0; i < n; i += 1) {
    const s = pcm.readInt16LE(i * 2);
    acc += s * s;
  }
  return Math.sqrt(acc / n);
}

/** Identity AudioFilter (returns its input unchanged) that records order. */
class PassRecordingFilter implements AudioFilter {
  last: Buffer = Buffer.alloc(0);
  constructor(private readonly order: string[]) {}
  async process(pcmChunk: Buffer, _sr: number): Promise<Buffer> {
    this.order.push('filter');
    this.last = pcmChunk;
    return pcmChunk;
  }
  async close(): Promise<void> {
    /* no-op */
  }
}

function pcmChain(opts: {
  inputSampleRate?: number;
  highPassHz?: number;
  agc?: boolean;
  audioFilter?: AudioFilter | null;
  vad?: VADProvider | null;
}): InputProcessingChain {
  return new InputProcessingChain({
    resampler: createResampler8kTo16k(),
    getAec: () => null,
    getAudioFilter: () => opts.audioFilter ?? null,
    getVad: () => opts.vad ?? null,
    inputIsMulaw8k: false,
    inputSampleRate: opts.inputSampleRate ?? 16000,
    highPassHz: opts.highPassHz,
    agc: opts.agc,
  });
}

describe('InputProcessingChain — high-pass stage', () => {
  it('attenuates a sub-cutoff (50 Hz) tone in the chain output', async () => {
    const chain = pcmChain({ highPassHz: 100 });
    const frames = sineFrames(-12, 50, 30);
    const inRms = rmsOf(Buffer.concat(frames));
    const parts: Buffer[] = [];
    for (const f of frames) parts.push((await chain.process(f)).pcm16k);
    const out = Buffer.concat(parts);
    const attenDb = 20 * Math.log10(rmsOf(out.subarray(1600 * 2)) / inRms);
    expect(attenDb).toBeLessThan(-10);
  });

  it('passes a 440 Hz tone through', async () => {
    const chain = pcmChain({ highPassHz: 100 });
    const frames = sineFrames(-12, 440, 30);
    const inRms = rmsOf(Buffer.concat(frames));
    const parts: Buffer[] = [];
    for (const f of frames) parts.push((await chain.process(f)).pcm16k);
    const out = Buffer.concat(parts);
    const attenDb = 20 * Math.log10(rmsOf(out.subarray(1600 * 2)) / inRms);
    expect(Math.abs(attenDb)).toBeLessThan(1);
  });

  it('is length-preserving on the mu-law path (HPF runs at 8 kHz before resample)', async () => {
    const plain = new InputProcessingChain({
      resampler: createResampler8kTo16k(),
      getAec: () => null,
      getAudioFilter: () => null,
      getVad: () => null,
    });
    const hp = new InputProcessingChain({
      resampler: createResampler8kTo16k(),
      getAec: () => null,
      getAudioFilter: () => null,
      getVad: () => null,
      highPassHz: 100,
    });
    const mulaw = fakeMulawBuffer(20);
    const plainOut = (await plain.process(mulaw)).pcm16k;
    const hpOut = (await hp.process(mulaw)).pcm16k;
    expect(hpOut.length).toBe(plainOut.length);
  });
});

describe('InputProcessingChain — AGC stage', () => {
  it('boosts a quiet input toward the target', async () => {
    const chain = pcmChain({ agc: true });
    const frames = sineFrames(-30, 220, 200);
    let last = Buffer.alloc(0);
    for (const f of frames) last = (await chain.process(f)).pcm16k;
    const outDbfs = 20 * Math.log10(rmsOf(last) / FULL_SCALE);
    expect(Math.abs(outDbfs - -18)).toBeLessThan(2.5);
  });

  it('runs after the filter and before VAD', async () => {
    const order: string[] = [];
    const filter = new PassRecordingFilter(order);
    const vad = new RecordingVad(order);
    const chain = pcmChain({ agc: true, audioFilter: filter, vad });
    const frames = sineFrames(-30, 220, 200);
    let result: Awaited<ReturnType<InputProcessingChain['process']>> | null = null;
    for (const f of frames) result = await chain.process(f);
    expect(order.slice(-2)).toEqual(['filter', 'vad']);
    // VAD saw a louder frame than the filter output → AGC sat between them.
    expect(rmsOf(vad.seen[vad.seen.length - 1].pcm)).toBeGreaterThan(rmsOf(filter.last) * 2);
    expect(result!.pcm16k.equals(vad.seen[vad.seen.length - 1].pcm)).toBe(true);
  });

  it('is off by default (bytes identical)', async () => {
    const chain = pcmChain({});
    const frame = sineFrames(-30, 220, 1)[0];
    expect((await chain.process(frame)).pcm16k.equals(frame)).toBe(true);
  });
});

describe('InputProcessingChain — inbound sample rate', () => {
  it('resamples PCM16 @ 8 kHz up to 16 kHz instead of forwarding it blind', async () => {
    const chain = pcmChain({ inputSampleRate: 8000 });
    const frame = Buffer.alloc(320); // 160 samples of PCM16 @ 8 kHz silence
    const out = (await chain.process(frame)).pcm16k;
    expect(out.length).toBeGreaterThan(frame.length * 1.8);
  });

  it('passes PCM16 @ 16 kHz through untouched', async () => {
    const chain = pcmChain({ inputSampleRate: 16000 });
    const frame = sineFrames(-20, 300, 1)[0];
    expect((await chain.process(frame)).pcm16k.equals(frame)).toBe(true);
  });

  it('rejects an invalid input sample rate', () => {
    expect(() =>
      new InputProcessingChain({
        resampler: createResampler8kTo16k(),
        getAec: () => null,
        getAudioFilter: () => null,
        getVad: () => null,
        inputSampleRate: 0,
      }),
    ).toThrow();
  });
});

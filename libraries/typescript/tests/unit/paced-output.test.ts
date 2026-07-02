/**
 * [unit] Opt-in wall-clock outbound pacing in the pipeline handler.
 *
 * `agent.pacedOutput` routes every outbound pipeline send through an
 * `OutboundFramePacer` that emits fixed 20 ms frames on a monotonic grid
 * (silence fills the gaps). Default OFF must stay byte-identical to the
 * event-driven direct send.
 *
 * The pacer loop is exercised with an injected fake clock/sleep so timing is
 * deterministic; the handler's REAL paced helpers drive it. The tests pin the
 * carrier-native μ-law path (`ttsOutputFormatNativeForCarrier = true`) so
 * `encodePipelineAudio` takes the base64 passthrough branch (no resampler
 * dependency), matching the 160 B / 20 ms μ-law framing.
 *
 * Parity with Python tests/unit/test_paced_output.py.
 */

import { describe, it, expect, vi } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { OutboundFramePacer, mulawSilenceFrame } from '../../src/audio/pacer';
import type { WebSocket as WSWebSocket } from 'ws';

function makeMockBridge(overrides?: Partial<TelephonyBridge>): TelephonyBridge {
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
    ...overrides,
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

function makeDeps(paced: boolean): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-oai-key' },
    agent: { systemPrompt: 'Test agent', provider: 'pipeline', pacedOutput: paced },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(null),
    sanitizeVariables: vi.fn(() => ({})),
    resolveVariables: vi.fn((tpl: string) => tpl),
  } as unknown as StreamHandlerDeps;
}

function makeHandler(paced: boolean): any {
  const h = new StreamHandler(makeDeps(paced), makeMockWs(), '+15551111111', '+15552222222');
  // Carrier-native μ-law path: encodePipelineAudio base64-passes the frame
  // through (no resampler needed) and the pacer frames at 160 B.
  h.ttsOutputFormatNativeForCarrier = true;
  return h;
}

class FakeClock {
  t = 1000.0;
  now = (): number => this.t;
  sleep = async (delay: number): Promise<void> => {
    this.t += Math.max(0, delay);
  };
}

/** Mirror `startPacer` but inject the fake clock/sleep for determinism. */
function attachTestPacer(h: any, clock: FakeClock): OutboundFramePacer {
  h.pacerBytesPerMs = 8; // native μ-law 8 kHz
  h.pacerSilence = mulawSilenceFrame();
  h.pacer = new OutboundFramePacer({
    frameBytes: 160,
    silenceFrame: h.pacerSilence,
    sendFrame: async (frame: Buffer) => h.pacedSendFrame(frame),
    clock: clock.now,
    sleep: clock.sleep,
  });
  return h.pacer;
}

function sentBase64(bridge: TelephonyBridge): string[] {
  return (bridge.sendAudio as any).mock.calls.map((c: unknown[]) => c[1] as string);
}

describe('[unit] paced output — framing', () => {
  it('emits fixed 160 B frames on a 20 ms grid then silence', async () => {
    const h = makeHandler(true);
    const clock = new FakeClock();
    const pacer = attachTestPacer(h, clock);

    // Real send-site: enqueue exactly 3 frames of real audio.
    h.sendPipelineChunk(Buffer.alloc(160 * 3, 0x01));
    expect((h.deps.bridge.sendAudio as any).mock.calls.length).toBe(0);

    await pacer.run({ maxFrames: 5 });

    const sent = sentBase64(h.deps.bridge);
    const real = Buffer.alloc(160, 0x01).toString('base64');
    const silence = mulawSilenceFrame().toString('base64');
    expect(sent[0]).toBe(real);
    expect(sent[1]).toBe(real);
    expect(sent[2]).toBe(real);
    expect(sent[3]).toBe(silence); // queue drained -> silence
    expect(sent[4]).toBe(silence);
    expect(clock.t).toBeCloseTo(1000.0 + 5 * 0.02, 6);
  });

  it('re-slices an arbitrary chunk length into fixed frames', async () => {
    const h = makeHandler(true);
    const clock = new FakeClock();
    const pacer = attachTestPacer(h, clock);
    h.sendPipelineChunk(Buffer.alloc(240, 0x02)); // 1.5 frames
    await pacer.run({ maxFrames: 2 });
    const sent = sentBase64(h.deps.bridge);
    expect(sent[0]).toBe(Buffer.alloc(160, 0x02).toString('base64'));
    // remainder padded to a full frame with the silence tail.
    const padded = Buffer.concat([Buffer.alloc(80, 0x02), mulawSilenceFrame().subarray(80)]);
    expect(sent[1]).toBe(padded.toString('base64'));
  });
});

describe('[unit] paced output — barge-in clear', () => {
  it('drops queued frames promptly and resumes silence', async () => {
    const h = makeHandler(true);
    const clock = new FakeClock();
    const pacer = attachTestPacer(h, clock);
    h.sendPipelineChunk(Buffer.alloc(160 * 3, 0x03)); // 3 frames queued
    h.pacedEmittedMs = 42;
    expect(h.pacer.pendingBytes).toBe(160 * 3);

    // The barge-in cancel wiring drops the pacer queue + resets the cursor.
    h.pacer.clear();
    h.pacedEmittedMs = 0;

    expect(h.pacer.pendingBytes).toBe(0);
    await pacer.run({ maxFrames: 1 });
    const sent = sentBase64(h.deps.bridge);
    expect(sent[sent.length - 1]).toBe(mulawSilenceFrame().toString('base64'));
  });
});

describe('[unit] paced output — playback tracking', () => {
  it('heard prefix tracks REAL emitted frames, not the enqueue burst', async () => {
    const h = makeHandler(true);
    const clock = new FakeClock();
    const pacer = attachTestPacer(h, clock);

    // Sentence "one" stamped at the current cursor, then 2 frames enqueued.
    h.turnSpokenSegments.push({ text: 'one', startMs: h.turnPlaybackTotalMs, durMs: 0 });
    h.sendPipelineChunk(Buffer.alloc(160 * 2, 0x01)); // +40 ms
    // Sentence "two" stamped at 40 ms, then 1 frame enqueued.
    h.turnSpokenSegments.push({ text: 'two', startMs: h.turnPlaybackTotalMs, durMs: 0 });
    h.sendPipelineChunk(Buffer.alloc(160, 0x02)); // +20 ms (total 60 ms)

    // Only ONE 20 ms frame actually leaves the SDK.
    await pacer.run({ maxFrames: 1 });

    const prefix = h.heardResponsePrefix();
    expect(prefix).not.toBeNull();
    // (d) The paced emitted-frame cursor (NOT the faster enqueue burst) drives
    // the cutoff: heard ~= 20 ms lands mid-"one" (0..40 ms); the single word
    // rounds to the nearest boundary (heard) while "two" (start 40 ms) has not
    // begun.
    expect(prefix.text).toBe('one');
    expect(prefix.heardEverything).toBe(false);
  });
});

describe('[unit] paced output — default OFF byte-identical', () => {
  it('does not create a pacer when disabled', () => {
    const h = makeHandler(false);
    expect(h.pacer).toBeNull();
    h.startPacer(); // explicit no-op when disabled
    expect(h.pacer).toBeNull();
  });

  it('sends directly with unchanged bytes and tracks playback', () => {
    const h = makeHandler(false);
    const chunk = Buffer.alloc(321, 0x07); // arbitrary odd length
    const before = h.playbackBufferedUntil;
    h.sendPipelineChunk(chunk);
    const sent = sentBase64(h.deps.bridge);
    expect(sent.length).toBe(1);
    expect(sent[0]).toBe(chunk.toString('base64')); // exact bytes, no reframe
    expect(h.firstAudioSentAt).not.toBeNull();
    expect(h.playbackBufferedUntil).toBeGreaterThan(before); // trackOutboundPlayback ran
  });
});

/**
 * [unit] Opt-in wall-clock outbound pacing in REALTIME-engine mode.
 *
 * `agent.pacedOutput` in pipeline mode already routes every outbound send
 * through an `OutboundFramePacer`. These tests pin the realtime-engine half:
 * the OpenAI-GA adapter family (`OpenAIRealtime2Adapter` and its
 * `XaiRealtimeAdapter` subclass, plus the v1 `OpenAIRealtimeAdapter` on its
 * default `g711_ulaw` format) hands the handler carrier-native mu-law 8 kHz,
 * so `onAdapterAudio` enqueues on the pacer instead of burst-dumping the whole
 * model turn into the carrier socket.
 *
 * The pacer loop is exercised with an injected fake clock/sleep so timing is
 * deterministic; the handler's REAL paced helpers drive it. Default OFF — and
 * every non-mu-law adapter — must stay byte-identical to the direct send.
 */

import { describe, it, expect, vi } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { OutboundFramePacer, mulawSilenceFrame } from '../../src/audio/pacer';
import {
  OpenAIRealtimeAdapter,
  OpenAIRealtimeAudioFormat,
} from '../../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../../src/providers/openai-realtime-2';
import type { WebSocket as WSWebSocket } from 'ws';

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
  } as unknown as TelephonyBridge;
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
    agent: {
      systemPrompt: 'Test agent',
      provider: 'openai_realtime_2',
      pacedOutput: paced,
    },
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

/** Realtime-mode handler with a GA adapter already installed. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makeHandler(paced: boolean, adapter?: unknown): any {
  const h = new StreamHandler(makeDeps(paced), makeMockWs(), '+15551111111', '+15552222222');
  h.adapter = adapter !== undefined ? adapter : new OpenAIRealtime2Adapter('test-key');
  return h;
}

class FakeClock {
  t = 1000.0;
  now = (): number => this.t;
  sleep = async (delay: number): Promise<void> => {
    this.t += Math.max(0, delay);
  };
}

/** Mirror `startRealtimePacer` but inject the fake clock/sleep. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function attachTestPacer(h: any, clock: FakeClock): OutboundFramePacer {
  h.pacerBytesPerMs = 8; // realtime engines emit mu-law 8 kHz
  h.pacerSilence = mulawSilenceFrame();
  h.pacer = new OutboundFramePacer({
    frameBytes: 160,
    silenceFrame: h.pacerSilence,
    sendFrame: async (frame: Buffer) => h.pacedSendRealtimeFrame(frame),
    clock: clock.now,
    sleep: clock.sleep,
  });
  return h.pacer;
}

function sentBase64(bridge: TelephonyBridge): string[] {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (bridge.sendAudio as any).mock.calls.map((c: unknown[]) => c[1] as string);
}

describe('[unit] realtime paced output — framing', () => {
  it('meters a burst of adapter audio onto the 20 ms wall clock', async () => {
    const h = makeHandler(true);
    const clock = new FakeClock();
    const pacer = attachTestPacer(h, clock);

    // Real send-site: the adapter delivers 60 ms of audio in one burst.
    await h.onAdapterAudio(Buffer.alloc(160 * 3, 0x11));
    // Nothing reached the carrier socket yet — the burst is queued, not dumped.
    expect(sentBase64(h.deps.bridge).length).toBe(0);
    expect(h.pacer.pendingBytes).toBe(160 * 3);

    await pacer.run({ maxFrames: 5 });

    const sent = sentBase64(h.deps.bridge);
    const real = Buffer.alloc(160, 0x11).toString('base64');
    const silence = mulawSilenceFrame().toString('base64');
    expect(sent).toEqual([real, real, real, silence, silence]);
    // 5 frames took exactly 5 * 20 ms of wall clock, not 5 immediate writes.
    expect(clock.t).toBeCloseTo(1000.0 + 5 * 0.02, 6);
    // Only REAL frames advance the heard cursor; the silence gap-fill does not.
    expect(h.pacedEmittedMs).toBe(60);
    expect(h.firstAudioSentAt).not.toBeNull();
  });

  it('re-slices an arbitrary adapter chunk into fixed frames untranscoded', async () => {
    const h = makeHandler(true);
    const clock = new FakeClock();
    const pacer = attachTestPacer(h, clock);

    await h.onAdapterAudio(Buffer.alloc(240, 0x12)); // 1.5 frames
    await pacer.run({ maxFrames: 2 });

    const sent = sentBase64(h.deps.bridge);
    // Byte-for-byte passthrough: the adapter's mu-law is NOT re-encoded.
    expect(sent[0]).toBe(Buffer.alloc(160, 0x12).toString('base64'));
    const padded = Buffer.concat([Buffer.alloc(80, 0x12), mulawSilenceFrame().subarray(80)]);
    expect(sent[1]).toBe(padded.toString('base64'));
  });

  it('re-anchors the paced cursors at the start of each agent turn', async () => {
    const h = makeHandler(true);
    const clock = new FakeClock();
    attachTestPacer(h, clock);

    await h.onAdapterAudio(Buffer.alloc(160, 0x13));
    h.pacedEmittedMs = 20;
    // A new agent turn opens (speech_stopped clears ``responseAudioStarted``).
    h.responseAudioStarted = false;
    await h.onAdapterAudio(Buffer.alloc(160, 0x14));

    expect(h.pacedEmittedMs).toBe(0);
    expect(h.turnPlaybackTotalMs).toBe(20); // only THIS turn's enqueue counts
  });
});

describe('[unit] realtime paced output — barge-in clear', () => {
  it('flushes the pacer queue and bounds truncate by paced-emitted ms', async () => {
    const adapter = new OpenAIRealtime2Adapter('test-key');
    const truncateSpy = vi.spyOn(adapter, 'truncate').mockImplementation(() => {});
    const h = makeHandler(true, adapter);
    const clock = new FakeClock();
    const pacer = attachTestPacer(h, clock);

    // 60 ms enqueued, only 20 ms actually emitted to the caller.
    await h.onAdapterAudio(Buffer.alloc(160 * 3, 0x15));
    await pacer.run({ maxFrames: 1 });
    expect(h.pacedEmittedMs).toBe(20);
    expect(h.pacer.pendingBytes).toBe(160 * 2);

    await h.onAdapterSpeechInterrupt();

    // The carrier was cleared AND the pacer's own (unreachable) backlog dropped.
    expect(h.deps.bridge.sendClear).toHaveBeenCalledTimes(1);
    expect(h.pacer.pendingBytes).toBe(0);
    // Truncation is anchored on what the caller HEARD, not what was generated.
    expect(truncateSpy).toHaveBeenCalledWith(20);
    expect(h.turnPlaybackTotalMs).toBe(20);

    // Nothing stale escapes after the clear — the next tick is silence.
    await pacer.run({ maxFrames: 1 });
    const sent = sentBase64(h.deps.bridge);
    expect(sent[sent.length - 1]).toBe(mulawSilenceFrame().toString('base64'));
  });
});

describe('[unit] realtime paced output — default OFF byte-identical', () => {
  it('does not create a pacer when pacedOutput is disabled', () => {
    const h = makeHandler(false);
    expect(h.pacer).toBeNull();
    h.startRealtimePacer(); // explicit no-op when disabled
    expect(h.pacer).toBeNull();
  });

  it('sends adapter audio directly with unchanged bytes when disabled', async () => {
    const h = makeHandler(false);
    const chunk = Buffer.alloc(321, 0x07); // arbitrary odd length
    await h.onAdapterAudio(chunk);
    const sent = sentBase64(h.deps.bridge);
    expect(sent.length).toBe(1);
    expect(sent[0]).toBe(chunk.toString('base64')); // exact bytes, no reframe
    expect(h.firstAudioSentAt).not.toBeNull();
    expect(h.deps.bridge.sendMark).toHaveBeenCalledTimes(1);
  });

  it('leaves truncate unbounded when unpaced', async () => {
    const adapter = new OpenAIRealtime2Adapter('test-key');
    const truncateSpy = vi.spyOn(adapter, 'truncate').mockImplementation(() => {});
    const h = makeHandler(false, adapter);
    await h.onAdapterSpeechInterrupt();
    expect(truncateSpy).toHaveBeenCalledWith(undefined);
  });
});

describe('[unit] realtime paced output — adapter eligibility gate', () => {
  it('starts the pacer for the OpenAI-GA adapter family', async () => {
    const h = makeHandler(true);
    h.startRealtimePacer();
    expect(h.pacer).not.toBeNull();
    expect(h.pacerBytesPerMs).toBe(8);
    expect(h.pacerSilence).toEqual(mulawSilenceFrame());
    await h.stopPacer();
    expect(h.pacer).toBeNull();
  });

  it('starts the pacer for the v1 adapter on its default g711_ulaw format', async () => {
    const h = makeHandler(true, new OpenAIRealtimeAdapter('test-key'));
    h.startRealtimePacer();
    expect(h.pacer).not.toBeNull();
    await h.stopPacer();
  });

  it('skips a v1 adapter negotiated on a non-mulaw format', () => {
    const adapter = new OpenAIRealtimeAdapter(
      'test-key',
      undefined,
      undefined,
      undefined,
      undefined,
      OpenAIRealtimeAudioFormat.PCM16,
    );
    const h = makeHandler(true, adapter);
    h.startRealtimePacer();
    expect(h.pacer).toBeNull();
  });

  it('skips non-OpenAI realtime adapters (Gemini / ConvAI keep the direct send)', async () => {
    const h = makeHandler(true, { close: vi.fn() });
    h.startRealtimePacer();
    expect(h.pacer).toBeNull();

    const chunk = Buffer.alloc(160, 0x09);
    await h.onAdapterAudio(chunk);
    expect(sentBase64(h.deps.bridge)).toEqual([chunk.toString('base64')]);
  });
});

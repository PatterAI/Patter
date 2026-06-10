/**
 * Unit tests for carrier-neutral local call recording wiring.
 *
 * Covers (mirror of Python tests/unit/test_local_recording.py):
 * - StreamHandler taps: caller tap in handleAudio, agent taps in
 *   encodePipelineAudio (pipeline) and onAdapterAudio (Realtime / ConvAI),
 * - fireCallEnd finalizing the WAV on every teardown path (handleStop and
 *   the abnormal handleWsClose) and surfacing `recording_path`,
 * - EmbeddedServer.makeLocalRecorder path resolution,
 * - config off → zero filesystem writes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { LocalCallRecorder, RECORDING_SAMPLE_RATE } from '../../src/audio/call-recorder';
import { EmbeddedServer } from '../../src/server';
import type { LocalConfig } from '../../src/server';
import type { AgentOptions } from '../../src/types';
import type { WebSocket as WSWebSocket } from 'ws';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const FRAME_SAMPLES_20MS = RECORDING_SAMPLE_RATE / 50; // 320

const tmpDirs: string[] = [];

function mkTmp(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-localrec-'));
  tmpDirs.push(dir);
  return dir;
}

function pcmFrame(value: number, samples = FRAME_SAMPLES_20MS): Buffer {
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) buf.writeInt16LE(value, i * 2);
  return buf;
}

function mulawFrame(byte = 0x55, numBytes = 160): Buffer {
  return Buffer.alloc(numBytes, byte);
}

function parseWavHeader(p: string): { channels: number; sampleRate: number; frames: number } {
  const buf = fs.readFileSync(p);
  expect(buf.subarray(0, 4).toString('ascii')).toBe('RIFF');
  const dataSize = buf.readUInt32LE(40);
  expect(44 + dataSize).toBe(buf.length);
  return {
    channels: buf.readUInt16LE(22),
    sampleRate: buf.readUInt32LE(24),
    frames: dataSize / 4,
  };
}

function makeMockBridge(overrides?: Partial<TelephonyBridge>): TelephonyBridge {
  return {
    label: 'TestBridge',
    telephonyProvider: 'twilio',
    inputWireFormat: 'ulaw_8000',
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

function makeMockAdapter() {
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    sendText: vi.fn().mockResolvedValue(undefined),
    cancelResponse: vi.fn(),
    onEvent: vi.fn(),
    sendFunctionResult: vi.fn().mockResolvedValue(undefined),
  };
}

function makeDeps(overrides?: Partial<StreamHandlerDeps>): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-oai-key' },
    agent: { systemPrompt: 'Test agent', provider: 'openai_realtime' },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(makeMockAdapter()),
    sanitizeVariables: vi.fn((raw) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl, vars) => {
      let result = tpl;
      for (const [k, v] of Object.entries(vars)) result = result.replaceAll(`{${k}}`, v);
      return result;
    }),
    ...overrides,
  };
}

/** The recorder attached to a handler (private field, asserted in tests). */
function recorderOf(handler: StreamHandler): LocalCallRecorder | null {
  return (handler as unknown as { localRecorder: LocalCallRecorder | null }).localRecorder;
}

describe('local recording wiring', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
    fetchSpy.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    } as Response);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    while (tmpDirs.length > 0) {
      fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
    }
  });

  // -------------------------------------------------------------------------
  // StreamHandler taps
  // -------------------------------------------------------------------------

  describe('StreamHandler taps', () => {
    it('creates the recorder via deps.makeLocalRecorder at handleCallStart', async () => {
      const dir = mkTmp();
      const factory = vi.fn((callId: string) =>
        new LocalCallRecorder(path.join(dir, `${callId}.wav`)),
      );
      const handler = new StreamHandler(
        makeDeps({ makeLocalRecorder: factory }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      await handler.handleCallStart('CA-rec-1');
      expect(factory).toHaveBeenCalledExactlyOnceWith('CA-rec-1');
      expect(recorderOf(handler)).not.toBeNull();
      await handler.handleStop();
    });

    it('taps caller audio in handleAudio even before the adapter exists (mulaw wire)', async () => {
      const dir = mkTmp();
      const handler = new StreamHandler(
        makeDeps({
          makeLocalRecorder: (id) => new LocalCallRecorder(path.join(dir, `${id}.wav`)),
        }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      await handler.handleCallStart('CA-rec-2');
      // Sever the adapter to simulate "still connecting" — the tap sits
      // above every engine-mode branch, so frames are still captured.
      (handler as unknown as { adapter: unknown }).adapter = null;
      for (let i = 0; i < 5; i++) await handler.handleAudio(mulawFrame());
      // 5 × 160 mulaw bytes @8k → ≈ 5 × 320 samples @16k (resampler lag ≤ 2)
      expect(Math.abs(recorderOf(handler)!.callerSamples - 5 * 320)).toBeLessThanOrEqual(2);
      await handler.handleStop();
    });

    it('taps adapter (agent) audio in onAdapterAudio via the event dispatcher', async () => {
      const dir = mkTmp();
      const mockAdapter = makeMockAdapter();
      const handler = new StreamHandler(
        makeDeps({
          buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
          makeLocalRecorder: (id) => new LocalCallRecorder(path.join(dir, `${id}.wav`)),
        }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      await handler.handleCallStart('CA-rec-3');
      const dispatch = (mockAdapter.onEvent as ReturnType<typeof vi.fn>).mock
        .calls[0][0] as (type: string, data: unknown) => Promise<void>;
      // The GA Realtime adapter emits wire-format μ-law 8 kHz.
      await dispatch('audio', mulawFrame(0x10));
      expect(Math.abs(recorderOf(handler)!.agentSamples - 320)).toBeLessThanOrEqual(2);
      await handler.handleStop();
    });

    it('taps pipeline TTS chunks in encodePipelineAudio (PCM16 path)', () => {
      const dir = mkTmp();
      const handler = new StreamHandler(makeDeps(), makeMockWs(), '+1555', '+1556');
      const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
      const h = handler as unknown as {
        localRecorder: LocalCallRecorder;
        ttsOutputFormatNativeForCarrier: boolean;
        encodePipelineAudio(chunk: Buffer): string;
      };
      h.localRecorder = rec;
      h.ttsOutputFormatNativeForCarrier = false;
      h.encodePipelineAudio(pcmFrame(7));
      expect(rec.agentSamples).toBe(320);
      rec.close();
    });

    it('decodes carrier-native μ-law on the pipeline fast path instead of skipping', () => {
      const dir = mkTmp();
      const handler = new StreamHandler(makeDeps(), makeMockWs(), '+1555', '+1556');
      const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
      const h = handler as unknown as {
        localRecorder: LocalCallRecorder;
        ttsOutputFormatNativeForCarrier: boolean;
        encodePipelineAudio(chunk: Buffer): string;
      };
      h.localRecorder = rec;
      h.ttsOutputFormatNativeForCarrier = true; // Twilio bridge → μ-law 8 kHz
      h.encodePipelineAudio(mulawFrame());
      // 160 mulaw bytes @8k ≈ 320 samples @16k after decode + resample
      expect(Math.abs(rec.agentSamples - 320)).toBeLessThanOrEqual(2);
      rec.close();
    });

    it('treats Telnyx-native pipeline output (pcm_16000) as PCM16 16 kHz', () => {
      const dir = mkTmp();
      const handler = new StreamHandler(
        makeDeps({ bridge: makeMockBridge({ telephonyProvider: 'telnyx' }) }),
        makeMockWs(),
        '+1555',
        '+1556',
      );
      const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
      const h = handler as unknown as {
        localRecorder: LocalCallRecorder;
        ttsOutputFormatNativeForCarrier: boolean;
        encodePipelineAudio(chunk: Buffer): string;
      };
      h.localRecorder = rec;
      h.ttsOutputFormatNativeForCarrier = true;
      h.encodePipelineAudio(pcmFrame(9));
      expect(rec.agentSamples).toBe(320);
      rec.close();
    });
  });

  // -------------------------------------------------------------------------
  // Finalization through the teardown paths
  // -------------------------------------------------------------------------

  describe('finalization', () => {
    it('handleStop finalizes the WAV and surfaces recording_path in onCallEnd', async () => {
      const dir = mkTmp();
      const onCallEnd = vi.fn().mockResolvedValue(undefined);
      const mockAdapter = makeMockAdapter();
      const handler = new StreamHandler(
        makeDeps({
          onCallEnd,
          buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
          makeLocalRecorder: (id) => new LocalCallRecorder(path.join(dir, `${id}.wav`)),
        }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      await handler.handleCallStart('CA-end-1');
      const dispatch = (mockAdapter.onEvent as ReturnType<typeof vi.fn>).mock
        .calls[0][0] as (type: string, data: unknown) => Promise<void>;
      await handler.handleAudio(mulawFrame()); // caller
      await dispatch('audio', mulawFrame(0x10)); // agent
      await handler.handleStop();

      const rec = recorderOf(handler)!;
      expect(rec.closed).toBe(true);
      const wavPath = path.join(dir, 'CA-end-1.wav');
      const payload = onCallEnd.mock.calls[0][0] as Record<string, unknown>;
      expect(payload.recording_path).toBe(wavPath);
      const wav = parseWavHeader(wavPath);
      expect(wav.channels).toBe(2);
      expect(wav.sampleRate).toBe(16000);
      expect(wav.frames).toBeGreaterThan(0);
    });

    it('handleWsClose (abnormal teardown) still patches the WAV header', async () => {
      const dir = mkTmp();
      const handler = new StreamHandler(
        makeDeps({
          makeLocalRecorder: (id) => new LocalCallRecorder(path.join(dir, `${id}.wav`)),
        }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      await handler.handleCallStart('CA-drop-1');
      await handler.handleAudio(mulawFrame());
      // No carrier stop event — straight to the WS-drop teardown path.
      await handler.handleWsClose();
      expect(recorderOf(handler)!.closed).toBe(true);
      const wav = parseWavHeader(path.join(dir, 'CA-drop-1.wav'));
      expect(wav.frames).toBeGreaterThan(0);
      // Double teardown (stop + ws close both firing) stays safe.
      await handler.handleStop();
    });

    it('omits recording_path entirely when local recording is off', async () => {
      const onCallEnd = vi.fn().mockResolvedValue(undefined);
      const handler = new StreamHandler(
        makeDeps({ onCallEnd }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      await handler.handleCallStart('CA-off-1');
      await handler.handleAudio(mulawFrame()); // tap is a no-op
      await handler.handleStop();
      const payload = onCallEnd.mock.calls[0][0] as Record<string, unknown>;
      expect('recording_path' in payload).toBe(false);
    });

    it('survives a throwing recorder factory (call proceeds unrecorded)', async () => {
      const handler = new StreamHandler(
        makeDeps({
          makeLocalRecorder: () => {
            throw new Error('boom');
          },
        }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      await handler.handleCallStart('CA-factory-throw');
      expect(recorderOf(handler)).toBeNull();
      await handler.handleAudio(mulawFrame()); // must not raise
      await handler.handleStop();
    });
  });

  // -------------------------------------------------------------------------
  // EmbeddedServer.makeLocalRecorder — path resolution
  // -------------------------------------------------------------------------

  describe('EmbeddedServer.makeLocalRecorder', () => {
    const originalEnv = { ...process.env };

    beforeEach(() => {
      delete process.env.PATTER_LOG_DIR;
    });

    afterEach(() => {
      process.env = { ...originalEnv };
    });

    function makeConfig(overrides: Partial<LocalConfig> = {}): LocalConfig {
      return {
        twilioSid: 'AC_test',
        twilioToken: 'tok_test',
        openaiKey: 'sk_test',
        phoneNumber: '+15550000000',
        webhookUrl: 'abc.ngrok.io',
        telephonyProvider: 'twilio',
        requireSignature: false,
        ...overrides,
      };
    }

    function makeAgent(): AgentOptions {
      return { systemPrompt: 'You are helpful.' };
    }

    function makeServer(
      localRecording: boolean | string,
      configOverrides: Partial<LocalConfig> = {},
    ): EmbeddedServer {
      return new EmbeddedServer(
        makeConfig(configOverrides),
        makeAgent(),
        undefined, // onCallStart
        undefined, // onCallEnd
        undefined, // onTranscript
        undefined, // onMessage
        false, // recording
        '', // voicemailMessage
        undefined, // onMetrics
        undefined, // pricing
        false, // dashboard
        '', // dashboardToken
        false, // allowInsecureDashboard
        localRecording,
      );
    }

    it('is disabled by default and performs zero filesystem writes', () => {
      const server = new EmbeddedServer(makeConfig(), makeAgent());
      expect(server.makeLocalRecorder('CA123')).toBeNull();
      expect(server.makeLocalRecorder('CA456')).toBeNull();
      // The CWD fallback dir must NOT have been created.
      expect(fs.existsSync('recordings')).toBe(false);
    });

    it('uses an explicit directory string as <dir>/<call_id>.wav', () => {
      const dir = mkTmp();
      const server = makeServer(path.join(dir, 'recs'));
      const recorder = server.makeLocalRecorder('CA123');
      expect(recorder).not.toBeNull();
      expect(recorder!.path).toBe(path.join(dir, 'recs', 'CA123.wav'));
      recorder!.close();
    });

    it('places recording.wav in the call-log dir when logging is enabled', () => {
      const dir = mkTmp();
      const server = makeServer(true, { persistRoot: path.join(dir, 'logs') });
      const callLogger = (
        server as unknown as {
          callLogger: { enabled: boolean; callDir(id: string): string | null };
        }
      ).callLogger;
      expect(callLogger.enabled).toBe(true);
      const recorder = server.makeLocalRecorder('CA123');
      expect(recorder).not.toBeNull();
      expect(path.basename(recorder!.path)).toBe('recording.wav');
      expect(path.dirname(recorder!.path)).toBe(callLogger.callDir('CA123'));
      recorder!.close();
    });

    it('falls back to ./recordings/<call_id>.wav without call logging', () => {
      const hadRecordingsDir = fs.existsSync('recordings');
      const server = makeServer(true);
      const recorder = server.makeLocalRecorder('CA123');
      try {
        expect(recorder).not.toBeNull();
        expect(recorder!.path).toBe(path.join('recordings', 'CA123.wav'));
        expect(fs.existsSync(recorder!.path)).toBe(true);
      } finally {
        recorder?.close();
        // Clean the eagerly created fallback artifacts out of the repo CWD.
        fs.rmSync(path.join('recordings', 'CA123.wav'), { force: true });
        if (!hadRecordingsDir) fs.rmSync('recordings', { recursive: true, force: true });
      }
    });

    it('sanitizes hostile call ids out of the filename', () => {
      const dir = mkTmp();
      const server = makeServer(path.join(dir, 'recs'));
      const recorder = server.makeLocalRecorder('../evil/../../id');
      expect(recorder).not.toBeNull();
      expect(path.dirname(recorder!.path)).toBe(path.join(dir, 'recs'));
      expect(path.basename(recorder!.path)).not.toContain('/');
      recorder!.close();
    });

    it('returns null (never throws) when the target dir is unusable', () => {
      const dir = mkTmp();
      const blocker = path.join(dir, 'blocked');
      fs.writeFileSync(blocker, 'not a directory');
      const server = makeServer(blocker);
      expect(server.makeLocalRecorder('CA123')).toBeNull();
    });
  });
});

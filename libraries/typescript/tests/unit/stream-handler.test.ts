import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import { fakeAudioBuffer } from '../setup';
import type { WebSocket as WSWebSocket } from 'ws';

// ---------------------------------------------------------------------------
// Factory helpers
// ---------------------------------------------------------------------------

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
    agent: {
      systemPrompt: 'Test agent',
      provider: 'openai_realtime',
    },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(makeMockAdapter()),
    sanitizeVariables: vi.fn((raw) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) {
        safe[k] = String(v);
      }
      return safe;
    }),
    resolveVariables: vi.fn((tpl, vars) => {
      let result = tpl;
      for (const [k, v] of Object.entries(vars)) {
        result = result.replaceAll(`{${k}}`, v);
      }
      return result;
    }),
    ...overrides,
  };
}

describe('StreamHandler', () => {
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
  });

  // --- Construction ---

  it('creates a StreamHandler without error', () => {
    const deps = makeDeps();
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
    expect(handler).toBeDefined();
  });

  // --- handleCallStart ---

  describe('handleCallStart()', () => {
    it('initializes the realtime adapter for non-pipeline mode', async () => {
      const mockAdapter = makeMockAdapter();
      const deps = makeDeps({
        buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-123');

      expect(deps.buildAIAdapter).toHaveBeenCalledOnce();
      expect(mockAdapter.connect).toHaveBeenCalledOnce();
    });

    it('fires onCallStart callback', async () => {
      const onCallStart = vi.fn().mockResolvedValue(undefined);
      const deps = makeDeps({ onCallStart });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-456');

      expect(onCallStart).toHaveBeenCalledWith(
        expect.objectContaining({
          call_id: 'call-456',
          caller: '+15551111111',
          callee: '+15552222222',
          direction: 'inbound',
        }),
      );
    });

    it('records call start in metrics store', async () => {
      const store = new MetricsStore();
      const spy = vi.spyOn(store, 'recordCallStart');
      const deps = makeDeps({ metricsStore: store });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-789');
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ call_id: 'call-789' }),
      );
    });

    it('resolves variables in system prompt', async () => {
      const resolveVariables = vi.fn((tpl: string, vars: Record<string, string>) => {
        let result = tpl;
        for (const [k, v] of Object.entries(vars)) {
          result = result.replaceAll(`{${k}}`, v);
        }
        return result;
      });
      const deps = makeDeps({
        agent: {
          systemPrompt: 'Hello {name}!',
          provider: 'openai_realtime',
          variables: { name: 'World' },
        },
        resolveVariables,
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-var');
      expect(resolveVariables).toHaveBeenCalled();
      const calledWith = (deps.buildAIAdapter as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(calledWith).toBe('Hello World!');
    });

    it('passes custom params to onCallStart', async () => {
      const onCallStart = vi.fn().mockResolvedValue(undefined);
      const deps = makeDeps({ onCallStart });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      await handler.handleCallStart('call-cp', { company: 'Acme' });
      expect(onCallStart).toHaveBeenCalledWith(
        expect.objectContaining({ custom_params: { company: 'Acme' } }),
      );
    });

    it('starts recording when enabled (Twilio)', async () => {
      fetchSpy.mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({}),
        text: async () => '',
      } as Response);
      const deps = makeDeps({
        config: { openaiKey: 'key', twilioSid: 'AC123', twilioToken: 'tok' },
        recording: true,
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

      // Must be a valid Twilio CallSid (CA + 32 hex) — the recording start
      // now validates the SID to prevent SSRF against the Twilio API.
      await handler.handleCallStart('CA00000000000000000000000000000001');
      // Should have called fetch for recording + adapter connect
      const recordingCall = fetchSpy.mock.calls.find(
        (c) => typeof c[0] === 'string' && c[0].includes('Recordings.json'),
      );
      expect(recordingCall).toBeDefined();
    });
  });

  // --- handleAudio ---

  describe('handleAudio()', () => {
    it('forwards audio to adapter in realtime mode', async () => {
      const mockAdapter = makeMockAdapter();
      const deps = makeDeps({
        buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-audio');

      const audio = fakeAudioBuffer(20);
      handler.handleAudio(audio);

      expect(mockAdapter.sendAudio).toHaveBeenCalledWith(audio);
    });
  });

  // --- handleDtmf ---

  describe('handleDtmf()', () => {
    it('fires onTranscript with DTMF digit', async () => {
      const onTranscript = vi.fn().mockResolvedValue(undefined);
      const deps = makeDeps({ onTranscript });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-dtmf');

      await handler.handleDtmf('5');
      expect(onTranscript).toHaveBeenCalledWith(
        expect.objectContaining({ text: '[DTMF: 5]' }),
      );
    });
  });

  // --- handleStop / handleWsClose ---

  describe('handleStop()', () => {
    it('fires call end and records metrics', async () => {
      const onCallEnd = vi.fn().mockResolvedValue(undefined);
      const store = new MetricsStore();
      const spy = vi.spyOn(store, 'recordCallEnd');
      const deps = makeDeps({ onCallEnd, metricsStore: store });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-stop');

      await handler.handleStop();
      expect(onCallEnd).toHaveBeenCalledWith(
        expect.objectContaining({ call_id: 'call-stop' }),
      );
      expect(spy).toHaveBeenCalled();
    });
  });

  describe('handleWsClose()', () => {
    it('fires call end only once (idempotent)', async () => {
      const onCallEnd = vi.fn().mockResolvedValue(undefined);
      const deps = makeDeps({ onCallEnd });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-close');

      await handler.handleWsClose();
      await handler.handleWsClose(); // second call should be no-op
      expect(onCallEnd).toHaveBeenCalledTimes(1);
    });
  });

  // --- setStreamSid ---

  describe('setStreamSid()', () => {
    it('sets the stream SID for Twilio media events', async () => {
      const bridge = makeMockBridge();
      const mockAdapter = makeMockAdapter();
      // When an adapter event fires 'audio', it uses bridge.sendAudio with streamSid
      mockAdapter.onEvent.mockImplementation(async (cb: (type: string, data: unknown) => Promise<void>) => {
        await cb('audio', Buffer.from('test'));
      });
      const deps = makeDeps({
        bridge,
        buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      handler.setStreamSid('stream-abc');
      await handler.handleCallStart('call-sid');

      // After adapter event fires 'audio', sendAudio should use the stream SID
      expect(bridge.sendAudio).toHaveBeenCalledWith(
        ws,
        expect.any(String),
        'stream-abc',
      );
    });
  });

  // --- TelephonyBridge interface abstraction ---

  describe('TelephonyBridge abstraction', () => {
    it('uses bridge.sendClear on interruption event', async () => {
      const bridge = makeMockBridge();
      const mockAdapter = makeMockAdapter();
      mockAdapter.onEvent.mockImplementation(async (cb: (type: string, data: unknown) => Promise<void>) => {
        await cb('speech_started', null);
      });
      const deps = makeDeps({
        bridge,
        buildAIAdapter: vi.fn().mockReturnValue(mockAdapter),
      });
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      await handler.handleCallStart('call-int');

      expect(bridge.sendClear).toHaveBeenCalled();
    });

    it('uses bridge label for logging context', () => {
      const bridge = makeMockBridge({ label: 'CustomBridge' });
      const deps = makeDeps({ bridge });
      const ws = makeMockWs();
      // Just verify construction doesn't throw with custom bridge
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      expect(handler).toBeDefined();
    });
  });

  // -------------------------------------------------------------------------
  // Fix #35 — Barge-in cancels in-flight LLM stream
  //
  // Pre-fix: ``cancelSpeaking`` flipped ``isSpeaking=false`` and cleared
  // downstream audio, but the ``for await`` loop kept consuming LLM tokens
  // we would never speak — wasted cost and held the provider connection.
  //
  // Post-fix: ``cancelSpeaking`` aborts an ``AbortController`` whose
  // ``signal`` is checked between tokens, breaking the loop early.
  // -------------------------------------------------------------------------
  describe('barge-in cancels in-flight LLM stream', () => {
    it('aborts the AbortController on cancelSpeaking', () => {
      const deps = makeDeps();
      const ws = makeMockWs();
      const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');
      // Simulate that runPipelineLlm has set up an AbortController for the turn.
      const controller = new AbortController();
      // Use bracket access to reach the private field for test purposes only.
      (handler as unknown as { llmAbort: AbortController | null }).llmAbort =
        controller;
      (handler as unknown as { isSpeaking: boolean }).isSpeaking = true;

      expect(controller.signal.aborted).toBe(false);
      // cancelSpeaking is private — invoke the public surface that calls it.
      (handler as unknown as { cancelSpeaking: () => void }).cancelSpeaking();
      expect(controller.signal.aborted).toBe(true);
    });

    it('signal.aborted bounds tokens consumed below tokens emitted', async () => {
      // Sentinel async-gen yielding a known number of tokens; if we don't
      // honour the abort signal we would drain the entire generator.
      async function* tokens() {
        for (let i = 0; i < 50; i++) {
          yield `tok${i}`;
          // Yield to the microtask queue so other code can run between tokens.
          await Promise.resolve();
        }
      }

      const ctrl = new AbortController();
      const consumed: string[] = [];
      for await (const t of tokens()) {
        if (ctrl.signal.aborted) break;
        consumed.push(t);
        if (consumed.length === 3) ctrl.abort();
      }
      expect(consumed.length).toBeLessThan(50);
      expect(consumed.length).toBeLessThanOrEqual(4);
    });
  });

  // -------------------------------------------------------------------------
  // canBargeIn() — adaptive gate on minimum speaking duration. With AEC on
  // it covers the filter's ~1 s warmup window; with AEC off it is just a
  // 250 ms anti-flicker margin so PSTN barge-in stays responsive.
  // -------------------------------------------------------------------------
  describe('barge-in gate (adaptive: AEC on vs off)', () => {
    function priv(h: StreamHandler) {
      return h as unknown as {
        isSpeaking: boolean;
        speakingStartedAt: number | null;
        firstAudioSentAt: number | null;
        aec: unknown;
        canBargeIn: () => boolean;
        handleBargeIn: (t: { text?: string }) => boolean;
        llmAbort: AbortController | null;
        cancelSpeaking: () => void;
      };
    }

    it('canBargeIn() returns true when no turn is active', () => {
      const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
      const p = priv(h);
      p.speakingStartedAt = null;
      expect(p.canBargeIn()).toBe(true);
    });

    it('canBargeIn() false before the first TTS chunk has hit the wire', () => {
      // 0.6.2 fix: ElevenLabs first-byte latency is hundreds of ms. Pre-fix
      // a 250 ms gate measured from beginSpeaking expired before any audio
      // went out, letting background noise self-cancel the agent's first
      // turn. Post-fix the gate is anchored on firstAudioSentAt — if that's
      // null we are still waiting for the TTS provider's first byte.
      const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
      const p = priv(h);
      p.aec = null;
      p.speakingStartedAt = Date.now() - 5000; // long past the 250 ms gate
      p.firstAudioSentAt = null; // but no audio has gone out yet
      expect(p.canBargeIn()).toBe(false);
    });

    // -----------------------------------------------------------------------
    // AEC OFF (default — PSTN deployments). Gate is 500 ms (raised 100 →
    // 500 on 2026-05-19 after the 0.6.2 acceptance run showed phantom VAD
    // ``speech_start`` events firing within the first ~250 ms of the
    // prewarmed firstMessage and cancelling it).
    // -----------------------------------------------------------------------
    describe('AEC off (PSTN default)', () => {
      it('canBargeIn() false within 500 ms anti-flicker window', () => {
        const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
        const p = priv(h);
        p.aec = null;
        p.speakingStartedAt = Date.now() - 250;
        p.firstAudioSentAt = Date.now() - 250; // 250 ms — still inside 500 ms gate
        expect(p.canBargeIn()).toBe(false);
      });

      it('canBargeIn() true past 500 ms (well below the 1 s AEC gate)', () => {
        const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
        const p = priv(h);
        p.aec = null;
        p.speakingStartedAt = Date.now() - 700;
        p.firstAudioSentAt = Date.now() - 700; // 700 ms — past 500 ms gate, under 1 s
        expect(p.canBargeIn()).toBe(true);
      });

      it('handleBargeIn fires after 600 ms with AEC off (the bug fix)', () => {
        // Pre-fix this would have been suppressed by the hardcoded 1 s gate.
        const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
        const p = priv(h);
        p.aec = null;
        p.isSpeaking = true;
        p.speakingStartedAt = Date.now() - 600;
        p.firstAudioSentAt = Date.now() - 600;
        const result = p.handleBargeIn({ text: 'stop' });
        expect(result).toBe(true);
        expect(p.isSpeaking).toBe(false);
      });
    });

    // -----------------------------------------------------------------------
    // AEC ON (browser / native). Gate is 1000 ms — covers filter warmup.
    // -----------------------------------------------------------------------
    describe('AEC on (browser/native)', () => {
      // Sentinel object — canBargeIn only checks ``aec !== null``,
      // so any non-null value selects the AEC gate.
      const aecSentinel = { tag: 'aec' } as unknown;

      it('canBargeIn() false within the 1 s warmup window', () => {
        const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
        const p = priv(h);
        p.aec = aecSentinel;
        p.speakingStartedAt = Date.now() - 400; // would PASS with AEC off
        p.firstAudioSentAt = Date.now() - 400;
        expect(p.canBargeIn()).toBe(false);
      });

      it('canBargeIn() true past 1 s', () => {
        const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
        const p = priv(h);
        p.aec = aecSentinel;
        p.speakingStartedAt = Date.now() - 1200;
        p.firstAudioSentAt = Date.now() - 1200;
        expect(p.canBargeIn()).toBe(true);
      });

      it('handleBargeIn suppressed at 400 ms with AEC on', () => {
        const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
        const p = priv(h);
        p.aec = aecSentinel;
        p.isSpeaking = true;
        p.speakingStartedAt = Date.now() - 400;
        p.firstAudioSentAt = Date.now() - 400;
        const result = p.handleBargeIn({ text: 'stop' });
        expect(result).toBe(false);
        expect(p.isSpeaking).toBe(true);
      });

      it('handleBargeIn fires past 1 s with AEC on', () => {
        const h = new StreamHandler(makeDeps(), makeMockWs(), '+15551111111', '+15552222222');
        const p = priv(h);
        p.aec = aecSentinel;
        p.isSpeaking = true;
        p.speakingStartedAt = Date.now() - 1500;
        p.firstAudioSentAt = Date.now() - 1500;
        const result = p.handleBargeIn({ text: 'stop' });
        expect(result).toBe(true);
        expect(p.isSpeaking).toBe(false);
      });
    });
  });

  // -------------------------------------------------------------------------
  // firstMessage mark-gated pacing — BUG #128 regression coverage.
  //
  // Pre-fix the firstMessage TTS chunks were pushed into the carrier
  // WebSocket as fast as the TTS provider yielded them. A barge-in
  // mid-buffer issued ``sendClear``, but the WebSocket queue between the
  // SDK and Twilio's edge held several seconds of media frames already,
  // and the agent kept talking on the user's earpiece until that drained.
  //
  // Post-fix every chunk is followed by a mark; the loop awaits the
  // oldest mark before sending more once ``FIRST_MESSAGE_MARK_WINDOW``
  // chunks are unconfirmed. ``cancelSpeaking`` drains every pending mark
  // so the waiting loop exits on the next tick.
  // -------------------------------------------------------------------------
  // SKIPPED 2026-05-22: mark-gated per-chunk pacing was replaced with a
  // burst-deliver model in commit 5574997
  // (``fix(prewarm): burst-deliver prewarmed first-message bytes, drop the
  // slow per-chunk sleep``). ``sendPacedFirstMessageBytes`` /
  // ``firstMessageMarkCounter`` / ``sendMarkAwaitable`` no longer exist as
  // public surface. Left as ``describe.skip`` to preserve the historical
  // intent — the regression these tests pinned (audio buffered past
  // barge-in on the WS edge) is now covered by the burst-deliver path's
  // own ``cancelActiveStream`` plumbing.
  describe.skip('firstMessage mark-gated pacing', () => {
    interface FmPriv {
      isSpeaking: boolean;
      speakingStartedAt: number | null;
      firstAudioSentAt: number | null;
      aec: unknown;
      streamSid: string;
      pendingMarks: Array<{ name: string; resolve: () => void; promise: Promise<void> }>;
      firstMessageMarkCounter: number;
      sendPacedFirstMessageBytes: (b: Buffer) => Promise<boolean>;
      onMark: (n: string) => Promise<void>;
      runBargeInCancel: (t: string) => void;
    }

    function fmPriv(h: StreamHandler): FmPriv {
      return h as unknown as FmPriv;
    }

    function primeForFirstMessage(h: StreamHandler): FmPriv {
      const p = fmPriv(h);
      p.isSpeaking = true;
      p.speakingStartedAt = Date.now() - 5000;
      p.firstAudioSentAt = Date.now() - 5000;
      p.aec = null;
      p.streamSid = 'MZtest';
      return p;
    }

    async function flushMicrotasks(count = 10): Promise<void> {
      for (let i = 0; i < count; i++) await Promise.resolve();
    }

    const CHUNK_BYTES = 1280; // matches StreamHandler.PREWARM_CHUNK_BYTES
    // 1280 bytes / 32 bytes-per-ms = 40 ms of PCM16 16kHz audio per chunk.
    const PLAYOUT_MS = CHUNK_BYTES / 32;

    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('caps in-flight chunks at FIRST_MESSAGE_MARK_WINDOW and bails on barge-in', async () => {
      const sendAudio = vi.fn();
      const sendMark = vi.fn();
      const sendClear = vi.fn();
      const bridge = makeMockBridge({ sendAudio, sendMark, sendClear });
      const h = new StreamHandler(
        makeDeps({ bridge }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = primeForFirstMessage(h);
      // 4 chunks. Window=3, so chunks 1–3 send after their 40ms sleeps and
      // chunk 4 blocks on waitForMarkWindow until either a mark echoes OR
      // cancelSpeaking drains the queue.
      const bytes = Buffer.alloc(CHUNK_BYTES * 4, 0);
      const sendPromise = p.sendPacedFirstMessageBytes(bytes);

      // Chunks 1–2 go out without sleep (initial burst to pre-fill the PSTN
      // jitter buffer). Chunk 3 triggers the first fill of the mark window
      // and its 40ms playout sleep. Advancing by PLAYOUT_MS fires that sleep
      // and the loop blocks at waitForMarkWindow for chunk 4.
      await vi.advanceTimersByTimeAsync(PLAYOUT_MS);
      expect(sendAudio).toHaveBeenCalledTimes(3);
      expect(sendMark).toHaveBeenCalledTimes(3);
      expect(p.pendingMarks.length).toBe(3);

      // Simulate a confirmed barge-in: runBargeInCancel calls sendClear +
      // cancelSpeaking, and cancelSpeaking drains pendingMarks so the
      // sliding-window wait exits on the next tick.
      p.runBargeInCancel('the user spoke');
      await sendPromise;

      expect(sendClear).toHaveBeenCalledTimes(1);
      expect(p.isSpeaking).toBe(false);
      // Chunk 4 must NOT have hit the wire.
      expect(sendAudio).toHaveBeenCalledTimes(3);
    });

    it('echoed mark slides the window and the next chunk goes out', async () => {
      const sendAudio = vi.fn();
      const sendMark = vi.fn();
      const bridge = makeMockBridge({ sendAudio, sendMark });
      const h = new StreamHandler(
        makeDeps({ bridge }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = primeForFirstMessage(h);
      const bytes = Buffer.alloc(CHUNK_BYTES * 4, 0);
      const sendPromise = p.sendPacedFirstMessageBytes(bytes);

      // Chunks 1–2 burst (no sleep). Chunk 3 fills the window → 40ms sleep.
      await vi.advanceTimersByTimeAsync(PLAYOUT_MS);
      // Three chunks in flight, one waiting on the window.
      expect(sendAudio).toHaveBeenCalledTimes(3);
      expect(sendMark).toHaveBeenCalledTimes(3);

      // Twilio echoes the FIRST chunk's mark — the loop should advance.
      await p.onMark('fm_1');
      // Flush microtasks so waitForMarkWindow exits and chunk 4 sends.
      await flushMicrotasks();
      // Advance 1 × 40ms for chunk 4's playout sleep.
      await vi.advanceTimersByTimeAsync(PLAYOUT_MS);

      expect(sendAudio).toHaveBeenCalledTimes(4);
      expect(sendMark).toHaveBeenCalledTimes(4);
      // Let the remaining marks "play" so the loop returns.
      await p.onMark('fm_2');
      await p.onMark('fm_3');
      await p.onMark('fm_4');
      await sendPromise;
      expect(p.pendingMarks.length).toBe(0);
    });

    it('Telnyx (no marks): paces via playout-time and bails on cancelSpeaking', async () => {
      const sendAudio = vi.fn();
      const sendMark = vi.fn();
      const sendClear = vi.fn();
      const bridge = makeMockBridge({
        telephonyProvider: 'telnyx',
        sendAudio,
        sendMark,
        sendClear,
      });
      const h = new StreamHandler(
        makeDeps({ bridge }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = primeForFirstMessage(h);
      // 4 chunks. Each iteration awaits a fake setTimeout (40 ms), so the loop
      // emits the first chunk and suspends on the fake clock.
      const bytes = Buffer.alloc(CHUNK_BYTES * 4, 0);
      const sendPromise = p.sendPacedFirstMessageBytes(bytes);

      // Flush microtasks to let chunk 1 send and hit the fake 40ms timer.
      await flushMicrotasks();
      // Telnyx never sends marks — the queue stays empty even mid-loop.
      expect(sendMark).not.toHaveBeenCalled();
      expect(p.pendingMarks.length).toBe(0);
      // At least the first chunk should have hit the wire by the time
      // we trip the cancel.
      const sentBeforeCancel = sendAudio.mock.calls.length;
      expect(sentBeforeCancel).toBeGreaterThanOrEqual(1);

      p.runBargeInCancel('user spoke');
      // Fire the pending 40ms sleep so the loop can observe isSpeaking=false.
      await vi.runAllTimersAsync();
      await sendPromise;

      expect(sendClear).toHaveBeenCalledTimes(1);
      expect(p.isSpeaking).toBe(false);
      // After cancel no further chunks may go out.
      expect(sendAudio).toHaveBeenCalledTimes(sentBeforeCancel);
    });
  });

  // SKIPPED 2026-05-22: see note on the ``firstMessage mark-gated pacing``
  // block above — burst-deliver replaced the mark-window plumbing; pending
  // marks no longer exist to drain.
  describe.skip('cleanup drains pending firstMessage marks', () => {
    interface CleanupPriv {
      isSpeaking: boolean;
      speakingStartedAt: number | null;
      firstAudioSentAt: number | null;
      aec: unknown;
      streamSid: string;
      pendingMarks: Array<{ name: string; resolve: () => void; promise: Promise<void> }>;
      firstMessageMarkCounter: number;
      sendMarkAwaitable: () => Promise<void> | null;
    }

    function priv(h: StreamHandler): CleanupPriv {
      return h as unknown as CleanupPriv;
    }

    function primeForFirstMessage(h: StreamHandler): CleanupPriv {
      const p = priv(h);
      p.isSpeaking = true;
      p.speakingStartedAt = Date.now() - 5000;
      p.firstAudioSentAt = Date.now() - 5000;
      p.aec = null;
      p.streamSid = 'MZtest';
      return p;
    }

    it('handleStop resolves every pending mark', async () => {
      const sendMark = vi.fn();
      const bridge = makeMockBridge({ sendMark });
      const h = new StreamHandler(
        makeDeps({ bridge }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = primeForFirstMessage(h);

      // Queue three marks via the public send path then simulate an
      // abnormal stop mid firstMessage. Capture each promise so we
      // can assert they all resolve after handleStop.
      const m1 = p.sendMarkAwaitable();
      const m2 = p.sendMarkAwaitable();
      const m3 = p.sendMarkAwaitable();
      expect(p.pendingMarks.length).toBe(3);

      await h.handleStop();

      expect(p.pendingMarks.length).toBe(0);
      // Every captured promise resolved (await would hang otherwise).
      await Promise.all([m1, m2, m3]);
    });

    it('handleWsClose resolves every pending mark', async () => {
      const sendMark = vi.fn();
      const bridge = makeMockBridge({ sendMark });
      const h = new StreamHandler(
        makeDeps({ bridge }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = primeForFirstMessage(h);

      const m1 = p.sendMarkAwaitable();
      const m2 = p.sendMarkAwaitable();
      const m3 = p.sendMarkAwaitable();
      expect(p.pendingMarks.length).toBe(3);

      await h.handleWsClose();

      expect(p.pendingMarks.length).toBe(0);
      await Promise.all([m1, m2, m3]);
    });
  });

  // SKIPPED 2026-05-22: see note on the ``firstMessage mark-gated pacing``
  // block above — burst-deliver replaced the mark-counter plumbing;
  // ``firstMessageMarkCounter`` no longer exists.
  describe.skip('firstMessage mark counter resets across sends + on cleanup', () => {
    interface CounterPriv {
      isSpeaking: boolean;
      speakingStartedAt: number | null;
      firstAudioSentAt: number | null;
      aec: unknown;
      streamSid: string;
      pendingMarks: Array<{ name: string; resolve: () => void; promise: Promise<void> }>;
      firstMessageMarkCounter: number;
      sendPacedFirstMessageBytes: (b: Buffer) => Promise<boolean>;
      onMark: (n: string) => Promise<void>;
    }

    function priv(h: StreamHandler): CounterPriv {
      return h as unknown as CounterPriv;
    }

    function primeForFirstMessage(h: StreamHandler): CounterPriv {
      const p = priv(h);
      p.isSpeaking = true;
      p.speakingStartedAt = Date.now() - 5000;
      p.firstAudioSentAt = Date.now() - 5000;
      p.aec = null;
      p.streamSid = 'MZtest';
      return p;
    }

    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('sendPacedFirstMessageBytes resets counter between consecutive sends', async () => {
      const sendAudio = vi.fn();
      const sendMark = vi.fn();
      const bridge = makeMockBridge({ sendAudio, sendMark });
      const h = new StreamHandler(
        makeDeps({ bridge }),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = primeForFirstMessage(h);

      // CHUNK_BYTES = 1280 matches StreamHandler.PREWARM_CHUNK_BYTES.
      // Two chunks stay below FIRST_MESSAGE_MARK_WINDOW (3) so initialFillComplete
      // never flips to true and neither chunk triggers a playout sleep on Twilio.
      // advanceTimersByTimeAsync flushes microtasks first so both chunks are sent
      // synchronously before any time advance occurs.
      const CHUNK_BYTES = 1280;
      const PLAYOUT_MS = CHUNK_BYTES / 32; // 40ms (not used on Twilio here, kept for Telnyx parity)
      const bytes = Buffer.alloc(CHUNK_BYTES * 2, 0);

      const send1 = p.sendPacedFirstMessageBytes(bytes);
      // Flush microtasks (both chunks go out without sleep) and advance time
      // to drain any stray timers; marks are still pending — echo them.
      await vi.advanceTimersByTimeAsync(2 * PLAYOUT_MS);
      await p.onMark('fm_1');
      await p.onMark('fm_2');
      await send1;
      expect(p.firstMessageMarkCounter).toBe(2);
      expect(p.pendingMarks.length).toBe(0);
      const markCallsAfterFirst = sendMark.mock.calls.length;
      expect(
        sendMark.mock.calls.slice(0, markCallsAfterFirst).map((c) => c[1] as string),
      ).toEqual(['fm_1', 'fm_2']);

      // Second send: counter must reset to 0 at the top of the loop,
      // so the new sequence is fm_1, fm_2 — NOT fm_3, fm_4.
      const send2 = p.sendPacedFirstMessageBytes(bytes);
      await vi.advanceTimersByTimeAsync(2 * PLAYOUT_MS);
      const newMarks = sendMark.mock.calls
        .slice(markCallsAfterFirst)
        .map((c) => c[1] as string);
      expect(newMarks).toEqual(['fm_1', 'fm_2']);
      expect(p.firstMessageMarkCounter).toBe(2);

      await p.onMark('fm_1');
      await p.onMark('fm_2');
      await send2;
    });

    it('handleStop resets firstMessageMarkCounter', async () => {
      const h = new StreamHandler(
        makeDeps(),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = priv(h);
      // Pretend a prior turn left the counter at 7.
      p.firstMessageMarkCounter = 7;

      await h.handleStop();

      expect(p.firstMessageMarkCounter).toBe(0);
    });

    it('handleWsClose resets firstMessageMarkCounter', async () => {
      const h = new StreamHandler(
        makeDeps(),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = priv(h);
      p.firstMessageMarkCounter = 7;

      await h.handleWsClose();

      expect(p.firstMessageMarkCounter).toBe(0);
    });
  });

  describe('onMark only updates lastConfirmedMark on a matched mark', () => {
    interface OnMarkPriv {
      pendingMarks: Array<{ name: string; resolve: () => void; promise: Promise<void> }>;
      lastConfirmedMark: string;
    }

    it('does not overwrite lastConfirmedMark for an unknown mark name', async () => {
      const h = new StreamHandler(
        makeDeps(),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = h as unknown as OnMarkPriv;

      // Seed a real matched mark so lastConfirmedMark has a known
      // baseline that the unmatched echo must not overwrite.
      let resolveSeed!: () => void;
      const seedPromise = new Promise<void>((r) => {
        resolveSeed = r;
      });
      p.pendingMarks.push({ name: 'fm_seed', resolve: resolveSeed, promise: seedPromise });
      await h.onMark('fm_seed');
      expect(p.lastConfirmedMark).toBe('fm_seed');

      // Emit a mark name that is NOT in pendingMarks — e.g. echo
      // arrived after drain, or for an unknown identifier. The
      // handler's lastConfirmedMark must NOT be clobbered.
      await h.onMark('unknown_xyz');
      expect(p.lastConfirmedMark).toBe('fm_seed');
    });

    it('updates lastConfirmedMark only after the queue match succeeds', async () => {
      const h = new StreamHandler(
        makeDeps(),
        makeMockWs(),
        '+15551111111',
        '+15552222222',
      );
      const p = h as unknown as OnMarkPriv;
      expect(p.lastConfirmedMark).toBe('');

      let resolveA!: () => void;
      const promiseA = new Promise<void>((r) => {
        resolveA = r;
      });
      p.pendingMarks.push({ name: 'fm_1', resolve: resolveA, promise: promiseA });

      await h.onMark('fm_1');
      expect(p.lastConfirmedMark).toBe('fm_1');
      expect(p.pendingMarks.length).toBe(0);
    });
  });
});

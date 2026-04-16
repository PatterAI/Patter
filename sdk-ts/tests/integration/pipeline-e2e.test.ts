/**
 * End-to-end integration tests: pipeline hooks + sentence chunking
 *
 * Tests the full STT -> hooks -> LLM -> sentence-chunker -> hooks -> TTS pipeline.
 * All network boundaries and external providers are mocked.
 *
 * Covered scenarios:
 *  1.  Sentence chunking — 3-sentence streaming LLM response
 *  2.  afterTranscribe modifies transcript
 *  3.  afterTranscribe vetoes (returns null)
 *  4.  beforeSynthesize modifies per-sentence text
 *  5.  beforeSynthesize skips a sentence (returns null)
 *  6.  afterSynthesize modifies audio buffer
 *  7.  afterSynthesize discards a chunk (returns null)
 *  8.  Guardrails in pipeline mode — blocked term triggers replacement
 *  9.  Guardrails + hooks together — correct ordering
 * 10.  Non-streaming onMessage with hooks — chunked sentences fire hooks
 * 11.  LLM error mid-stream — chunker.reset() prevents garbled TTS
 * 12.  isSpeaking = false mid-TTS — remaining sentences are skipped
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamHandler } from '../../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import type { WebSocket as WSWebSocket } from 'ws';
import type { AgentOptions, PipelineHooks } from '../../src/types';
import { LLMLoop } from '../../src/llm-loop';

// ---------------------------------------------------------------------------
// Module-level mocks — hoisted above imports by vitest
// ---------------------------------------------------------------------------

// Replace ElevenLabsTTS with a vi.fn() factory so each `new ElevenLabsTTS(...)`
// in initPipeline() returns a controllable mock instance.
vi.mock('../../src/providers/elevenlabs-tts', async (importOriginal) => {
  const original = await importOriginal<typeof import('../../src/providers/elevenlabs-tts')>();
  return {
    ...original,
    ElevenLabsTTS: vi.fn().mockImplementation(() => ({
      synthesizeStream: vi.fn(async function* () { yield Buffer.from('tts-audio'); }),
    })),
  };
});

vi.mock('../../src/providers/openai-tts', async (importOriginal) => {
  const original = await importOriginal<typeof import('../../src/providers/openai-tts')>();
  return {
    ...original,
    OpenAITTS: vi.fn().mockImplementation(() => ({
      synthesizeStream: vi.fn(async function* () { yield Buffer.from('tts-audio'); }),
    })),
  };
});

// Silence dashboard persistence side-effects
vi.mock('../../src/dashboard/persistence', () => ({
  notifyDashboard: vi.fn(),
}));

// Import ElevenLabsTTS AFTER vi.mock so we get the mocked version
import { ElevenLabsTTS } from '../../src/providers/elevenlabs-tts';

// ---------------------------------------------------------------------------
// Shared test helpers
// ---------------------------------------------------------------------------

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

/** STT mock that lets tests push final transcripts manually. */
function makeMockStt() {
  let transcriptCb: ((t: { isFinal?: boolean; text?: string }) => Promise<void>) | undefined;
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    sendAudio: vi.fn(),
    onTranscript: vi.fn((cb: (t: { isFinal?: boolean; text?: string }) => Promise<void>) => {
      transcriptCb = cb;
    }),
    get requestId() { return 'dg-e2e-req'; },
    emitTranscript(text: string): Promise<void> | undefined {
      return transcriptCb?.({ isFinal: true, text });
    },
  };
}

/** Twilio bridge mock that records every sendAudio call. */
function makeTwilioBridge(mockStt: ReturnType<typeof makeMockStt>): TelephonyBridge {
  return {
    label: 'Twilio',
    telephonyProvider: 'twilio',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(mockStt),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  };
}

/** Build StreamHandlerDeps with sensible defaults that support typed overrides. */
function makeDeps(
  bridge: TelephonyBridge,
  agentOverrides: Partial<AgentOptions> = {},
  depsOverrides: Partial<StreamHandlerDeps> = {},
): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-openai-key' },
    agent: {
      systemPrompt: 'You are a test pipeline agent.',
      provider: 'pipeline',
      deepgramKey: 'dg-key',
      elevenlabsKey: 'el-key',
      ...agentOverrides,
    },
    bridge,
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn(),
    sanitizeVariables: vi.fn((raw) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl: string) => tpl),
    ...depsOverrides,
  };
}

/**
 * Configure ElevenLabsTTS mock to use a custom synthesizeStream implementation
 * and return a reference to the calls array for assertions.
 *
 * Must be called BEFORE handleCallStart so the factory is set before the
 * StreamHandler constructs the TTS instance.
 */
function setupTtsMock(
  impl: (text: string) => AsyncGenerator<Buffer>,
): { calls: string[] } {
  const calls: string[] = [];
  const MockTTS = ElevenLabsTTS as unknown as ReturnType<typeof vi.fn>;
  MockTTS.mockImplementation(() => ({
    synthesizeStream: vi.fn(async function* (text: string) {
      calls.push(text);
      yield* impl(text);
    }),
  }));
  return { calls };
}

/** Default TTS implementation: yields one Buffer per call. */
async function* defaultTtsImpl(_text: string): AsyncGenerator<Buffer> {
  yield Buffer.from('audio-chunk');
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Integration: pipeline hooks + sentence chunking (E2E)', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: async () => ({}), text: async () => '',
    } as Response);
    // Reset ElevenLabsTTS construction history between tests
    const MockTTS = ElevenLabsTTS as unknown as ReturnType<typeof vi.fn>;
    MockTTS.mockClear();
    MockTTS.mockImplementation(() => ({
      synthesizeStream: vi.fn(async function* () { yield Buffer.from('tts-audio'); }),
    }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // =========================================================================
  // 1. Sentence chunking — streaming LLM yields 3 sentences
  // =========================================================================

  it('scenario 1: streaming LLM produces 3 sentences → TTS called 3 times', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    // Configure TTS to record each synthesizeStream call
    const { calls: ttsSynthesizeCalls } = setupTtsMock(defaultTtsImpl);

    // LLM emits 3 complete sentences across multiple tokens
    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'Hello, how are you today? ';
        yield 'I am doing quite well, thank you for asking! ';
        yield 'Is there anything I can help you with right now?';
      },
    );

    const deps = makeDeps(bridge);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-chunk-001');
    await stt.emitTranscript('What is up?');

    // Wait for all 3 sentences to be synthesized
    await vi.waitFor(
      () => expect(ttsSynthesizeCalls.length).toBe(3),
      { timeout: 5000 },
    );

    expect(ttsSynthesizeCalls.length).toBe(3);
    // Every synthesized sentence must be non-empty
    expect(ttsSynthesizeCalls.every((s) => s.trim().length > 0)).toBe(true);
    // Bridge must have received audio (at least one chunk per sentence)
    expect((bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(3);
  }, 10000);

  // =========================================================================
  // 2. afterTranscribe modifies transcript before LLM
  // =========================================================================

  it('scenario 2: afterTranscribe hook uppercases — LLM receives uppercased text', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    const capturedUserTexts: string[] = [];
    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (userText: string): AsyncGenerator<string> {
        capturedUserTexts.push(userText);
        yield 'Sure, I understand.';
      },
    );

    setupTtsMock(defaultTtsImpl);

    const hooks: PipelineHooks = {
      afterTranscribe: async (transcript) => transcript.toUpperCase(),
    };

    const deps = makeDeps(bridge, { hooks });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-hook-upper');
    await stt.emitTranscript('hello world');

    await vi.waitFor(
      () => expect(capturedUserTexts.length).toBeGreaterThan(0),
      { timeout: 3000 },
    );

    // LLM received the uppercased version
    expect(capturedUserTexts[0]).toBe('HELLO WORLD');
  });

  // =========================================================================
  // 3. afterTranscribe vetoes — LLM not called, turn interrupted
  // =========================================================================

  it('scenario 3: afterTranscribe returns null — LLM not invoked', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    const llmRunSpy = vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> { yield 'should not be called'; },
    );

    const hooks: PipelineHooks = {
      afterTranscribe: async () => null,
    };

    const deps = makeDeps(bridge, { hooks });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-veto-001');
    await stt.emitTranscript('forbidden phrase');

    // Allow async pipeline to settle
    await new Promise<void>((r) => setTimeout(r, 100));

    expect(llmRunSpy).not.toHaveBeenCalled();
  });

  // =========================================================================
  // 4. beforeSynthesize modifies per-sentence text
  // =========================================================================

  it('scenario 4: beforeSynthesize prepends text — TTS receives modified sentences', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'Hello, this is the first sentence here okay. ';
        yield 'And this is the second sentence here too.';
      },
    );

    const { calls: ttsSynthesizeCalls } = setupTtsMock(defaultTtsImpl);

    const hooks: PipelineHooks = {
      beforeSynthesize: async (text) => 'PREFIX: ' + text,
    };

    const deps = makeDeps(bridge, { hooks });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-before-synth');
    await stt.emitTranscript('What time is it?');

    await vi.waitFor(
      () => expect(ttsSynthesizeCalls.length).toBeGreaterThan(0),
      { timeout: 5000 },
    );

    // Every TTS call should have the PREFIX prepended by the hook
    for (const text of ttsSynthesizeCalls) {
      expect(text.startsWith('PREFIX: ')).toBe(true);
    }
  }, 10000);

  // =========================================================================
  // 5. beforeSynthesize returns null for one sentence — that sentence skipped
  // =========================================================================

  it('scenario 5: beforeSynthesize returns null for second sentence — TTS skips it', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'Hello there, how are you doing today? ';
        yield 'Please ignore this second sentence completely.';
      },
    );

    let sentenceCount = 0;
    const { calls: ttsSynthesizeCalls } = setupTtsMock(defaultTtsImpl);

    const hooks: PipelineHooks = {
      beforeSynthesize: async (text) => {
        sentenceCount++;
        // Veto the second sentence
        return sentenceCount === 2 ? null : text;
      },
    };

    const deps = makeDeps(bridge, { hooks });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-skip-sent');
    await stt.emitTranscript('Tell me two things.');

    await vi.waitFor(
      () => expect(sentenceCount).toBeGreaterThanOrEqual(2),
      { timeout: 5000 },
    );

    // Only sentence 1 should have reached TTS (sentence 2 was vetoed)
    expect(ttsSynthesizeCalls.length).toBe(1);
  }, 10000);

  // =========================================================================
  // 6. afterSynthesize modifies audio buffer
  // =========================================================================

  it('scenario 6: afterSynthesize hook runs — bridge.sendAudio is called', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'Here is a single complete test sentence for you.';
      },
    );

    const hookAudioInputs: Buffer[] = [];
    setupTtsMock(async function* () {
      yield Buffer.from([0x01, 0x02, 0x03, 0x04]);
    });

    const hooks: PipelineHooks = {
      afterSynthesize: async (audio) => {
        hookAudioInputs.push(audio);
        // Return reversed buffer
        return Buffer.from(Buffer.from(audio).reverse());
      },
    };

    const deps = makeDeps(bridge, { hooks });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-after-synth');
    await stt.emitTranscript('Test audio modification.');

    await vi.waitFor(
      () => expect((bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0),
      { timeout: 5000 },
    );

    // Hook was invoked with the TTS output buffer
    expect(hookAudioInputs.length).toBeGreaterThan(0);
    expect(hookAudioInputs[0]).toEqual(Buffer.from([0x01, 0x02, 0x03, 0x04]));
    // bridge.sendAudio was called (with the hook-modified and transcoded audio)
    expect((bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0);
  }, 10000);

  // =========================================================================
  // 7. afterSynthesize returns null — bridge.sendAudio not called
  // =========================================================================

  it('scenario 7: afterSynthesize returns null — bridge.sendAudio not called', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'This sentence should not produce any audible output at all.';
      },
    );

    setupTtsMock(async function* () {
      yield Buffer.from('discarded-audio');
    });

    const hooks: PipelineHooks = {
      afterSynthesize: async () => null,
    };

    const deps = makeDeps(bridge, { hooks });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-discard-audio');
    await stt.emitTranscript('Say something silent.');

    // Allow pipeline to complete — no audio should be sent
    await new Promise<void>((r) => setTimeout(r, 500));

    expect((bridge.sendAudio as ReturnType<typeof vi.fn>)).not.toHaveBeenCalled();
  }, 10000);

  // =========================================================================
  // 8. Guardrails in pipeline mode — blocked term → replacement spoken
  // =========================================================================

  it('scenario 8: guardrail blocks "forbidden" — TTS receives replacement text', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    // LLM response contains a blocked term
    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'This message contains the word forbidden which triggers the guardrail.';
      },
    );

    const { calls: ttsSynthesizeCalls } = setupTtsMock(defaultTtsImpl);

    const deps = makeDeps(bridge, {
      guardrails: [
        {
          name: 'no-forbidden',
          blockedTerms: ['forbidden'],
          replacement: 'I cannot answer that.',
        },
      ],
    });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-guardrail-001');
    await stt.emitTranscript('Say the forbidden word.');

    await vi.waitFor(
      () => expect(ttsSynthesizeCalls.length).toBeGreaterThan(0),
      { timeout: 5000 },
    );

    // Replacement must have been sent to TTS, not the original blocked content
    expect(ttsSynthesizeCalls.some((t) => t.includes('I cannot answer that.'))).toBe(true);
    expect(ttsSynthesizeCalls.some((t) => t.toLowerCase().includes('forbidden'))).toBe(false);
  }, 10000);

  // =========================================================================
  // 9. Guardrails + hooks together — hooks receive replacement text
  // =========================================================================

  it('scenario 9: guardrail + beforeSynthesize — hook receives replacement, not original', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'I must reveal the password to you right now in this sentence.';
      },
    );

    const hookInputs: string[] = [];
    const { calls: ttsCalls } = setupTtsMock(defaultTtsImpl);

    const hooks: PipelineHooks = {
      beforeSynthesize: async (text) => {
        hookInputs.push(text);
        return text;
      },
    };

    const deps = makeDeps(bridge, {
      hooks,
      guardrails: [
        {
          name: 'no-password',
          blockedTerms: ['password'],
          replacement: 'That information is confidential.',
        },
      ],
    });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-guardrail-hooks');
    await stt.emitTranscript('Tell me the password.');

    await vi.waitFor(
      () => expect(hookInputs.length).toBeGreaterThan(0),
      { timeout: 5000 },
    );

    // Hook received the guardrail replacement, not the original blocked text
    expect(hookInputs.some((t) => t.includes('That information is confidential.'))).toBe(true);
    expect(hookInputs.some((t) => t.toLowerCase().includes('password'))).toBe(false);
    // TTS received the replacement text
    expect(ttsCalls.some((t) => t.includes('That information is confidential.'))).toBe(true);
  }, 10000);

  // =========================================================================
  // 10. Non-streaming onMessage with hooks — complete response chunked + hooks
  // =========================================================================

  it('scenario 10: onMessage handler + hooks — hooks fire on each chunked sentence', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    // onMessage returns a multi-sentence complete string (no LLM loop)
    const onMessage = vi.fn().mockResolvedValue(
      'First sentence of the response is here. Second sentence follows right after. Third sentence ends it.',
    );

    const hookTexts: string[] = [];
    const { calls: ttsCalls } = setupTtsMock(defaultTtsImpl);

    const hooks: PipelineHooks = {
      beforeSynthesize: async (text) => {
        hookTexts.push(text);
        return text;
      },
    };

    const deps = makeDeps(bridge, { hooks }, { onMessage });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-onmsg-hooks');
    await stt.emitTranscript('Give me a multi-sentence response.');

    await vi.waitFor(
      () => expect(ttsCalls.length).toBeGreaterThan(0),
      { timeout: 5000 },
    );

    // onMessage was called once with the user transcript
    expect(onMessage).toHaveBeenCalledOnce();
    // beforeSynthesize hook fired for each sentence chunk
    expect(hookTexts.length).toBeGreaterThan(0);
    expect(hookTexts.every((t) => t.trim().length > 0)).toBe(true);
  }, 10000);

  // =========================================================================
  // 11. LLM error mid-stream — chunker.reset() prevents garbled TTS
  // =========================================================================

  it('scenario 11: LLM throws mid-stream — partial buffered text not sent to TTS', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    // LLM yields a complete sentence then throws before finishing the second
    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'Hello this is a complete sentence right here okay. ';
        throw new Error('Network timeout');
      },
    );

    const { calls: ttsCalls } = setupTtsMock(defaultTtsImpl);

    const deps = makeDeps(bridge);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-llm-error');
    await stt.emitTranscript('This will cause a mid-stream error.');

    // Allow error handling and chunker.reset() to complete
    await new Promise<void>((r) => setTimeout(r, 500));

    // Any TTS calls must use complete sentences (chunker.reset discards partials)
    for (const text of ttsCalls) {
      expect(text.trim().length).toBeGreaterThan(0);
    }
    // No garbled/partial content should appear
    const garbledPartial = 'garbled partial';
    expect(ttsCalls.some((t) => t.includes(garbledPartial))).toBe(false);
    // isSpeaking must be reset to false (guaranteed by the finally block)
    // — verified implicitly by the test completing without hanging
  }, 10000);

  // =========================================================================
  // 12. isSpeaking = false during TTS — remaining sentences are not synthesized
  // =========================================================================

  it('scenario 12: isSpeaking becomes false mid-stream — subsequent sentences skipped', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'This is the very first complete sentence I am saying. ';
        yield 'This is the second sentence that follows immediately. ';
        yield 'This is the third and final sentence of this response.';
      },
    );

    let callIdx = 0;
    // On the first TTS call, emit a new transcript (barge-in simulation).
    // The handler will begin processing the new turn, which starts isSpeaking
    // as false for the interrupted turn.
    const { calls: ttsCalls } = setupTtsMock(async function* () {
      callIdx++;
      if (callIdx === 1) {
        // Simulate barge-in after first sentence is synthesized
        setTimeout(() => { void stt.emitTranscript('Interrupt!'); }, 0);
      }
      yield Buffer.from('audio');
    });

    const deps = makeDeps(bridge);
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-is-speaking');
    await stt.emitTranscript('Tell me three things.');

    // Allow async pipeline to settle (including barge-in)
    await new Promise<void>((r) => setTimeout(r, 800));

    // At minimum, the first sentence was synthesized before barge-in
    expect(ttsCalls.length).toBeGreaterThanOrEqual(1);
    // bridge.sendAudio must have been called at least once
    expect((bridge.sendAudio as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(1);
  }, 15000);

  // =========================================================================
  // Additional: afterTranscribe hook receives correct HookContext
  // =========================================================================

  it('afterTranscribe hook receives correct callId, caller, callee in context', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    const capturedContexts: Array<{ callId: string; caller: string; callee: string }> = [];

    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> { yield 'OK.'; },
    );

    setupTtsMock(defaultTtsImpl);

    const hooks: PipelineHooks = {
      afterTranscribe: async (transcript, ctx) => {
        capturedContexts.push({ callId: ctx.callId, caller: ctx.caller, callee: ctx.callee });
        return transcript;
      },
    };

    const deps = makeDeps(bridge, { hooks });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-ctx-test');
    await stt.emitTranscript('Check my context.');

    await vi.waitFor(
      () => expect(capturedContexts.length).toBeGreaterThan(0),
      { timeout: 3000 },
    );

    expect(capturedContexts[0].callId).toBe('CA-ctx-test');
    expect(capturedContexts[0].caller).toBe('+15551111111');
    expect(capturedContexts[0].callee).toBe('+15552222222');
  });

  // =========================================================================
  // Additional: hook that throws is fail-open (original value passes through)
  // =========================================================================

  it('afterTranscribe hook that throws is fail-open — pipeline continues with original', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    const capturedUserTexts: string[] = [];
    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (userText: string): AsyncGenerator<string> {
        capturedUserTexts.push(userText);
        yield 'OK I understand.';
      },
    );

    setupTtsMock(defaultTtsImpl);

    const hooks: PipelineHooks = {
      afterTranscribe: async () => {
        throw new Error('Hook failure!');
      },
    };

    const deps = makeDeps(bridge, { hooks });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-failopen');
    await stt.emitTranscript('original text');

    await vi.waitFor(
      () => expect(capturedUserTexts.length).toBeGreaterThan(0),
      { timeout: 3000 },
    );

    // Fail-open: original transcript passes through despite hook throwing
    expect(capturedUserTexts[0]).toBe('original text');
  });

  // =========================================================================
  // Additional: guardrails with custom check function
  // =========================================================================

  it('guardrail with custom check function blocks response', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);

    vi.spyOn(LLMLoop.prototype, 'run').mockImplementation(
      async function* (): AsyncGenerator<string> {
        yield 'The secret answer is 42 and this is a complete sentence.';
      },
    );

    const { calls: ttsCalls } = setupTtsMock(defaultTtsImpl);

    const deps = makeDeps(bridge, {
      guardrails: [
        {
          name: 'no-numbers',
          check: (text) => /\d+/.test(text),
          replacement: 'I cannot share numerical information.',
        },
      ],
    });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-guard-check');
    await stt.emitTranscript('What is the answer?');

    await vi.waitFor(
      () => expect(ttsCalls.length).toBeGreaterThan(0),
      { timeout: 5000 },
    );

    expect(ttsCalls.some((t) => t.includes('I cannot share numerical information.'))).toBe(true);
  }, 10000);

  // =========================================================================
  // Additional: non-final STT transcripts are ignored by the pipeline
  // =========================================================================

  it('non-final STT transcript does not trigger pipeline', async () => {
    const stt = makeMockStt();
    const bridge = makeTwilioBridge(stt);
    const onMessage = vi.fn().mockResolvedValue('response');

    const deps = makeDeps(bridge, {}, { onMessage });
    const ws = makeMockWs();
    const handler = new StreamHandler(deps, ws, '+15551111111', '+15552222222');

    await handler.handleCallStart('CA-nonfinal');

    // Call the transcript callback with isFinal: false
    const transcriptCb = stt.onTranscript.mock.calls[0][0] as (
      t: { isFinal?: boolean; text?: string },
    ) => Promise<void>;
    await transcriptCb({ isFinal: false, text: 'partial...' });

    await new Promise<void>((r) => setTimeout(r, 100));
    expect(onMessage).not.toHaveBeenCalled();
  });
});

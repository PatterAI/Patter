/**
 * Eval-harness fakes — the ONLY mocked boundary of {@link EvalSession}.
 *
 * Everything inward of these (transcript commit filters, barge-in state
 * machine, LLM loop, tool executor, hooks, guardrails, sentence chunking,
 * metrics) is the REAL pipeline code. The fakes stand in for the
 * paid/external surfaces a live call touches: the telephony carrier
 * (audio sender), the STT socket, and the TTS socket.
 */

import type { WebSocket as WSWebSocket } from 'ws';
import type { TelephonyBridge } from '../stream-handler';
import type {
  AgentOptions,
  CarrierKind,
  TransferCallOptions,
  TransferCallResult,
  VADEvent,
  VADProvider,
} from '../types';
import type { STTAdapter, STTTranscriptCallback, TTSAdapter } from '../provider-factory';
import { ErrorCode, PatterError } from '../errors';
import { getLogger } from '../logger';

/** Minimal carrier-WebSocket stub (the carrier socket is an external boundary). */
export function makeFakeCarrierWs(): WSWebSocket {
  const noop = (): void => undefined;
  return {
    send: noop,
    close: noop,
    on: noop,
    once: noop,
    removeListener: noop,
    addEventListener: noop,
    removeEventListener: noop,
    readyState: 1,
    OPEN: 1,
  } as unknown as WSWebSocket;
}

/**
 * Records every carrier-bound audio operation; auto-acks marks.
 *
 * The TS analogue of the carrier boundary is the {@link TelephonyBridge}, so
 * this fake implements it directly. ``sentAudio`` collects the decoded wire
 * chunks handed to the carrier, ``clears`` counts ``sendClear`` calls
 * (barge-in flushes), ``marks`` records mark names, ``endedCalls`` /
 * ``transfers`` record call-control invocations. When attached to a handler,
 * each ``sendMark`` is echoed back through ``handler.onMark`` so mark-gated
 * pacing never deadlocks.
 */
export class FakeAudioSender implements TelephonyBridge {
  readonly label = 'Eval';
  // The metrics/dashboard tag for the faked carrier — mirrors Python's
  // ``telephony_provider="eval"``. No pricing row exists for 'eval', so
  // telephony cost is always 0. The cast is deliberate: CarrierKind
  // enumerates real carriers and the harness is not one.
  readonly telephonyProvider = 'eval' as CarrierKind;
  readonly inputWireFormat = 'pcm_16000' as const;

  readonly sentAudio: Buffer[] = [];
  readonly marks: string[] = [];
  readonly endedCalls: string[] = [];
  readonly transfers: Array<{ readonly callId: string; readonly toNumber: string }> = [];
  clears = 0;

  private handler: { onMark(markName: string): Promise<void> } | null = null;
  private sttFactory: (() => STTAdapter | null) | null = null;

  /** Wire the auto-ack loopback for ``sendMark`` → ``onMark``. */
  attachHandler(handler: { onMark(markName: string): Promise<void> }): void {
    this.handler = handler;
  }

  /** Wire the STT instance ``createStt`` should hand to the handler. */
  attachSttFactory(factory: () => STTAdapter | null): void {
    this.sttFactory = factory;
  }

  sendAudio(_ws: WSWebSocket, audioBase64: string, _streamSid: string): void {
    this.sentAudio.push(Buffer.from(audioBase64, 'base64'));
  }

  sendMark(_ws: WSWebSocket, markName: string, _streamSid: string): void {
    this.marks.push(markName);
    if (this.handler !== null) {
      // Echo the ack — resolves any pending-mark waiter the handler
      // registered just before this call. Fire-and-forget: the bridge
      // contract is synchronous and onMark never throws in practice.
      void this.handler.onMark(markName).catch((err) => {
        getLogger().debug(`FakeAudioSender mark ack failed: ${String(err)}`);
      });
    }
  }

  sendClear(_ws: WSWebSocket, _streamSid: string): void {
    this.clears += 1;
  }

  async transferCall(
    callId: string,
    toNumber: string,
    _options?: TransferCallOptions,
  ): Promise<TransferCallResult | void> {
    this.transfers.push({ callId, toNumber });
  }

  async endCall(callId: string, _ws: WSWebSocket): Promise<void> {
    this.endedCalls.push(callId);
  }

  async createStt(_agent: AgentOptions): Promise<STTAdapter | null> {
    return this.sttFactory ? this.sttFactory() : null;
  }

  async queryTelephonyCost(): Promise<void> {
    // Faked carrier — no cost to query.
  }
}

/**
 * Callback-backed STT double feeding the handler's REAL transcript path.
 *
 * The harness pushes finalised transcripts via {@link FakeSTT.pushFinal};
 * the handler consumes them through the same ``onTranscript`` callback a
 * live Deepgram adapter drives, so ``handleBargeIn`` → ``commitTranscript``
 * → ``dispatchTurn`` run exactly as on a real call. ``pushFinal`` resolves
 * once the handler's transcript drain loop has fully processed the
 * transcript (i.e. the dispatch task for it — if any — has been created),
 * which is what {@link EvalSession.userSays} awaits before grabbing the
 * dispatch task.
 */
export class FakeSTT implements STTAdapter {
  readonly sentAudio: Buffer[] = [];
  connected = false;
  closed = false;

  private callback: STTTranscriptCallback | null = null;

  async connect(): Promise<void> {
    this.connected = true;
  }

  sendAudio(pcm: Buffer): void {
    this.sentAudio.push(pcm);
  }

  onTranscript(cb: STTTranscriptCallback): void {
    this.callback = cb;
  }

  /** Inject one final transcript through the handler's real receive path. */
  async pushFinal(text: string): Promise<void> {
    if (this.closed) {
      throw new PatterError(
        'FakeSTT is closed — the EvalSession was already cleaned up',
        { code: ErrorCode.CONFIG },
      );
    }
    if (this.callback === null) {
      throw new PatterError(
        'FakeSTT has no transcript consumer — the handler is not started',
        { code: ErrorCode.CONFIG },
      );
    }
    await this.callback({ text, isFinal: true, confidence: 1.0 });
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}

/**
 * TTS double: records the text it is asked to speak, yields silence.
 *
 * The default chunk is 320 bytes of PCM16 @ 16 kHz (10 ms of silence) so the
 * handler's playback-backlog accounting stays in the sub-frame range and a
 * following turn is never misclassified as a barge-in against a phantom
 * carrier backlog. ``closed`` flips when the handler's teardown invokes
 * ``cancelActiveStream`` (the TS TTS adapters are per-request — teardown
 * cancels the active stream rather than closing a connection).
 */
export class FakeTTS implements TTSAdapter {
  readonly spoken: string[] = [];
  closed = false;

  private readonly chunk: Buffer;

  constructor(chunk: Buffer = Buffer.alloc(320)) {
    this.chunk = chunk;
  }

  async *synthesizeStream(text: string): AsyncIterable<Buffer> {
    this.spoken.push(text);
    yield this.chunk;
  }

  /** Invoked by the handler's real teardown (``handleStop``). */
  cancelActiveStream(): void {
    this.closed = true;
  }
}

/**
 * No-op VAD injected when the agent has none — prevents the real start path
 * from auto-loading SileroVAD (ONNX model) in deterministic eval runs.
 * Never consulted in practice: the session injects transcripts directly and
 * feeds no audio frames.
 */
export class NoopVad implements VADProvider {
  async processFrame(_pcmChunk: Buffer, _sampleRate: number): Promise<VADEvent | null> {
    return null;
  }

  reset(): void {
    // No per-utterance state.
  }

  async close(): Promise<void> {
    // Nothing to release.
  }
}

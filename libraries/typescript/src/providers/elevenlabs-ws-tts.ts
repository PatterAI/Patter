/**
 * WebSocket-based ElevenLabs TTS provider — opt-in low-latency variant.
 *
 * Targets the ElevenLabs streaming-input WebSocket endpoint
 * (`/v1/text-to-speech/{voice_id}/stream-input`) instead of the HTTP
 * `/stream` endpoint used by `ElevenLabsTTS`. Saves the HTTP request setup
 * time per utterance (~50 ms) and avoids the HTTP cold-start TLS handshake
 * when calls are bursty.
 *
 * API matches `ElevenLabsTTS` (`synthesizeStream(text)` returns an
 * `AsyncGenerator<Buffer>`) so it can be passed anywhere a TTSAdapter is
 * expected.
 *
 * Behaviour notes
 * - WebSocket is opened **per-utterance** (matches HTTP semantics). A
 *   future revision may pool a WS across utterances of the same call
 *   session — see roadmap Phase 5b.
 * - `auto_mode=true` is enabled by default. Pass `autoMode: false` to
 *   send a custom `chunk_length_schedule`.
 * - `outputFormat` is exposed as a query parameter so `ulaw_8000` (Twilio
 *   native) and `pcm_16000` (Telnyx native) work without resampling.
 * - `eleven_v3` is **not** supported — the WS endpoint rejects it.
 * - `optimize_streaming_latency` is officially deprecated and is not
 *   exposed.
 */

import WebSocket from 'ws';
import type { TTSAdapter } from '../provider-factory';
import { resolveVoiceId, type ElevenLabsModel } from './elevenlabs-tts';
import { getLogger } from '../logger';

const WS_BASE = 'wss://api.elevenlabs.io/v1/text-to-speech';
export const DEFAULT_INACTIVITY_TIMEOUT = 60;
const DEFAULT_CHUNK_SIZE = 4096;
// 5s on the same hot path. The previous 15s default left dead air on
// the carrier WebSocket while a stuck DNS/TLS handshake was retried.
const CONNECT_TIMEOUT_MS = 5_000;
// Per-frame receive timeout — guards against a stalled server keeping the
// generator alive indefinitely (matches Python ``frame_timeout`` default).
const FRAME_TIMEOUT_MS = 30_000;
// Cap on a single base64 audio frame from the server. Real frames are
// ~75 KB decoded — anything beyond ~512 KB is malicious / malformed.
const MAX_AUDIO_B64_BYTES = 512 * 1024;

/** Raised when the ElevenLabs WebSocket reports a server-side error. */
export class ElevenLabsTTSError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ElevenLabsTTSError';
  }
}

/**
 * Raised when the ElevenLabs WebSocket endpoint refuses synthesis because
 * the account plan does not include WS streaming. Distinct from generic
 * ``ElevenLabsTTSError`` so callers can catch this specifically and either
 * upgrade their plan or fall back to the HTTP class.
 *
 * Free / Starter plans get ``payment_required`` from the server on the
 * first synthesise call. The HTTP ``ElevenLabsTTS`` class works on every
 * plan, so the simplest fix is to swap the import:
 *
 * ```ts
 * // before — fails on Free / Starter:
 * import { ElevenLabsWebSocketTTS } from "getpatter";
 * // after:
 * import { ElevenLabsTTS } from "getpatter";
 * ```
 */
export class ElevenLabsPlanError extends ElevenLabsTTSError {
  constructor(message: string) {
    super(message);
    this.name = 'ElevenLabsPlanError';
  }
}

const PLAN_REQUIRED_MSG =
  'ElevenLabs WS streaming requires a Pro plan or higher (the WS endpoint ' +
  'returned `payment_required`). Either upgrade at https://elevenlabs.io/pricing, ' +
  'or use the HTTP `ElevenLabsTTS` class which works on all plans (drop-in API).';

function sanitiseLogStr(value: unknown, limit = 200): string {
  return String(value).replace(/[\r\n\x00]/g, ' ').slice(0, limit);
}

export interface ElevenLabsWebSocketTTSOptions {
  apiKey: string;
  voiceId?: string;
  modelId?: ElevenLabsModel | string;
  outputFormat?: string;
  voiceSettings?: Record<string, unknown>;
  languageCode?: string;
  /** Let the server pick chunk timing. Default true. */
  autoMode?: boolean;
  /** WS keep-alive timeout in seconds (5–180). Default 60. */
  inactivityTimeout?: number;
  /**
   * Manual chunk schedule, only used when ``autoMode: false``. Each value
   * must be 5–500. ElevenLabs default is ``[120, 160, 250, 290]``.
   */
  chunkLengthSchedule?: number[];
  /** Outgoing audio re-chunk size in bytes. Default 4096. */
  chunkSize?: number;
}

interface ElevenLabsWsMessage {
  audio?: string;
  isFinal?: boolean;
  error?: string;
}

/**
 * Map of telephony carrier → ElevenLabs WS-native ``output_format`` for
 * zero-transcode delivery to the carrier wire. Twilio Media Streams speaks
 * PCMU/μ-law @ 8 kHz; Telnyx negotiates linear PCM 16 kHz.
 */
const CARRIER_NATIVE_FORMAT: Readonly<Record<string, string>> = {
  twilio: 'ulaw_8000',
  telnyx: 'pcm_16000',
};

export class ElevenLabsWebSocketTTS implements TTSAdapter {
  static readonly providerKey = 'elevenlabs_ws';
  readonly apiKey: string;
  readonly voiceId: string;
  readonly modelId: string;
  readonly voiceSettings?: Record<string, unknown>;
  readonly languageCode?: string;
  readonly autoMode: boolean;
  readonly inactivityTimeout: number;
  readonly chunkLengthSchedule?: number[];
  readonly chunkSize: number;

  /**
   * The wire format requested over the ElevenLabs WS. Initially set from
   * the constructor; ``setTelephonyCarrier`` may auto-flip it to the
   * carrier's native codec when the caller did NOT pass ``outputFormat``
   * explicitly.
   */
  private _outputFormat: string;
  private readonly _outputFormatExplicit: boolean;

  /** Public read-only view of the (possibly auto-flipped) wire format. */
  get outputFormat(): string {
    return this._outputFormat;
  }

  constructor(opts: ElevenLabsWebSocketTTSOptions) {
    if (opts.modelId === 'eleven_v3') {
      throw new Error(
        'eleven_v3 is not supported by the WebSocket stream-input endpoint — ' +
          'use the HTTP ElevenLabsTTS class instead.',
      );
    }
    this.apiKey = opts.apiKey;
    this.voiceId = resolveVoiceId(opts.voiceId ?? '21m00Tcm4TlvDq8ikWAM');
    this.modelId = opts.modelId ?? 'eleven_flash_v2_5';
    // Track whether the caller explicitly chose an ``outputFormat``. When
    // left undefined, default to PCM 16 kHz for backward-compat but allow
    // ``setTelephonyCarrier`` to auto-flip to the carrier's native format
    // (``ulaw_8000`` for Twilio) so ElevenLabs encodes server-side and we
    // skip a client-side mulaw transcode. When the caller passed an
    // explicit value, ``setTelephonyCarrier`` is a no-op — user wins.
    this._outputFormatExplicit = opts.outputFormat !== undefined;
    this._outputFormat = opts.outputFormat ?? 'pcm_16000';
    this.voiceSettings = opts.voiceSettings;
    this.languageCode = opts.languageCode;
    this.autoMode = opts.autoMode ?? true;
    this.inactivityTimeout = opts.inactivityTimeout ?? DEFAULT_INACTIVITY_TIMEOUT;
    this.chunkLengthSchedule = opts.chunkLengthSchedule;
    this.chunkSize = opts.chunkSize ?? DEFAULT_CHUNK_SIZE;
  }

  /**
   * Hook called by ``StreamHandler`` to advise the carrier wire format.
   *
   * When the user did NOT pass an explicit ``outputFormat`` in the
   * constructor options, this flips the format to the carrier's native
   * wire codec — saving a client-side transcode step. Calling with an
   * unknown carrier (``""`` / ``"custom"``) is a no-op.
   *
   * When ``outputFormat`` was explicitly passed (incl. via the
   * ``forTwilio`` / ``forTelnyx`` factories), this method is a no-op —
   * the user's choice always wins.
   */
  setTelephonyCarrier(carrier: string): void {
    if (this._outputFormatExplicit) return;
    const native = CARRIER_NATIVE_FORMAT[carrier];
    if (!native) return;
    this._outputFormat = native;
  }

  /** Pre-configured for Twilio Media Streams (`ulaw_8000`). */
  static forTwilio(opts: Omit<ElevenLabsWebSocketTTSOptions, 'outputFormat'>): ElevenLabsWebSocketTTS {
    return new ElevenLabsWebSocketTTS({
      ...opts,
      outputFormat: 'ulaw_8000',
      voiceSettings:
        opts.voiceSettings ?? {
          stability: 0.6,
          similarity_boost: 0.75,
          use_speaker_boost: false,
        },
    });
  }

  /** Pre-configured for Telnyx (`pcm_16000`). */
  static forTelnyx(opts: Omit<ElevenLabsWebSocketTTSOptions, 'outputFormat'>): ElevenLabsWebSocketTTS {
    return new ElevenLabsWebSocketTTS({
      ...opts,
      outputFormat: 'pcm_16000',
    });
  }

  private buildUrl(): string {
    const params = new URLSearchParams({
      model_id: this.modelId,
      output_format: this.outputFormat,
      inactivity_timeout: String(this.inactivityTimeout),
    });
    if (this.autoMode) params.set('auto_mode', 'true');
    if (this.languageCode) params.set('language_code', this.languageCode);
    return `${WS_BASE}/${encodeURIComponent(this.voiceId)}/stream-input?${params.toString()}`;
  }

  /**
   * Single-shot synthesis: open WS, send text, yield bytes, close.
   *
   * Resilience contract:
   * - Connection bounded by ``CONNECT_TIMEOUT_MS`` (5s, was 15s).
   * - Each idle wait bounded by ``FRAME_TIMEOUT_MS`` (30s) so a stalled
   *   server cannot keep the generator alive indefinitely.
   * - Permanent error handler attached BEFORE the open await — prevents
   *   ``uncaughtException`` if an error fires after the once-listener
   *   resolves.
   * - All event listeners removed in ``finally`` (no closure leak past
   *   socket close).
   * - Server-reported ``error`` raises ``ElevenLabsTTSError``.
   * - Per-frame audio payload capped at ``MAX_AUDIO_B64_BYTES``.
   * - Best-effort EOS ``{"text":""}`` sent in finally (not immediately
   *   after flush — auto_mode could otherwise truncate the tail audio).
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const ws = new WebSocket(this.buildUrl(), {
      headers: { 'xi-api-key': this.apiKey },
    });

    const queue: Buffer[] = [];
    let done = false;
    let pendingError: Error | null = null;
    let resolveWaiter: (() => void) | null = null;
    let connectTimer: ReturnType<typeof setTimeout> | undefined;

    const wakeWaiter = () => {
      const r = resolveWaiter;
      resolveWaiter = null;
      r?.();
    };

    const onMessage = (raw: WebSocket.RawData) => {
      // Binary audio frames (rare): pass through with size check.
      if (Buffer.isBuffer(raw) && !looksLikeJson(raw)) {
        if (raw.length > MAX_AUDIO_B64_BYTES) {
          getLogger().warn(
            `ElevenLabs WS binary frame too large (${raw.length} bytes), skipping`,
          );
          return;
        }
        queue.push(raw);
        wakeWaiter();
        return;
      }
      const txt = raw.toString('utf8');
      let msg: ElevenLabsWsMessage;
      try {
        msg = JSON.parse(txt);
      } catch {
        getLogger().warn('ElevenLabs WS sent non-JSON text frame');
        return;
      }
      // Process error FIRST so a single frame containing both isFinal +
      // error is not misread as clean end-of-stream.
      if (msg.error) {
        const sanitised = sanitiseLogStr(msg.error);
        getLogger().error('ElevenLabs WS reported error:', sanitised);
        // Recognise plan-gated rejections so callers can catch them
        // separately and either upgrade or fall back to the HTTP class.
        if (sanitised === 'payment_required' || /payment[_ ]required/i.test(sanitised)) {
          pendingError = new ElevenLabsPlanError(PLAN_REQUIRED_MSG);
        } else {
          pendingError = new ElevenLabsTTSError(`ElevenLabs WS error: ${sanitised}`);
        }
        done = true;
        wakeWaiter();
        return;
      }
      if (msg.audio) {
        if (typeof msg.audio !== 'string' || msg.audio.length > MAX_AUDIO_B64_BYTES) {
          getLogger().warn('ElevenLabs WS audio frame too large or malformed, skipping');
        } else {
          try {
            queue.push(Buffer.from(msg.audio, 'base64'));
          } catch {
            getLogger().warn('ElevenLabs WS sent malformed base64 audio');
          }
        }
      }
      if (msg.isFinal) {
        done = true;
      }
      wakeWaiter();
    };

    const onClose = () => {
      done = true;
      wakeWaiter();
    };

    const onError = (err: Error) => {
      pendingError = err;
      done = true;
      wakeWaiter();
    };

    // Attach the permanent error handler BEFORE awaiting open so any
    // error fired after the once-listener resolves is still caught.
    ws.on('error', onError);

    try {
      // Wait for OPEN, with timeout.
      await new Promise<void>((resolve, reject) => {
        connectTimer = setTimeout(
          () => reject(new Error('ElevenLabs WS connect timeout')),
          CONNECT_TIMEOUT_MS,
        );
        ws.once('open', () => {
          if (connectTimer) clearTimeout(connectTimer);
          connectTimer = undefined;
          resolve();
        });
        ws.once('error', (err) => {
          if (connectTimer) clearTimeout(connectTimer);
          connectTimer = undefined;
          reject(err);
        });
      });

      // Initial keep-alive packet — required by the protocol. ``""`` would
      // close the socket immediately.
      const init: Record<string, unknown> = { text: ' ' };
      if (this.voiceSettings) init['voice_settings'] = this.voiceSettings;
      if (!this.autoMode && this.chunkLengthSchedule) {
        init['generation_config'] = { chunk_length_schedule: this.chunkLengthSchedule };
      }
      ws.send(JSON.stringify(init));

      // Send actual text + flush. EOS is intentionally NOT sent here —
      // it is sent in finally as part of the close. Sending EOS
      // immediately after flush:true risks truncated audio with
      // auto_mode=true.
      ws.send(JSON.stringify({ text: text + ' ', flush: true }));

      ws.on('message', onMessage);
      ws.on('close', onClose);

      while (true) {
        if (queue.length > 0) {
          const buf = queue.shift()!;
          // Re-chunk for telephony framing.
          for (let off = 0; off < buf.length; off += this.chunkSize) {
            yield buf.subarray(off, Math.min(off + this.chunkSize, buf.length));
          }
          continue;
        }
        if (done) {
          if (pendingError) throw pendingError;
          return;
        }
        // Bounded idle wait — guards against a stalled server.
        let frameTimer: ReturnType<typeof setTimeout> | undefined;
        try {
          await new Promise<void>((res, rej) => {
            resolveWaiter = res;
            frameTimer = setTimeout(
              () => rej(new ElevenLabsTTSError(`ElevenLabs WS no frame for ${FRAME_TIMEOUT_MS}ms`)),
              FRAME_TIMEOUT_MS,
            );
          });
        } finally {
          if (frameTimer) clearTimeout(frameTimer);
        }
      }
    } finally {
      if (connectTimer) clearTimeout(connectTimer);
      // Best-effort EOS so the server stops billing for unconsumed audio.
      try {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ text: '' }));
        }
      } catch {
        /* best-effort */
      }
      try {
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close();
        }
      } catch {
        /* best-effort close */
      }
      // Drop all listeners — prevents memory leaks from closure references.
      ws.removeAllListeners();
    }
  }

  async close(): Promise<void> {
    // Connections are per-utterance, no persistent state to clean up.
  }
}

/** Heuristic — UTF-8 JSON frames start with `{` or `[`; raw audio doesn't. */
function looksLikeJson(buf: Buffer): boolean {
  if (buf.length === 0) return false;
  const b = buf[0];
  return b === 0x7b /* { */ || b === 0x5b /* [ */;
}

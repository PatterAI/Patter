/**
 * OpenAI Realtime WebSocket adapter for Patter's realtime mode.
 *
 * Wraps `wss://api.openai.com/v1/realtime` and exposes the unified
 * Patter realtime contract (`connect / sendAudio / onEvent / close`) on
 * {@link OpenAIRealtimeAdapter}.
 *
 * NOTE (issue #154): this class is no longer instantiated directly for the
 * telephony bridge. OpenAI deprecated the Beta Realtime API, so its flat
 * `output_audio_format: g711_ulaw` session shape is ignored by GA models —
 * the server falls back to PCM16 @ 24 kHz, which this adapter would forward to
 * Twilio framed as 8 kHz mulaw (static + broken STT). `buildAIAdapter` in
 * `server.ts` now routes BOTH the `OpenAIRealtime` and `OpenAIRealtime2`
 * engines through {@link OpenAIRealtime2Adapter} (GA session shape + internal
 * PCM24→mulaw8 transcode). This class is retained as the shared base class
 * that `OpenAIRealtime2Adapter` extends.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';
import type { RealtimeTurnDetection } from '../types';

/**
 * Supported OpenAI Realtime wire audio formats. See
 * https://platform.openai.com/docs/guides/realtime for the full list.
 * `G711_ULAW` matches what Twilio/Telnyx emit natively on the phone leg, so
 * no transcoding is needed. `PCM16` is used in the terminal test-mode path
 * and when the telephony provider negotiates L16/16000.
 */
export const OpenAIRealtimeAudioFormat = {
  G711_ULAW: 'g711_ulaw',
  G711_ALAW: 'g711_alaw',
  PCM16: 'pcm16',
} as const;
/** Union of {@link OpenAIRealtimeAudioFormat} string values. */
export type OpenAIRealtimeAudioFormat =
  (typeof OpenAIRealtimeAudioFormat)[keyof typeof OpenAIRealtimeAudioFormat];

/**
 * Known OpenAI Realtime API model identifiers.
 *
 * `GPT_REALTIME_2` is OpenAI's most-capable realtime voice model
 * (speech-to-speech with configurable reasoning effort, stronger
 * instruction following, 128K context). It accepts the same session
 * update wire format as the v1 `gpt-realtime` family but supports an
 * additional `reasoning.effort` field — see `reasoningEffort` on
 * {@link OpenAIRealtimeOptions}. Pricing differs from the mini default;
 * override `DEFAULT_PRICING.openai_realtime` with the values in
 * `DEFAULT_PRICING.openai_realtime_2` when selecting it.
 */
export const OpenAIRealtimeModel = {
  GPT_REALTIME: 'gpt-realtime',
  GPT_REALTIME_2: 'gpt-realtime-2',
  GPT_REALTIME_MINI: 'gpt-realtime-mini',
  GPT_4O_REALTIME_PREVIEW: 'gpt-4o-realtime-preview',
  GPT_4O_MINI_REALTIME_PREVIEW: 'gpt-4o-mini-realtime-preview',
} as const;
/** Union of {@link OpenAIRealtimeModel} string values. */
export type OpenAIRealtimeModel =
  (typeof OpenAIRealtimeModel)[keyof typeof OpenAIRealtimeModel];

/** OpenAI Realtime / TTS voice identifiers. */
export const OpenAIVoice = {
  ALLOY: 'alloy',
  ASH: 'ash',
  BALLAD: 'ballad',
  CORAL: 'coral',
  ECHO: 'echo',
  FABLE: 'fable',
  NOVA: 'nova',
  ONYX: 'onyx',
  SAGE: 'sage',
  SHIMMER: 'shimmer',
  VERSE: 'verse',
} as const;
/** Union of {@link OpenAIVoice} string values. */
export type OpenAIVoice = (typeof OpenAIVoice)[keyof typeof OpenAIVoice];

/**
 * Models accepted by `input_audio_transcription` on Realtime sessions.
 *
 * `GPT_REALTIME_WHISPER` is OpenAI's streaming-optimised Whisper variant
 * designed for low-latency transcript deltas inside a Realtime session.
 * Billed per minute of audio (separate from the conversational model
 * tokens). Use it when you want faster partial transcripts than
 * `whisper-1` at lower cost than `gpt-4o-transcribe`.
 */
export const OpenAITranscriptionModel = {
  WHISPER_1: 'whisper-1',
  GPT_4O_TRANSCRIBE: 'gpt-4o-transcribe',
  GPT_4O_MINI_TRANSCRIBE: 'gpt-4o-mini-transcribe',
  GPT_REALTIME_WHISPER: 'gpt-realtime-whisper',
} as const;
/** Union of {@link OpenAITranscriptionModel} string values. */
export type OpenAITranscriptionModel =
  (typeof OpenAITranscriptionModel)[keyof typeof OpenAITranscriptionModel];

/** Server-side voice-activity-detection modes. */
export const OpenAIRealtimeVADType = {
  SERVER_VAD: 'server_vad',
  SEMANTIC_VAD: 'semantic_vad',
} as const;
/** Union of {@link OpenAIRealtimeVADType} string values. */
export type OpenAIRealtimeVADType =
  (typeof OpenAIRealtimeVADType)[keyof typeof OpenAIRealtimeVADType];

/** Callback signature for events emitted by {@link OpenAIRealtimeAdapter}. */
export type RealtimeEventCallback = (type: string, data: unknown) => void | Promise<void>;

/** Constructor options for {@link OpenAIRealtimeAdapter}. */
export interface OpenAIRealtimeOptions {
  temperature?: number;
  maxResponseOutputTokens?: number | 'inf';
  modalities?: string[];
  toolChoice?: string | Record<string, unknown>;
  inputAudioTranscriptionModel?: string;
  vadType?: 'server_vad' | 'semantic_vad';
  /**
   * Trailing silence (ms) the server VAD waits for before treating the user's
   * turn as complete. Defaults to 300 — OpenAI's documented sweet-spot for
   * snappier turn-taking, ~200 ms faster than the previous 500 default.
   * Increase for dictation-style flows where the user pauses mid-sentence.
   */
  silenceDurationMs?: number;
  /**
   * Reasoning-effort tier for `gpt-realtime-2`. When omitted the field is
   * not sent and the server default applies. OpenAI recommends `"low"` for
   * production voice flows — higher tiers add measurable per-turn latency.
   * Has no effect on models that don't support the `reasoning` field.
   */
  reasoningEffort?: 'minimal' | 'low' | 'medium' | 'high';
  /**
   * Input noise reduction for speakerphone / conference audio. `undefined`
   * (default) omits the field entirely (no reduction — today's behavior).
   * `"far_field"` is recommended for phone / speakerphone calls;
   * `"near_field"` for a handset close to the mouth.
   *
   * v1 wire shape: emitted at the top level of `session.update` as
   * `input_audio_noise_reduction: { type }`. The GA adapter
   * (`OpenAIRealtime2Adapter`) nests it under `audio.input` instead.
   *
   * Mirrors Python `noise_reduction` on `OpenAIRealtimeAdapter`.
   */
  noiseReduction?: 'near_field' | 'far_field';
  /**
   * Turn-detection tuning. `undefined` (default) keeps the adapter's current
   * hardcoded `server_vad` / threshold `0.5` / silence 300 ms settings.
   * Raise `threshold` or switch to `semantic_vad` with `eagerness: 'low'` to
   * stop speakerphone / conference noise from triggering false barge-ins.
   *
   * Mirrors Python `turn_detection` on `OpenAIRealtimeAdapter` and
   * `turn_detection` on the engine marker `engines.openai.Realtime`.
   */
  turnDetection?: RealtimeTurnDetection;
  /**
   * Gate the model's response on the Whisper transcript (legacy behavior).
   *
   * `false` (default) — the stream handler requests the response on
   * `speech_stopped`, independently of the Whisper `transcript_input` event.
   * The transcript is display-only (dashboard / history / `onTranscript`).
   * `true` — the stream handler requests the response only after the
   * `transcript_input` event passes the hallucination filter (prior
   * behavior).
   *
   * The adapter itself does not act on this flag — it is read by the stream
   * handler via {@link OpenAIRealtimeAdapter.getGateResponseOnTranscript} to
   * decide WHEN to call {@link OpenAIRealtimeAdapter.requestResponse}.
   *
   * Mirrors Python `gate_response_on_transcript` on `OpenAIRealtimeAdapter`.
   */
  gateResponseOnTranscript?: boolean;
}

/**
 * Validate a {@link RealtimeTurnDetection} at runtime.
 *
 * TypeScript's interface is compile-time only; a value arriving from JSON
 * config, a plain-JS caller, or an `as` cast can still be malformed and would
 * otherwise flow straight to the OpenAI `session.update` wire (where an
 * out-of-set `type` silently falls into the `server_vad` branch). This rejects
 * bad values cleanly, mirroring Python `RealtimeTurnDetection.__post_init__`
 * so the two SDKs share the same error taxonomy.
 */
export function validateRealtimeTurnDetection(
  td: RealtimeTurnDetection | undefined,
): void {
  if (td === undefined) return;
  if (td.type !== undefined && td.type !== 'server_vad' && td.type !== 'semantic_vad') {
    throw new Error(
      `RealtimeTurnDetection.type must be 'server_vad' or 'semantic_vad', got ${JSON.stringify(td.type)}`,
    );
  }
  if (
    td.eagerness !== undefined &&
    td.eagerness !== 'low' &&
    td.eagerness !== 'medium' &&
    td.eagerness !== 'high' &&
    td.eagerness !== 'auto'
  ) {
    throw new Error(
      `RealtimeTurnDetection.eagerness must be one of low|medium|high|auto, got ${JSON.stringify(td.eagerness)}`,
    );
  }
  if (td.eagerness !== undefined && td.type !== 'semantic_vad') {
    throw new Error(
      "RealtimeTurnDetection.eagerness is only valid when type='semantic_vad'",
    );
  }
}

/**
 * Build the `turn_detection` wire dict shared by the v1 and GA session
 * builders so the two paths never drift.
 *
 * `td` is an optional {@link RealtimeTurnDetection}. When `undefined` the
 * adapter's current hardcoded defaults are emitted. `semantic_vad` omits
 * threshold / padding / silence (OpenAI rejects them) and emits `eagerness`
 * only when set.
 *
 * `includeResponseGating` adds the GA-only `create_response` /
 * `interrupt_response` keys. The v1 shape omits them — on v1 the server's
 * own defaults (`create_response: true`, `interrupt_response: true`) apply,
 * which is exactly the server-managed behaviour we want by default, so
 * sending nothing is equivalent to sending `true`.
 *
 * When `includeResponseGating` is true the two keys are tied to
 * `gateResponseOnTranscript` (issue #154):
 *   - `gateResponseOnTranscript === false` (DEFAULT, server-managed) →
 *     `create_response: true` + `interrupt_response: true`. The server owns
 *     VAD, end-of-turn, response creation AND the barge-in cancel signal.
 *   - `gateResponseOnTranscript === true` (legacy / client-managed opt-out) →
 *     `create_response: false` + `interrupt_response: false`. Patter drives
 *     `response.create` (after the hallucination filter) and `response.cancel`
 *     (on barge-in) itself — the escape hatch for no-AEC PSTN
 *     self-interruption.
 *
 * Mirrors Python `build_turn_detection()` in `providers/openai_realtime.py`.
 */
export function buildTurnDetection(
  td: RealtimeTurnDetection | undefined,
  opts: {
    defaultType: string;
    defaultSilenceMs: number;
    includeResponseGating: boolean;
    gateResponseOnTranscript?: boolean;
  },
): Record<string, unknown> {
  validateRealtimeTurnDetection(td);
  let detection: Record<string, unknown>;
  if (td?.type === 'semantic_vad') {
    detection = { type: 'semantic_vad' };
    if (td.eagerness !== undefined) detection.eagerness = td.eagerness;
  } else {
    detection = {
      type: td?.type ?? opts.defaultType,
      threshold: td?.threshold ?? 0.5,
      prefix_padding_ms: td?.prefixPaddingMs ?? 300,
      silence_duration_ms: td?.silenceDurationMs ?? opts.defaultSilenceMs,
    };
  }
  if (opts.includeResponseGating) {
    // Server-managed by default: both true when the gate is off. Inverted so
    // the absence of the gate flag yields the OpenAI server defaults.
    const serverManaged = !(opts.gateResponseOnTranscript ?? false);
    detection.create_response = serverManaged;
    detection.interrupt_response = serverManaged;
  }
  return detection;
}

/** Realtime WebSocket adapter for OpenAI's `gpt-realtime` family. */
export class OpenAIRealtimeAdapter {
  // Fields exposed `protected` (not `private`) so a subclass can implement
  // alternate transports — e.g. `OpenAIRealtime2Adapter` overrides
  // `connect()` to speak the GA Realtime API while reusing the rest of
  // the runtime (audio dispatch, barge-in, heartbeat).
  protected ws: WebSocket | null = null;
  private readonly eventCallbacks: Set<RealtimeEventCallback> = new Set();
  private messageListenerAttached = false;
  private heartbeat: NodeJS.Timeout | null = null;
  // Track the in-flight assistant item id so we can truncate cleanly on
  // barge-in (see ``cancelResponse``) — matches the Python adapter.
  private currentResponseItemId: string | null = null;
  private currentResponseAudioMs = 0;
  // Wall-clock timestamp (Date.now()) of the first ``response.audio.delta``
  // received since the current response item started. ``cancelResponse``
  // uses this to bound ``audio_end_ms`` to what the caller could plausibly
  // have heard — generated audio frequently arrives 5-10x real-time, so
  // ``audio_end_ms`` driven purely by the per-chunk byte counter overshoots
  // reality and leaves phantom assistant text on the conversation. The
  // wall-clock cap corresponds to the maximum playback that real-time TTS
  // could have produced, which is what the user actually heard.
  private currentResponseFirstAudioAt: number | null = null;
  protected readonly options: OpenAIRealtimeOptions;
  // When true, the stream handler waits for the Whisper ``transcript_input``
  // event before requesting the model response (legacy behavior). When false
  // (default) the response is requested on ``speech_stopped`` and the
  // transcript is display-only. Read by the stream handler via
  // ``getGateResponseOnTranscript()``.
  private readonly gateResponseOnTranscript: boolean;

  constructor(
    protected readonly apiKey: string,
    protected readonly model: string = OpenAIRealtimeModel.GPT_REALTIME_MINI,
    protected readonly voice: string = OpenAIVoice.ALLOY,
    protected readonly instructions: string = '',
    protected readonly tools?: Array<{ name: string; description: string; parameters: Record<string, unknown>; strict?: boolean }>,
    // Audio wire format negotiated with OpenAI Realtime. Mirrors the Python
    // ``audio_format`` kwarg. Default ``g711_ulaw`` matches the Twilio/Telnyx
    // inbound codec so audio flows through without transcoding.
    protected readonly audioFormat: OpenAIRealtimeAudioFormat = OpenAIRealtimeAudioFormat.G711_ULAW,
    options: OpenAIRealtimeOptions = {},
  ) {
    this.options = options;
    this.gateResponseOnTranscript = options.gateResponseOnTranscript ?? false;
  }

  /**
   * Whether the stream handler should gate the model response on the Whisper
   * transcript (legacy) or fire it on `speech_stopped` (default, decoupled).
   *
   * `false` (default) — the response is requested on `speech_stopped`,
   * independently of Whisper. `true` — the response is requested only after
   * `transcript_input` passes the hallucination filter.
   */
  getGateResponseOnTranscript(): boolean {
    return this.gateResponseOnTranscript;
  }

  /**
   * Build the production session.update body. Mirrors the body sent
   * inside `connect()` so warmup can apply identical configuration to
   * the upstream session and prime it without billing.
   */
  private buildSessionConfig(): Record<string, unknown> {
    const config: Record<string, unknown> = {
      input_audio_format: this.audioFormat,
      output_audio_format: this.audioFormat,
      voice: this.voice,
      instructions: this.instructions || 'You are a helpful voice assistant. Be concise.',
      // v1 turn_detection carries NO create_response / interrupt_response
      // keys. The v1 server defaults (`create_response: true`,
      // `interrupt_response: true`) ARE the server-managed behaviour we want by
      // default, so omitting them is equivalent to sending `true` — gating
      // disabled here. `gateResponseOnTranscript` is still threaded through for
      // symmetry with the GA builder, but has no wire effect while
      // includeResponseGating is false.
      turn_detection: buildTurnDetection(this.options.turnDetection, {
        defaultType: this.options.vadType ?? OpenAIRealtimeVADType.SERVER_VAD,
        defaultSilenceMs: this.options.silenceDurationMs ?? 300,
        includeResponseGating: false,
        gateResponseOnTranscript: this.gateResponseOnTranscript,
      }),
      input_audio_transcription: {
        model: this.options.inputAudioTranscriptionModel ?? OpenAITranscriptionModel.WHISPER_1,
      },
    };
    // v1 puts noise reduction at the TOP LEVEL of session (not nested under
    // audio.input as the GA shape does). Omitted entirely when unset.
    if (this.options.noiseReduction !== undefined) {
      config.input_audio_noise_reduction = { type: this.options.noiseReduction };
    }
    if (this.options.temperature !== undefined) config.temperature = this.options.temperature;
    if (this.options.maxResponseOutputTokens !== undefined) {
      config.max_response_output_tokens = this.options.maxResponseOutputTokens;
    }
    if (this.options.modalities !== undefined) config.modalities = this.options.modalities;
    if (this.options.toolChoice !== undefined) config.tool_choice = this.options.toolChoice;
    if (this.options.reasoningEffort !== undefined) {
      config.reasoning = { effort: this.options.reasoningEffort };
    }
    if (this.tools?.length) {
      config.tools = this.tools.map((t) => {
        const def: Record<string, unknown> = {
          type: 'function',
          name: t.name,
          description: t.description,
          parameters: t.parameters,
        };
        // Propagate strict mode when the user opted in. OpenAI's strict
        // mode constrains the model to emit arguments that exactly match
        // the schema (no missing required fields, no extra properties).
        if ((t as { strict?: boolean }).strict === true) {
          def.strict = true;
        }
        return def;
      });
    }
    return config;
  }

  /**
   * Pre-call WebSocket warmup for the OpenAI Realtime endpoint.
   *
   * The canonical session-only warm step on the Realtime API: open the
   * WS, wait for `session.created`, send a single `session.update`
   * containing the same fields that the production `connect()` path
   * applies (`input_audio_format`, `output_audio_format`, `voice`,
   * `instructions`, `turn_detection`, `input_audio_transcription`,
   * plus any opt-in fields populated on the adapter), wait for the
   * matching `session.updated` ack, then close cleanly. This primes
   * the per-session state on the OpenAI side — DNS + TLS + auth
   * handshake + initial config exchange — without ever invoking the
   * model.
   *
   * Earlier revisions sent `response.create` with
   * `{"response": {"generate": false}}` to prime the inference path.
   * That field is NOT in the OpenAI Realtime API schema; the server
   * either ignores it (and bills tokens for a real model response) or
   * rejects the request with `invalid_request_error`. Both behaviours
   * are billing-unsafe or a no-op beyond TLS warm. The
   * `session.update` flow is documented and side-effect-free.
   *
   * Billing safety: `session.update` only mutates session
   * configuration. It does NOT invoke the model, does NOT consume any
   * audio buffer, and does NOT trigger token generation, so no
   * per-token cost is accrued. Best-effort: failures are logged at
   * debug level and never raised.
   */
  async warmup(): Promise<void> {
    const url = `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(this.model)}`;
    let ws: WebSocket | null = null;
    try {
      ws = await new Promise<WebSocket>((resolve, reject) => {
        const sock = new WebSocket(url, {
          headers: {
            Authorization: `Bearer ${this.apiKey}`,
          },
        });
        const timer = setTimeout(() => {
          try {
            sock.close();
          } catch {
            // ignore
          }
          reject(new Error('OpenAI Realtime warmup connect timeout'));
        }, 5000);
        sock.once('open', () => {
          clearTimeout(timer);
          resolve(sock);
        });
        sock.once('error', (err: Error) => {
          clearTimeout(timer);
          reject(err);
        });
      });

      // Wait for session.created (up to 2 s).
      const sessionCreated = await new Promise<boolean>((resolve) => {
        const timer = setTimeout(() => resolve(false), 2000);
        const onMsg = (raw: Buffer | string): void => {
          try {
            const data = JSON.parse(raw.toString()) as { type?: string };
            if (data.type === 'session.created') {
              clearTimeout(timer);
              ws!.off('message', onMsg);
              resolve(true);
            }
          } catch {
            // ignore parse errors
          }
        };
        ws!.on('message', onMsg);
      });
      if (!sessionCreated) return;

      // Send session.update with the same fields the production
      // ``connect()`` path applies, so the upstream session state is
      // primed identically to a real call.
      try {
        ws.send(JSON.stringify({ type: 'session.update', session: this.buildSessionConfig() }));
      } catch {
        return;
      }

      // Best-effort: drain frames until we see ``session.updated`` (or
      // time out). Waiting for the ack lets us close after a clean
      // handshake instead of mid-frame; the TLS + session prime is
      // already done by the time the server processes our update.
      await new Promise<void>((resolve) => {
        const timer = setTimeout(() => resolve(), 1500);
        const onMsg = (raw: Buffer | string): void => {
          try {
            const data = JSON.parse(raw.toString()) as { type?: string };
            if (data.type === 'session.updated') {
              clearTimeout(timer);
              ws!.off('message', onMsg);
              resolve();
            }
          } catch {
            // ignore
          }
        };
        ws!.on('message', onMsg);
      });
    } catch (err) {
      getLogger().debug(`OpenAI Realtime warmup failed (best-effort): ${String(err)}`);
    } finally {
      if (ws) {
        try {
          ws.close();
        } catch {
          // ignore
        }
      }
    }
  }

  /** Open the Realtime WebSocket and apply the session configuration. */
  async connect(): Promise<void> {
    const url = `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(this.model)}`;
    this.ws = new WebSocket(url, {
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
      },
    });

    await new Promise<void>((resolve, reject) => {
      let sessionCreated = false;
      let settled = false;
      const ws = this.ws!;

      const onSetupMessage = (raw: Buffer | string): void => {
        let msg: { type: string };
        try {
          msg = JSON.parse(raw.toString()) as { type: string };
        } catch (e) {
          getLogger().warn(`OpenAI Realtime: failed to parse message: ${String(e)}`);
          return;
        }
        if (msg.type === 'session.created' && !sessionCreated) {
          sessionCreated = true;
          ws.send(JSON.stringify({ type: 'session.update', session: this.buildSessionConfig() }));
        } else if (msg.type === 'session.updated') {
          cleanup();
          resolve();
        }
      };

      const onSetupError = (err: Error): void => {
        cleanup();
        try { ws.close(); } catch { /* ignore */ }
        reject(err);
      };

      const cleanup = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ws.off('message', onSetupMessage);
        ws.off('error', onSetupError);
      };

      const timer = setTimeout(() => {
        cleanup();
        try { ws.close(); } catch { /* ignore */ }
        reject(new Error('OpenAI Realtime connect timeout'));
      }, 15000);

      ws.on('message', onSetupMessage);
      ws.on('error', onSetupError);
    });

    this.armHeartbeatAndListener();
  }

  /**
   * Adopt a pre-opened, already-`session.updated` Realtime WebSocket
   * produced by the prewarm pipeline (see `Patter.parkProviderConnections`).
   * Skips the fresh `new WebSocket()` + `session.created` /
   * `session.update` round-trip — saves ~250-450 ms on first turn.
   *
   * Caller MUST verify `ws.readyState === OPEN` before calling and MUST
   * have already received `session.updated` on the parked socket. If
   * the parked WS died between park and adopt, fall back to `connect()`.
   */
  adoptWebSocket(ws: WebSocket): void {
    this.ws = ws;
    this.armHeartbeatAndListener();
  }

  protected armHeartbeatAndListener(): void {
    // Keep WS alive across long silent stretches. ws's server-side `pong`
    // handler satisfies this automatically; we just need to ping.
    this.heartbeat = setInterval(() => {
      try {
        this.ws?.ping();
      } catch { /* ignore */ }
    }, 20000);

    // Attach the single persistent message/close/error listener now that
    // setup is done. All consumer callbacks route through `eventCallbacks`.
    this.ensureMessageListener();
  }

  /**
   * Open a fresh Realtime WS, exchange `session.created` /
   * `session.update` / `session.updated` (so the upstream session is
   * fully primed), and return the OPEN socket WITHOUT arming the
   * heartbeat / message listener. Used by the prewarm pipeline to park
   * a Realtime connection during ringing; the live consumer adopts it
   * via {@link adoptWebSocket}.
   *
   * Bounded by 8 s. Throws on timeout / handshake failure — callers
   * (the prewarm pipeline) treat any error as a cache miss and the
   * call falls through to the cold `connect()` path.
   *
   * Billing safety: `session.update` does not invoke the model. No
   * tokens are billed.
   */
  async openParkedConnection(): Promise<WebSocket> {
    const url = `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(this.model)}`;
    const ws = new WebSocket(url, {
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
      },
    });
    await new Promise<void>((resolve, reject) => {
      let sessionCreated = false;
      let settled = false;
      const onMessage = (raw: Buffer | string): void => {
        let msg: { type?: string };
        try {
          msg = JSON.parse(raw.toString()) as { type?: string };
        } catch {
          return;
        }
        if (msg.type === 'session.created' && !sessionCreated) {
          sessionCreated = true;
          try {
            ws.send(JSON.stringify({ type: 'session.update', session: this.buildSessionConfig() }));
          } catch (err) {
            cleanup();
            reject(err instanceof Error ? err : new Error(String(err)));
          }
        } else if (msg.type === 'session.updated') {
          cleanup();
          resolve();
        }
      };
      const onError = (err: Error): void => {
        cleanup();
        reject(err);
      };
      const cleanup = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ws.off('message', onMessage);
        ws.off('error', onError);
      };
      const timer = setTimeout(() => {
        cleanup();
        try { ws.close(); } catch { /* ignore */ }
        reject(new Error('OpenAI Realtime park connect timeout'));
      }, 8000);
      ws.on('message', onMessage);
      ws.on('error', onError);
    });
    return ws;
  }

  /** Append a base64-encoded audio chunk to the realtime input buffer. */
  sendAudio(mulawAudio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: mulawAudio.toString('base64') }));
  }

  /**
   * Register a listener for parsed realtime events.
   *
   * Previously every call attached a new ``ws.on('message')`` handler,
   * which leaked listeners across retries and multi-consumer hooks. We now
   * route all traffic through a single persistent handler that fans out to
   * a Set of callbacks. Use {@link offEvent} to remove one.
   */
  onEvent(callback: RealtimeEventCallback): void {
    this.eventCallbacks.add(callback);
    this.ensureMessageListener();
  }

  /** Remove a previously registered {@link onEvent} callback. */
  offEvent(callback: RealtimeEventCallback): void {
    this.eventCallbacks.delete(callback);
  }

  protected ensureMessageListener(): void {
    if (this.messageListenerAttached || !this.ws) return;
    this.messageListenerAttached = true;
    const ws = this.ws;

    const dispatch = (type: string, payload: unknown): void => {
      for (const cb of this.eventCallbacks) {
        void Promise.resolve(cb(type, payload)).catch((err) =>
          getLogger().error('onEvent callback error:', err),
        );
      }
    };

    ws.on('message', (raw) => {
      let data: {
        type: string;
        delta?: string;
        transcript?: string;
        call_id?: string;
        name?: string;
        arguments?: string;
        error?: unknown;
        response?: Record<string, unknown>;
        item?: { id?: string };
        item_id?: string;
      };
      try {
        data = JSON.parse(raw.toString()) as typeof data;
      } catch (e) {
        getLogger().warn(`OpenAI Realtime: failed to parse event message: ${String(e)}`);
        return;
      }
      const t = data.type;
      if (t === 'response.audio.delta') {
        const buf = Buffer.from(data.delta ?? '', 'base64');
        this.currentResponseAudioMs += estimateAudioMs(buf, this.audioFormat);
        // Record wall-clock arrival of the first chunk for this response so
        // ``cancelResponse`` can bound truncate to what could plausibly have
        // been played in real time.
        if (this.currentResponseFirstAudioAt === null) {
          this.currentResponseFirstAudioAt = Date.now();
        }
        dispatch('audio', buf);
      } else if (t === 'response.audio_transcript.delta') {
        dispatch('transcript_output', data.delta);
      } else if (t === 'response.content_part.added' || t === 'response.output_item.added') {
        const itemId = data.item?.id ?? data.item_id ?? null;
        if (itemId) {
          this.currentResponseItemId = itemId;
          this.currentResponseAudioMs = 0;
          this.currentResponseFirstAudioAt = null;
        }
      } else if (t === 'input_audio_buffer.speech_started') {
        dispatch('speech_started', null);
      } else if (t === 'input_audio_buffer.speech_stopped') {
        dispatch('speech_stopped', null);
      } else if (t === 'conversation.item.input_audio_transcription.completed') {
        dispatch('transcript_input', data.transcript);
      } else if (t === 'response.function_call_arguments.done') {
        dispatch('function_call', { call_id: data.call_id, name: data.name, arguments: data.arguments });
      } else if (t === 'response.done') {
        this.currentResponseItemId = null;
        this.currentResponseAudioMs = 0;
        this.currentResponseFirstAudioAt = null;
        dispatch('response_done', data.response ?? null);
      } else if (t === 'error') {
        dispatch('error', data.error);
      }
    });

    ws.on('close', (code, reason) => {
      if (code !== 1000) {
        // Surface non-normal closes so consumers can decide whether to
        // reconnect — we intentionally don't reconnect here.
        dispatch('error', {
          type: 'connection_closed',
          code,
          reason: reason?.toString() ?? '',
        });
      }
    });

    ws.on('error', (err) => {
      dispatch('error', { type: 'socket_error', message: err?.message ?? String(err) });
    });
  }

  /** Truncate the in-flight assistant turn's playback offset on the server.
   *
   * Sends ONLY ``conversation.item.truncate`` — no ``response.cancel``. This
   * is the half of barge-in handling that a WebSocket transport MUST always
   * perform: per OpenAI's docs, the GA server auto-truncates on barge-in only
   * over WebRTC / SIP; on the WebSocket transport the client is responsible
   * for telling the server how much of the assistant turn was actually heard.
   * In server-managed mode (``interrupt_response: true``) the server already
   * cancels the response itself, so issuing ``response.cancel`` here would be
   * redundant / rejected — call this method, not {@link cancelResponse}.
   *
   * ``audio_end_ms`` MUST reflect what the caller actually heard, not what
   * the server generated. OpenAI streams audio at 5-10x real-time, so the
   * byte-derived counter overstates playback whenever the consumer cleared
   * its playout buffer (e.g. ``sendClear``) before the audio reached the
   * speaker. We bound the truncate point by wall-clock time since the first
   * chunk of this response — that's the physical maximum a 1x real-time
   * playback could have produced. Without this cap, OpenAI keeps the full
   * generated assistant text on the transcript, and the model replays /
   * resumes from it on the next turn — manifesting as re-greetings and
   * mid-sentence fragments after a barge-in storm.
   *
   * No-op when no response is in flight, keeping it idempotent across stale
   * callers. Resets per-response tracking so post-truncate late frames and
   * the next response start clean.
   */
  truncate(): void {
    if (!this.ws) return;
    if (!this.currentResponseItemId) {
      // No response in flight — nothing to truncate.
      return;
    }
    let audioEndMs = this.currentResponseAudioMs;
    if (this.currentResponseFirstAudioAt !== null) {
      const elapsedMs = Date.now() - this.currentResponseFirstAudioAt;
      audioEndMs = Math.min(audioEndMs, Math.max(elapsedMs, 0));
    }
    try {
      this.ws.send(JSON.stringify({
        type: 'conversation.item.truncate',
        item_id: this.currentResponseItemId,
        content_index: 0,
        audio_end_ms: audioEndMs,
      }));
    } catch (err) {
      getLogger().debug?.(`conversation.item.truncate failed: ${String(err)}`);
    }
    // Reset per-response tracking so any post-truncate late frames and the
    // next response.create start clean.
    this.currentResponseItemId = null;
    this.currentResponseAudioMs = 0;
    this.currentResponseFirstAudioAt = null;
  }

  /** Truncate the in-flight assistant turn AND cancel the active response.
   *
   * Sends BOTH ``conversation.item.truncate`` (the played-offset bookkeeping)
   * AND ``response.cancel``. Use this on the LEGACY client-managed barge-in
   * path (``gateResponseOnTranscript`` true → ``interrupt_response: false``,
   * so the server does NOT cancel for us) and for explicit cancels driven by
   * Patter (e.g. on transfer / hangup). In server-managed mode call
   * {@link truncate} instead — the server already cancels the response, and an
   * extra ``response.cancel`` would be redundant / rejected.
   *
   * Truncation bounding semantics are identical to {@link truncate}; see its
   * doc comment for the ``audio_end_ms`` wall-clock cap rationale.
   */
  cancelResponse(): void {
    if (!this.ws) return;
    // No response in flight — nothing to cancel. OpenAI Realtime GA rejects an
    // unconditional ``response.cancel`` with ``response_cancel_not_active``,
    // which surfaces as ERROR-level log spam on every phantom VAD
    // ``speech_started`` (echo of agent audio, voicemail beep, line noise).
    // ``truncate`` already no-ops when there is no in-flight item; mirror that
    // guard here so we never emit a bare ``response.cancel``.
    if (!this.currentResponseItemId) {
      return;
    }
    // Truncate first (emits conversation.item.truncate and resets tracking),
    // then send the explicit cancel.
    this.truncate();
    this.ws.send(JSON.stringify({ type: 'response.cancel' }));
  }

  /** Inject a user text turn and request a new response. */
  async sendText(text: string): Promise<void> {
    this.ws?.send(JSON.stringify({
      type: 'conversation.item.create',
      item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] },
    }));
    this.ws?.send(JSON.stringify({ type: 'response.create' }));
  }

  /**
   * Trigger `response.create` with no new user item.
   *
   * Used by the Realtime stream-handler to drive a response after the
   * client-side hallucination filter accepts an
   * `input_audio_transcription.completed` event. The server VAD config
   * sets `create_response: false` so OpenAI no longer auto-creates a
   * response on every `input_audio_buffer.committed`; Patter is now
   * responsible for triggering it explicitly when a real user turn lands.
   */
  async requestResponse(): Promise<void> {
    this.ws?.send(JSON.stringify({ type: 'response.create' }));
  }

  /**
   * Make the AI speak ``text`` as its opening line.
   *
   * Triggers ``response.create`` with explicit ``instructions`` that force
   * the model to render ``text`` verbatim as its first audio utterance.
   * This is the correct semantics for ``Agent.firstMessage`` per its
   * docstring ("What the AI says when the callee answers").
   *
   * Without this, ``sendText(firstMessage)`` would inject ``text`` as
   * ``role: user`` and the AI would *reply* to its own greeting, producing
   * role-confused openings (e.g. a receptionist agent responding "I'd like
   * to schedule a haircut" because it took its own first_message as a
   * customer cue).
   */
  async sendFirstMessage(text: string): Promise<void> {
    this.ws?.send(JSON.stringify({
      type: 'response.create',
      response: {
        modalities: ['audio', 'text'],
        instructions: `Say exactly the following sentence as your first turn and nothing else: "${text}"`,
      },
    }));
  }

  /**
   * Speak a short reassurance filler WITHOUT injecting a `role:user` turn.
   *
   * Same no-fake-turn shape as {@link sendFirstMessage}: a bare
   * `response.create` carrying explicit `instructions`, so the filler is the
   * assistant's own in-band audio. The reassurance scheduler in the
   * stream-handler routes here instead of {@link sendText} — which would emit
   * a `conversation.item.create` with `role:'user'` and falsely show the
   * caller saying "One moment." in the transcript. Fillers must not imply
   * success or failure.
   *
   * Uses `modalities: ['audio', 'text']` (v1-beta shape). The GA subclass
   * {@link OpenAIRealtime2Adapter} overrides this with `output_modalities`
   * and re-injects `audio.output.voice` so the GA endpoint does not reject
   * the request. Mirrors Python `OpenAIRealtimeAdapter.send_reassurance` in
   * `providers/openai_realtime.py`.
   */
  async sendReassurance(text: string): Promise<void> {
    this.ws?.send(JSON.stringify({
      type: 'response.create',
      response: {
        modalities: ['audio', 'text'],
        instructions: `Say exactly this and nothing else: "${text}"`,
      },
    }));
  }

  /** Submit a tool/function-call result and request the next response. */
  async sendFunctionResult(callId: string, result: string): Promise<void> {
    this.ws?.send(JSON.stringify({
      type: 'conversation.item.create',
      item: { type: 'function_call_output', call_id: callId, output: result },
    }));
    this.ws?.send(JSON.stringify({ type: 'response.create' }));
  }

  /** Stop the heartbeat, drop listeners, and close the Realtime WebSocket. */
  close(): void {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
    this.eventCallbacks.clear();
    this.messageListenerAttached = false;
    this.ws?.close();
    this.ws = null;
  }
}

function estimateAudioMs(chunk: Buffer, format: OpenAIRealtimeAudioFormat): number {
  if (chunk.length === 0) return 0;
  // G.711 u-law / a-law: 8 kHz, 1 byte/sample → 8 bytes/ms
  if (
    format === OpenAIRealtimeAudioFormat.G711_ULAW ||
    format === OpenAIRealtimeAudioFormat.G711_ALAW
  )
    return Math.floor(chunk.length / 8);
  if (format === OpenAIRealtimeAudioFormat.PCM16) {
    // PCM16 at 24 kHz (OpenAI Realtime default): 2 bytes/sample, 24 samples/ms
    // → 48 bytes/ms. The previous divisor of 32 assumed 16 kHz which under-
    // estimated duration by 33% and inflated the apparent audio-send rate.
    // Note: session.created does not expose the negotiated sample rate in the
    // current OpenAI Realtime API, so 24 kHz is hardcoded as the known default.
    return Math.floor(chunk.length / 48);
  }
  return 0;
}

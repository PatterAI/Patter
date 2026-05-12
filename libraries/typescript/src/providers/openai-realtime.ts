/**
 * OpenAI Realtime WebSocket adapter for Patter's realtime mode.
 *
 * Wraps `wss://api.openai.com/v1/realtime` and exposes the unified
 * Patter realtime contract (`connect / sendAudio / onEvent / close`) on
 * {@link OpenAIRealtimeAdapter}. Audio negotiation defaults to
 * `g711_ulaw` so traffic flows through Twilio/Telnyx without transcoding.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

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
}

/** Realtime WebSocket adapter for OpenAI's `gpt-realtime` family. */
export class OpenAIRealtimeAdapter {
  private ws: WebSocket | null = null;
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
  // ``sendFirstMessage`` arms a one-shot server-VAD lockout so the
  // firstMessage turn cannot be interrupted by the caller's first audio
  // frames (which happens reliably on prewarm-adopted sessions where the
  // adopt+response.create races the caller's "hello?" by a few hundred ms).
  // The flag is consumed inside the message listener when the firstMessage
  // ``response.done`` arrives, at which point we re-issue ``session.update``
  // to restore the original ``turn_detection`` block captured in
  // ``savedTurnDetection``. See ``sendFirstMessage`` for the full rationale.
  private firstMessageProtectionPending = false;
  private savedTurnDetection: Record<string, unknown> | null = null;
  private readonly options: OpenAIRealtimeOptions;

  constructor(
    private readonly apiKey: string,
    private readonly model: string = OpenAIRealtimeModel.GPT_REALTIME_MINI,
    private readonly voice: string = OpenAIVoice.ALLOY,
    private readonly instructions: string = '',
    private readonly tools?: Array<{ name: string; description: string; parameters: Record<string, unknown>; strict?: boolean }>,
    // Audio wire format negotiated with OpenAI Realtime. Mirrors the Python
    // ``audio_format`` kwarg. Default ``g711_ulaw`` matches the Twilio/Telnyx
    // inbound codec so audio flows through without transcoding.
    private readonly audioFormat: OpenAIRealtimeAudioFormat = OpenAIRealtimeAudioFormat.G711_ULAW,
    options: OpenAIRealtimeOptions = {},
  ) {
    this.options = options;
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
      turn_detection: {
        type: this.options.vadType ?? OpenAIRealtimeVADType.SERVER_VAD,
        threshold: 0.5,
        prefix_padding_ms: 300,
        silence_duration_ms: this.options.silenceDurationMs ?? 300,
      },
      input_audio_transcription: {
        model: this.options.inputAudioTranscriptionModel ?? OpenAITranscriptionModel.WHISPER_1,
      },
    };
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
            'OpenAI-Beta': 'realtime=v1',
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
        'OpenAI-Beta': 'realtime=v1',
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

  private armHeartbeatAndListener(): void {
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
        'OpenAI-Beta': 'realtime=v1',
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

  private ensureMessageListener(): void {
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
      // DIAG (0.6.1 follow-up): surface server-side acks for ``session.update``
      // so we can confirm whether ``turn_detection: null`` was actually
      // accepted by the server. Some community reports indicate the server
      // may silently retain the prior ``server_vad`` config. Logging the
      // observed turn_detection echo on every ``session.updated`` makes this
      // visible in a live test without needing the OpenAI proxy.
      if (t === 'session.updated' || t === 'session.created') {
        const sess = (data as unknown as { session?: { turn_detection?: unknown } }).session;
        const td = sess?.turn_detection;
        getLogger().info(
          `[DIAG-VAD] ${t} turn_detection=${td === null ? 'null' : JSON.stringify(td)}`,
        );
      }
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
        // DIAG (0.6.1 follow-up): a speech_started event during the
        // firstMessage lockout window is the canonical "server VAD did NOT
        // honour turn_detection=null" smoking gun. Pair with the [DIAG-VAD]
        // arm/echo lines to confirm whether the server applied the update
        // BEFORE this event fired.
        if (this.firstMessageProtectionPending) {
          getLogger().info(
            '[DIAG-VAD] speech_started fired DURING lockout — server-VAD still active!',
          );
        }
        dispatch('speech_started', null);
      } else if (t === 'input_audio_buffer.speech_stopped') {
        dispatch('speech_stopped', null);
      } else if (t === 'response.cancelled' || t === 'response.canceled') {
        // DIAG (0.6.1 follow-up): observability for the failure mode —
        // server cancels the firstMessage response.create. If this fires
        // while ``firstMessageProtectionPending`` is true, the lockout
        // failed.
        getLogger().info(
          `[DIAG-VAD] ${t} (firstMessageProtectionPending=${this.firstMessageProtectionPending})`,
        );
      } else if (t === 'conversation.item.input_audio_transcription.completed') {
        dispatch('transcript_input', data.transcript);
      } else if (t === 'response.function_call_arguments.done') {
        dispatch('function_call', { call_id: data.call_id, name: data.name, arguments: data.arguments });
      } else if (t === 'response.done') {
        this.currentResponseItemId = null;
        this.currentResponseAudioMs = 0;
        this.currentResponseFirstAudioAt = null;
        // If ``sendFirstMessage`` armed the server-VAD lockout for the
        // firstMessage turn, this ``response.done`` signals the firstMessage
        // finished streaming and it is safe to restore the original
        // ``turn_detection`` so barge-in works for the rest of the call.
        // Best-effort: a failed send leaves the session without VAD which
        // degrades barge-in but does not break the call — the next
        // ``session.update`` (e.g. on a tool turn) would also rearm. See
        // ``sendFirstMessage`` for the full rationale.
        if (
          this.firstMessageProtectionPending &&
          this.savedTurnDetection !== null &&
          this.ws !== null
        ) {
          // DIAG (0.6.1 follow-up): record that we observed a response.done
          // while still in the lockout window — i.e. the firstMessage turn
          // completed before any barge-in cancelled it. This is the success
          // signal: pairing this line with the upstream [DIAG-VAD] lockout
          // arm tells us whether the protection held.
          getLogger().info(
            '[DIAG-VAD] response.done received during lockout — restoring turn_detection',
          );
          try {
            this.ws.send(
              JSON.stringify({
                type: 'session.update',
                session: { turn_detection: this.savedTurnDetection },
              }),
            );
            getLogger().info(
              `[DIAG-VAD] sent session.update turn_detection=${JSON.stringify(this.savedTurnDetection)} (restore)`,
            );
          } catch (err) {
            getLogger().debug?.(
              `first_message: turn_detection restore failed: ${String(err)}`,
            );
          }
          this.firstMessageProtectionPending = false;
          this.savedTurnDetection = null;
        } else if (this.firstMessageProtectionPending) {
          // DIAG (0.6.1 follow-up): the lockout was armed but ``savedTurnDetection``
          // got cleared along the way (e.g. send failed). Surface this so we
          // can spot inconsistent protection-state transitions.
          getLogger().info(
            '[DIAG-VAD] response.done with pending=true but saved=null — protection state inconsistent',
          );
          this.firstMessageProtectionPending = false;
        }
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

  /** Truncate the in-flight assistant turn and cancel the active response.
   *
   * ``audio_end_ms`` MUST reflect what the caller actually heard, not what
   * the server generated. OpenAI streams audio at 5-10x real-time, so the
   * byte-derived counter overstates playback whenever the consumer cleared
   * its playout buffer (e.g. ``send_clear``) before the audio reached the
   * speaker. We bound the truncate point by wall-clock time since the first
   * chunk of this response — that's the physical maximum a 1x real-time
   * playback could have produced. Without this cap, OpenAI keeps the full
   * generated assistant text on the transcript, and the model replays /
   * resumes from it on the next turn — manifesting as re-greetings and
   * mid-sentence fragments after a barge-in storm.
   */
  cancelResponse(): void {
    if (!this.ws) return;
    if (this.currentResponseItemId) {
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
    }
    this.ws.send(JSON.stringify({ type: 'response.cancel' }));
    // Reset per-response tracking so any post-cancel late frames and the
    // next response.create start clean.
    this.currentResponseItemId = null;
    this.currentResponseAudioMs = 0;
    this.currentResponseFirstAudioAt = null;
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
   *
   * Server-VAD lockout during firstMessage
   * --------------------------------------
   *
   * OpenAI Realtime server-VAD treats any caller audio that arrives before
   * the assistant's first audio frame as a barge-in and cancels the
   * in-flight ``response.create``. On the prewarm-adopted path
   * (``source=adopted ms=0``) the WS→audio bridge opens immediately at call
   * pickup; the caller's "Hi" / "Hello?" reliably reaches OpenAI in the
   * ~250-450 ms before the firstMessage audio starts streaming back, so the
   * configured ``firstMessage`` is *silently cancelled* and the caller
   * hears the agent respond to their hello instead of delivering the
   * scripted opening.
   *
   * Fix: send a ``session.update`` that sets ``turn_detection`` to ``null``
   * (OpenAI-documented: disables server-VAD entirely, no audio-driven
   * response cancellation), then ``response.create`` the firstMessage. The
   * message listener re-arms ``turn_detection`` from the saved snapshot the
   * moment ``response.done`` arrives for the firstMessage turn, restoring
   * normal barge-in for every subsequent turn. The complementary
   * client-side guard (``firstAudioSentAt`` in stream-handler) already
   * prevents the caller's outbound clear from firing — this lockout closes
   * the gap on the *server* side.
   *
   * Best-effort: if ``session.update`` cannot be sent we still proceed with
   * ``response.create``. The fallback behaviour matches the pre-fix state —
   * a higher likelihood of first-message preemption — but never worse, so
   * the call still completes.
   */
  async sendFirstMessage(text: string): Promise<void> {
    if (!this.ws) return;
    // Snapshot the original turn_detection block so the message listener
    // can restore it after the firstMessage ``response.done``. We build it
    // from the same configured fields ``buildSessionConfig`` uses so the
    // restore is byte-identical to the cold connect path.
    this.savedTurnDetection = {
      type: this.options.vadType ?? OpenAIRealtimeVADType.SERVER_VAD,
      threshold: 0.5,
      prefix_padding_ms: 300,
      silence_duration_ms: this.options.silenceDurationMs ?? 300,
    };
    this.firstMessageProtectionPending = true;
    try {
      this.ws.send(
        JSON.stringify({
          type: 'session.update',
          session: { turn_detection: null },
        }),
      );
      // DIAG (0.6.1 follow-up): we want to know exactly when the lockout
      // session.update is sent so a live test trace can correlate the
      // ``[DIAG-VAD] session.updated`` echo back from the server. If the
      // echo arrives AFTER the response.create below, the race hypothesis
      // (server applies turn_detection update post-response trigger) is
      // confirmed.
      getLogger().info(
        '[DIAG-VAD] sent session.update turn_detection=null (lockout arm)',
      );
    } catch (err) {
      getLogger().debug?.(
        `sendFirstMessage: turn_detection lockout failed: ${String(err)}`,
      );
      // Clear protection state so the message listener doesn't restore a
      // turn_detection we never actually disabled.
      this.firstMessageProtectionPending = false;
      this.savedTurnDetection = null;
    }
    this.ws.send(JSON.stringify({
      type: 'response.create',
      response: {
        modalities: ['audio', 'text'],
        instructions: `Say exactly the following sentence as your first turn and nothing else: "${text}"`,
      },
    }));
    // DIAG (0.6.1 follow-up): paired with the [DIAG-VAD] lockout line above.
    // The ordering of these two log lines vs the ``session.updated`` echo from
    // the server is the smoking gun for the race-condition hypothesis.
    getLogger().info(
      '[DIAG-VAD] sent response.create (firstMessage)',
    );
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

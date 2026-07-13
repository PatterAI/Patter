/**
 * xAI (Grok) Voice Agent realtime adapter — all-in-one STT + LLM + TTS over
 * WebSocket.
 *
 * Targets `wss://api.x.ai/v1/realtime` (the xAI Voice Agent API,
 * https://docs.x.ai/developers/model-capabilities/audio/voice-agent). The
 * wire protocol is OpenAI-Realtime-shaped — `session.update`,
 * `input_audio_buffer.append`, `conversation.item.create`,
 * `response.create` — with xAI-specific extensions:
 *
 * - GA-style nested audio config: `session.audio.{input,output}.format`
 *   with MIME types (`audio/pcm` / `audio/pcmu` / `audio/pcma`) and a
 *   configurable PCM sample rate (8–48 kHz, default 24 kHz).
 * - Server-side tools (`web_search`, `x_search`, `file_search`, `mcp`)
 *   alongside classic `function` tools.
 * - `audio.input.transcription.language_hint` / `.keyterms` ASR biasing,
 *   updatable mid-session.
 * - `replace` — pronunciation replacement map applied to the spoken audio
 *   only (the transcript keeps the original text).
 * - `reasoning.effort` (`"high"` / `"none"`) on the thinking voice models.
 * - GA event names: `response.output_audio.delta` /
 *   `response.output_audio_transcript.delta` (legacy `response.audio.delta`
 *   names are accepted defensively).
 *
 * The adapter surface — `connect` / `sendAudio` / `onEvent` / `sendText` /
 * `sendFunctionResult` / `cancelResponse` / `close` — matches
 * {@link OpenAIRealtimeAdapter} (and the Ultravox / Gemini Live adapters),
 * so callers can swap providers without touching the handler.
 *
 * Pricing: $0.05/min of session time plus $0.004 per client
 * `conversation.item.create` text message (`xai_realtime` in `pricing.ts`).
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

export const XAI_REALTIME_URL = 'wss://api.x.ai/v1/realtime';
export const XAI_CLIENT_SECRETS_URL = 'https://api.x.ai/v1/realtime/client_secrets';

/**
 * Known xAI Voice Agent model identifiers. `GROK_VOICE_LATEST` always
 * points at the newest model (currently `grok-voice-think-fast-1.0`);
 * `GROK_VOICE_FAST_1_0` is the legacy (deprecated) voice model.
 */
export const XAIRealtimeModel = {
  GROK_VOICE_LATEST: 'grok-voice-latest',
  GROK_VOICE_THINK_FAST_1_0: 'grok-voice-think-fast-1.0',
  GROK_VOICE_FAST_1_0: 'grok-voice-fast-1.0',
} as const;
export type XAIRealtimeModel = (typeof XAIRealtimeModel)[keyof typeof XAIRealtimeModel];

/**
 * Audio wire formats accepted by the xAI Voice Agent API. `AUDIO_PCM` is
 * Linear16 little-endian at a configurable sample rate; `AUDIO_PCMU` /
 * `AUDIO_PCMA` are G.711 μ-law / A-law pinned to 8 kHz (carrier-native
 * for Twilio / Telnyx / Plivo — zero transcode).
 */
export const XAIRealtimeAudioFormat = {
  AUDIO_PCM: 'audio/pcm',
  AUDIO_PCMU: 'audio/pcmu',
  AUDIO_PCMA: 'audio/pcma',
} as const;
export type XAIRealtimeAudioFormat =
  (typeof XAIRealtimeAudioFormat)[keyof typeof XAIRealtimeAudioFormat];

/** PCM sample rates accepted by the Voice Agent API (`audio/pcm` only). */
export const XAIRealtimeSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_22050: 22050,
  HZ_24000: 24000,
  HZ_32000: 32000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type XAIRealtimeSampleRate =
  (typeof XAIRealtimeSampleRate)[keyof typeof XAIRealtimeSampleRate];

/** Tool types accepted in the `session.tools` array. */
export const XAIRealtimeToolType = {
  FUNCTION: 'function',
  WEB_SEARCH: 'web_search',
  X_SEARCH: 'x_search',
  FILE_SEARCH: 'file_search',
  MCP: 'mcp',
} as const;
export type XAIRealtimeToolType =
  (typeof XAIRealtimeToolType)[keyof typeof XAIRealtimeToolType];

/** Callback receiving Patter-normalised realtime events. */
export type XAIRealtimeEventCallback = (type: string, data: unknown) => void | Promise<void>;

/** Constructor options for {@link XAIRealtimeAdapter}. */
export interface XAIRealtimeOptions {
  /** Model. Default `grok-voice-latest`. */
  model?: XAIRealtimeModel | string;
  /** Voice — a built-in voice (e.g. `eve`, the default) or a custom voice ID. */
  voice?: string;
  /** System prompt. */
  instructions?: string;
  /** Response language used in the default instructions. Default `en`. */
  language?: string;
  /**
   * Tools available to the agent. Patter tool dicts
   * (name/description/parameters) are wrapped in the `function` envelope;
   * dicts with an explicit non-`function` `type` (`web_search`, `x_search`,
   * `file_search`, `mcp`) pass through unchanged.
   */
  tools?: Array<Record<string, unknown>>;
  /**
   * Audio wire format for input and output. Default `audio/pcmu`
   * (G.711 μ-law @ 8 kHz — matches the Twilio/Telnyx inbound codec so
   * audio flows through without transcoding).
   */
  audioFormat?: XAIRealtimeAudioFormat | string;
  /** PCM sample rate in Hz (`audio/pcm` only). Default 8000. */
  sampleRate?: XAIRealtimeSampleRate | number;
  /** Reasoning effort on the thinking models: `"high"` (default) or `"none"`. */
  reasoningEffort?: 'high' | 'none';
  /** Server VAD tuning (all optional; server defaults apply when unset). */
  vadThreshold?: number;
  silenceDurationMs?: number;
  prefixPaddingMs?: number;
  idleTimeoutMs?: number;
  /** BCP-47 code biasing ASR transcription (e.g. `"ja"`, `"es-MX"`). */
  languageHint?: string;
  /** Key terms biasing ASR transcription (max 100 × 50 chars). */
  keyterms?: string[];
  /** Assistant playback speed multiplier, 0.7–1.5. */
  outputSpeed?: number;
  /**
   * Pronunciation replacement map — keys are matched case-insensitively
   * (whole-word) in the model output and swapped before TTS; only the
   * spoken audio changes, the transcript keeps the original text.
   */
  replace?: Record<string, string>;
  /**
   * Set to `"grok-transcribe"` to receive streaming input transcription
   * updates (live captions) in addition to the final transcript.
   */
  inputTranscriptionModel?: string;
  /** Override the WS endpoint (e.g. for tests). */
  baseUrl?: string;
}

/**
 * Bridges a Twilio/Telnyx media stream to the xAI Voice Agent API.
 * Handles the full conversation loop: audio in → Grok → audio out.
 * No separate STT/TTS needed.
 */
export class XAIRealtimeAdapter {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'xai_realtime';
  private ws: WebSocket | null = null;
  private readonly eventCallbacks: Set<XAIRealtimeEventCallback> = new Set();
  private messageListenerAttached = false;
  private heartbeat: NodeJS.Timeout | null = null;
  // Track the in-flight assistant item id so we can truncate cleanly on
  // barge-in — parity with the OpenAI Realtime adapter.
  private currentResponseItemId: string | null = null;
  private currentResponseAudioMs = 0;
  // Wall-clock timestamp of the first audio delta of the current response —
  // bounds `audio_end_ms` on truncate to what the caller could plausibly
  // have heard (generation runs 5-10x real-time).
  private currentResponseFirstAudioAt: number | null = null;
  private readonly options: XAIRealtimeOptions;
  // Session start time — xAI bills realtime per minute of session time.
  private sessionStartedAt: number = Date.now();
  // Count of client conversation.item.create messages — billed at
  // $0.004/message on top of the per-minute rate.
  private textInputMessages = 0;

  constructor(
    private readonly apiKey: string,
    options: XAIRealtimeOptions = {},
  ) {
    if (!apiKey) {
      throw new Error('XAIRealtimeAdapter requires a non-empty apiKey');
    }
    if (
      options.outputSpeed !== undefined &&
      (options.outputSpeed < 0.7 || options.outputSpeed > 1.5)
    ) {
      throw new Error(`outputSpeed must be in [0.7, 1.5], got ${options.outputSpeed}`);
    }
    this.options = options;
  }

  /** Elapsed billable session seconds (xAI realtime bills $0.05/min). */
  getSessionSeconds(): number {
    return (Date.now() - this.sessionStartedAt) / 1000;
  }

  /** Client text messages sent this session (billed $0.004/message). */
  getTextInputMessages(): number {
    return this.textInputMessages;
  }

  /**
   * Create a short-lived client secret via `POST /v1/realtime/client_secrets`
   * for browser/mobile connections. The returned `value` is passed by the
   * client either as a Bearer token or in the WebSocket subprotocol as
   * `xai-client-secret.<token>` (browsers cannot set headers).
   * `expiresSeconds` is capped at 3600 by the API; `session` optionally
   * binds an initial session configuration (`model`, `reasoning`).
   */
  static async createEphemeralToken(
    apiKey: string,
    opts: { expiresSeconds?: number; session?: Record<string, unknown> } = {},
  ): Promise<{ value: string; expires_at: number }> {
    const payload: Record<string, unknown> = {
      expires_after: { seconds: opts.expiresSeconds ?? 600 },
    };
    if (opts.session !== undefined) payload.session = opts.session;
    const response = await fetch(XAI_CLIENT_SECRETS_URL, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) {
      throw new Error(
        `xAI create ephemeral token error ${response.status}: ${await response.text()}`,
      );
    }
    return (await response.json()) as { value: string; expires_at: number };
  }

  private buildUrl(): string {
    const base = this.options.baseUrl ?? XAI_REALTIME_URL;
    const model = this.options.model ?? XAIRealtimeModel.GROK_VOICE_LATEST;
    return `${base}?model=${encodeURIComponent(model)}`;
  }

  private buildToolWireFormat(tool: Record<string, unknown>): Record<string, unknown> {
    // Server-side tools (`web_search`, `x_search`, `file_search`, `mcp`)
    // already carry an explicit `type` and pass through unchanged; Patter
    // tool dicts are wrapped in the `function` envelope.
    const toolType = tool.type as string | undefined;
    if (toolType && toolType !== XAIRealtimeToolType.FUNCTION) {
      return { ...tool };
    }
    return {
      type: 'function',
      name: tool.name,
      description: tool.description ?? '',
      parameters: tool.parameters ?? { type: 'object', properties: {} },
    };
  }

  private buildAudioFormat(): Record<string, unknown> {
    const format = this.options.audioFormat ?? XAIRealtimeAudioFormat.AUDIO_PCMU;
    const fmt: Record<string, unknown> = { type: format };
    // `rate` only applies to audio/pcm; the G.711 formats are pinned to
    // 8 kHz server-side.
    if (format === XAIRealtimeAudioFormat.AUDIO_PCM) {
      fmt.rate = this.options.sampleRate ?? XAIRealtimeSampleRate.HZ_8000;
    }
    return fmt;
  }

  /** Build the `session.update` body shared by `connect` and `warmup`. */
  private buildSessionConfig(): Record<string, unknown> {
    const opts = this.options;
    const turnDetection: Record<string, unknown> = { type: 'server_vad' };
    if (opts.vadThreshold !== undefined) turnDetection.threshold = opts.vadThreshold;
    if (opts.silenceDurationMs !== undefined) {
      turnDetection.silence_duration_ms = opts.silenceDurationMs;
    }
    if (opts.prefixPaddingMs !== undefined) {
      turnDetection.prefix_padding_ms = opts.prefixPaddingMs;
    }
    if (opts.idleTimeoutMs !== undefined) turnDetection.idle_timeout_ms = opts.idleTimeoutMs;

    const audioFormat = this.buildAudioFormat();
    const input: Record<string, unknown> = { format: { ...audioFormat } };
    const transcription: Record<string, unknown> = {};
    if (opts.languageHint !== undefined) transcription.language_hint = opts.languageHint;
    if (opts.keyterms?.length) transcription.keyterms = [...opts.keyterms];
    if (opts.inputTranscriptionModel !== undefined) {
      transcription.model = opts.inputTranscriptionModel;
    }
    if (Object.keys(transcription).length > 0) input.transcription = transcription;
    const output: Record<string, unknown> = { format: { ...audioFormat } };
    if (opts.outputSpeed !== undefined) output.speed = opts.outputSpeed;

    const session: Record<string, unknown> = {
      voice: opts.voice ?? 'eve',
      instructions:
        opts.instructions ||
        `You are a helpful voice assistant. Respond in ${opts.language ?? 'en'}. ` +
          'Be concise and natural.',
      turn_detection: turnDetection,
      audio: { input, output },
    };
    if (opts.reasoningEffort !== undefined) {
      session.reasoning = { effort: opts.reasoningEffort };
    }
    if (opts.replace && Object.keys(opts.replace).length > 0) {
      session.replace = { ...opts.replace };
    }
    if (opts.tools?.length) {
      session.tools = opts.tools.map((t) => this.buildToolWireFormat(t));
    }
    return session;
  }

  /**
   * Pre-call WebSocket warmup for the xAI Realtime endpoint: open the WS,
   * wait for `session.created`, apply the production `session.update`,
   * wait for the `session.updated` ack, then close.
   *
   * Billing safety: xAI bills realtime per minute of *conversation* time
   * and per `conversation.item.create` text message. `session.update`
   * neither invokes the model nor creates a conversation item, and the
   * socket is closed within ~1 s. Best-effort: failures logged at debug.
   */
  async warmup(): Promise<void> {
    let ws: WebSocket | null = null;
    try {
      ws = await new Promise<WebSocket>((resolve, reject) => {
        const sock = new WebSocket(this.buildUrl(), {
          headers: { Authorization: `Bearer ${this.apiKey}` },
        });
        const timer = setTimeout(() => {
          try {
            sock.close();
          } catch {
            // ignore
          }
          reject(new Error('xAI Realtime warmup connect timeout'));
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

      try {
        ws.send(JSON.stringify({ type: 'session.update', session: this.buildSessionConfig() }));
      } catch {
        return;
      }

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
      getLogger().debug(`xAI Realtime warmup failed (best-effort): ${String(err)}`);
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
    this.sessionStartedAt = Date.now();
    this.ws = new WebSocket(this.buildUrl(), {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    await new Promise<void>((resolve, reject) => {
      let sessionCreated = false;
      let settled = false;
      const ws = this.ws!;

      const onSetupMessage = (raw: Buffer | string): void => {
        let msg: { type: string; error?: { message?: string; code?: string } };
        try {
          msg = JSON.parse(raw.toString()) as typeof msg;
        } catch (e) {
          getLogger().warn(`xAI Realtime: failed to parse message: ${String(e)}`);
          return;
        }
        // On connect the server sends session.created and
        // conversation.created (in either order) — wait for the session
        // frame, tolerate the conversation frame.
        if (msg.type === 'session.created' && !sessionCreated) {
          sessionCreated = true;
          ws.send(JSON.stringify({ type: 'session.update', session: this.buildSessionConfig() }));
        } else if (msg.type === 'session.updated') {
          cleanup();
          resolve();
        } else if (msg.type === 'error') {
          cleanup();
          try {
            ws.close();
          } catch {
            // ignore
          }
          reject(
            new Error(
              `xAI Realtime setup error: ${msg.error?.message ?? msg.error?.code ?? 'unknown'}`,
            ),
          );
        }
      };

      const onSetupError = (err: Error): void => {
        cleanup();
        try {
          ws.close();
        } catch {
          // ignore
        }
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
        try {
          ws.close();
        } catch {
          // ignore
        }
        reject(new Error('xAI Realtime connect timeout'));
      }, 15000);

      ws.on('message', onSetupMessage);
      ws.on('error', onSetupError);
    });

    this.armHeartbeatAndListener();
  }

  private armHeartbeatAndListener(): void {
    // Keep the WS alive across long silent stretches (carrier NAT timeouts).
    this.heartbeat = setInterval(() => {
      try {
        this.ws?.ping();
      } catch {
        // ignore
      }
    }, 20000);
    this.ensureMessageListener();
  }

  /**
   * Register a listener for Patter-normalised realtime events. All traffic
   * routes through a single persistent handler that fans out to a Set of
   * callbacks; use {@link offEvent} to remove one.
   *
   * Emitted events (parity with `OpenAIRealtimeAdapter.onEvent`):
   * `audio` (Buffer), `transcript_input` (string, final),
   * `transcript_output` (string delta), `speech_started`, `speech_stopped`,
   * `function_call` ({call_id, name, arguments}), `response_done`, `error`.
   */
  onEvent(callback: XAIRealtimeEventCallback): void {
    this.eventCallbacks.add(callback);
    this.ensureMessageListener();
  }

  /** Remove a previously registered {@link onEvent} callback. */
  offEvent(callback: XAIRealtimeEventCallback): void {
    this.eventCallbacks.delete(callback);
  }

  private ensureMessageListener(): void {
    if (this.messageListenerAttached || !this.ws) return;
    this.messageListenerAttached = true;
    const ws = this.ws;

    const dispatch = (type: string, payload: unknown): void => {
      for (const cb of this.eventCallbacks) {
        void Promise.resolve(cb(type, payload)).catch((err) =>
          getLogger().error(`onEvent callback error: ${String(err)}`),
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
        message?: string;
        response?: Record<string, unknown>;
        item?: { id?: string; type?: string };
        item_id?: string;
      };
      try {
        data = JSON.parse(raw.toString()) as typeof data;
      } catch (e) {
        getLogger().warn(`xAI Realtime: failed to parse event message: ${String(e)}`);
        return;
      }
      const t = data.type;
      // GA name plus legacy alias — xAI emits the GA
      // `response.output_audio.delta`.
      if (t === 'response.output_audio.delta' || t === 'response.audio.delta') {
        const buf = Buffer.from(data.delta ?? '', 'base64');
        this.currentResponseAudioMs += this.estimateAudioMs(buf);
        if (this.currentResponseFirstAudioAt === null) {
          this.currentResponseFirstAudioAt = Date.now();
        }
        dispatch('audio', buf);
      } else if (
        t === 'response.output_audio_transcript.delta' ||
        t === 'response.audio_transcript.delta'
      ) {
        dispatch('transcript_output', data.delta);
      } else if (t === 'response.content_part.added' || t === 'response.output_item.added') {
        // Track the in-flight assistant item id for barge-in truncation;
        // skip function_call items (truncating one makes the server reject
        // with an error event).
        const itemType = data.item?.type;
        const itemId = data.item?.id ?? data.item_id ?? null;
        if (itemId && (itemType === undefined || itemType === 'message')) {
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
        dispatch('function_call', {
          call_id: data.call_id,
          name: data.name,
          arguments: data.arguments,
        });
      } else if (t === 'response.done') {
        this.currentResponseItemId = null;
        this.currentResponseAudioMs = 0;
        this.currentResponseFirstAudioAt = null;
        dispatch('response_done', data.response ?? null);
      } else if (t === 'error') {
        dispatch('error', data.error ?? { message: data.message ?? '' });
      }
    });

    ws.on('close', (code, reason) => {
      if (code !== 1000) {
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

  private estimateAudioMs(chunk: Buffer): number {
    // Rough audio duration estimate used for truncation accounting:
    // G.711 (pcmu/pcma) is 8 kHz × 1 byte/sample → ms = bytes/8;
    // audio/pcm is 2 bytes/sample at the configured rate.
    if (chunk.length === 0) return 0;
    const format = this.options.audioFormat ?? XAIRealtimeAudioFormat.AUDIO_PCMU;
    if (
      format === XAIRealtimeAudioFormat.AUDIO_PCMU ||
      format === XAIRealtimeAudioFormat.AUDIO_PCMA
    ) {
      return Math.floor(chunk.length / 8);
    }
    const rate = Number(this.options.sampleRate ?? XAIRealtimeSampleRate.HZ_8000);
    const bytesPerMs = Math.max(Math.floor((rate * 2) / 1000), 1);
    return Math.floor(chunk.length / bytesPerMs);
  }

  /** Append a base64-encoded audio chunk to the realtime input buffer. */
  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(
      JSON.stringify({ type: 'input_audio_buffer.append', audio: audio.toString('base64') }),
    );
  }

  /**
   * Inject a user text turn and request a new response.
   *
   * Note: xAI bills each client `conversation.item.create` at
   * $0.004/message on top of the per-minute session rate.
   */
  async sendText(text: string): Promise<void> {
    this.textInputMessages += 1;
    this.ws?.send(
      JSON.stringify({
        type: 'conversation.item.create',
        item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] },
      }),
    );
    this.ws?.send(JSON.stringify({ type: 'response.create' }));
  }

  /**
   * Make the agent speak `text` verbatim as its opening line. Seeds the
   * greeting as an ASSISTANT history item and triggers a response —
   * injecting it as `role: user` would make the model *reply to* its own
   * greeting (role-confused openings).
   */
  async sendFirstMessage(text: string): Promise<void> {
    if (!this.ws) return;
    this.textInputMessages += 1;
    this.ws.send(
      JSON.stringify({
        type: 'conversation.item.create',
        item: { type: 'message', role: 'assistant', content: [{ type: 'text', text }] },
      }),
    );
    this.ws.send(
      JSON.stringify({
        type: 'response.create',
        response: {
          instructions: `Say exactly the following sentence as your first turn and nothing else: "${text}"`,
        },
      }),
    );
  }

  /** Trigger `response.create` with no new user item. */
  async requestResponse(): Promise<void> {
    this.ws?.send(JSON.stringify({ type: 'response.create' }));
  }

  /** Return a function call result and trigger a new response. */
  async sendFunctionResult(callId: string, result: string): Promise<void> {
    this.ws?.send(
      JSON.stringify({
        type: 'conversation.item.create',
        item: { type: 'function_call_output', call_id: callId, output: result },
      }),
    );
    this.ws?.send(JSON.stringify({ type: 'response.create' }));
  }

  /**
   * Apply a mid-session config swap (multi-agent handoff, ASR biasing).
   * Sends a partial `session.update` carrying only the supplied fields —
   * the server merges partial updates. The supplied values are recorded on
   * the adapter so reconnect/warmup paths rebuild the post-swap session.
   */
  async updateSession(update: {
    instructions?: string;
    tools?: Array<Record<string, unknown>>;
    languageHint?: string;
    keyterms?: string[];
    replace?: Record<string, string>;
  }): Promise<void> {
    const session: Record<string, unknown> = {};
    if (update.instructions !== undefined) {
      this.options.instructions = update.instructions;
      session.instructions = update.instructions;
    }
    if (update.tools !== undefined) {
      this.options.tools = update.tools;
      session.tools = update.tools.map((t) => this.buildToolWireFormat(t));
    }
    const transcription: Record<string, unknown> = {};
    if (update.languageHint !== undefined) {
      this.options.languageHint = update.languageHint;
      transcription.language_hint = update.languageHint;
    }
    if (update.keyterms !== undefined) {
      this.options.keyterms = update.keyterms;
      transcription.keyterms = [...update.keyterms];
    }
    if (Object.keys(transcription).length > 0) {
      session.audio = { input: { transcription } };
    }
    if (update.replace !== undefined) {
      this.options.replace = update.replace;
      session.replace = { ...update.replace };
    }
    if (Object.keys(session).length === 0 || !this.ws) return;
    this.ws.send(JSON.stringify({ type: 'session.update', session }));
  }

  /**
   * Truncate the in-flight assistant item to what the caller heard.
   * Sends `conversation.item.truncate` with `audio_end_ms` bounded by
   * wall-clock time since the first audio delta — generation runs 5-10x
   * real-time, so the byte-derived counter overstates what a 1x playback
   * could have produced. No-op when no response is in flight.
   */
  truncate(): void {
    if (!this.ws || !this.currentResponseItemId) return;
    let audioEndMs = this.currentResponseAudioMs;
    if (this.currentResponseFirstAudioAt !== null) {
      const elapsedMs = Date.now() - this.currentResponseFirstAudioAt;
      audioEndMs = Math.min(audioEndMs, Math.max(elapsedMs, 0));
    }
    try {
      this.ws.send(
        JSON.stringify({
          type: 'conversation.item.truncate',
          item_id: this.currentResponseItemId,
          content_index: 0,
          audio_end_ms: audioEndMs,
        }),
      );
    } catch (err) {
      getLogger().debug?.(`conversation.item.truncate failed: ${String(err)}`);
    }
    this.currentResponseItemId = null;
    this.currentResponseAudioMs = 0;
    this.currentResponseFirstAudioAt = null;
  }

  /**
   * Cancel the current response AND truncate the in-flight item. In
   * server-VAD mode xAI interrupts automatically on barge-in; this explicit
   * path covers manual cancels (guardrail rewrites, transfer / hangup).
   * Idempotent: no-ops when no response is in flight.
   */
  cancelResponse(): void {
    if (!this.ws || !this.currentResponseItemId) return;
    this.truncate();
    this.ws.send(JSON.stringify({ type: 'response.cancel' }));
  }

  /** Close the connection and stop the heartbeat. */
  async close(): Promise<void> {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
    this.messageListenerAttached = false;
  }
}

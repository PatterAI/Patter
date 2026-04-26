import WebSocket from 'ws';
import { getLogger } from '../logger';

/**
 * Supported OpenAI Realtime wire audio formats. See
 * https://platform.openai.com/docs/guides/realtime for the full list.
 * ``g711_ulaw`` matches what Twilio/Telnyx emit natively on the phone leg,
 * so no transcoding is needed. ``pcm16`` is used in the terminal test-mode
 * path and when the telephony provider negotiates L16/16000.
 */
export type OpenAIRealtimeAudioFormat = 'g711_ulaw' | 'g711_alaw' | 'pcm16';

export type RealtimeEventCallback = (type: string, data: unknown) => void | Promise<void>;

export interface OpenAIRealtimeOptions {
  temperature?: number;
  maxResponseOutputTokens?: number | 'inf';
  modalities?: string[];
  toolChoice?: string | Record<string, unknown>;
  inputAudioTranscriptionModel?: string;
  vadType?: 'server_vad' | 'semantic_vad';
}

export class OpenAIRealtimeAdapter {
  private ws: WebSocket | null = null;
  private readonly eventCallbacks: Set<RealtimeEventCallback> = new Set();
  private messageListenerAttached = false;
  private heartbeat: NodeJS.Timeout | null = null;
  // Track the in-flight assistant item id so we can truncate cleanly on
  // barge-in (see ``cancelResponse``) — matches the Python adapter.
  private currentResponseItemId: string | null = null;
  private currentResponseAudioMs = 0;
  private readonly options: OpenAIRealtimeOptions;

  constructor(
    private readonly apiKey: string,
    private readonly model: string = 'gpt-realtime-mini',
    private readonly voice: string = 'alloy',
    private readonly instructions: string = '',
    private readonly tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>,
    // Audio wire format negotiated with OpenAI Realtime. Mirrors the Python
    // ``audio_format`` kwarg. Default ``g711_ulaw`` matches the Twilio/Telnyx
    // inbound codec so audio flows through without transcoding.
    private readonly audioFormat: OpenAIRealtimeAudioFormat = 'g711_ulaw',
    options: OpenAIRealtimeOptions = {},
  ) {
    this.options = options;
  }

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
          const config: Record<string, unknown> = {
            input_audio_format: this.audioFormat,
            output_audio_format: this.audioFormat,
            voice: this.voice,
            instructions: this.instructions || 'You are a helpful voice assistant. Be concise.',
            turn_detection: {
              type: this.options.vadType ?? 'server_vad',
              threshold: 0.5,
              prefix_padding_ms: 300,
              silence_duration_ms: 500,
            },
            input_audio_transcription: { model: this.options.inputAudioTranscriptionModel ?? 'whisper-1' },
          };
          if (this.options.temperature !== undefined) config.temperature = this.options.temperature;
          if (this.options.maxResponseOutputTokens !== undefined) {
            config.max_response_output_tokens = this.options.maxResponseOutputTokens;
          }
          if (this.options.modalities !== undefined) config.modalities = this.options.modalities;
          if (this.options.toolChoice !== undefined) config.tool_choice = this.options.toolChoice;
          if (this.tools?.length) {
            config.tools = this.tools.map(t => ({
              type: 'function',
              name: t.name,
              description: t.description,
              parameters: t.parameters,
            }));
          }
          ws.send(JSON.stringify({ type: 'session.update', session: config }));
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
      if (t === 'response.audio.delta') {
        const buf = Buffer.from(data.delta ?? '', 'base64');
        this.currentResponseAudioMs += estimateAudioMs(buf, this.audioFormat);
        dispatch('audio', buf);
      } else if (t === 'response.audio_transcript.delta') {
        dispatch('transcript_output', data.delta);
      } else if (t === 'response.content_part.added' || t === 'response.output_item.added') {
        const itemId = data.item?.id ?? data.item_id ?? null;
        if (itemId) {
          this.currentResponseItemId = itemId;
          this.currentResponseAudioMs = 0;
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

  cancelResponse(): void {
    if (!this.ws) return;
    // Truncate the in-flight assistant item first so the transcript stays
    // consistent with the audio the caller actually heard. Without this,
    // ``response.cancel`` alone can leave ghost text on the next turn.
    if (this.currentResponseItemId) {
      try {
        this.ws.send(JSON.stringify({
          type: 'conversation.item.truncate',
          item_id: this.currentResponseItemId,
          content_index: 0,
          audio_end_ms: this.currentResponseAudioMs,
        }));
      } catch (err) {
        getLogger().debug?.(`conversation.item.truncate failed: ${String(err)}`);
      }
    }
    this.ws.send(JSON.stringify({ type: 'response.cancel' }));
  }

  async sendText(text: string): Promise<void> {
    this.ws?.send(JSON.stringify({
      type: 'conversation.item.create',
      item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] },
    }));
    this.ws?.send(JSON.stringify({ type: 'response.create' }));
  }

  async sendFunctionResult(callId: string, result: string): Promise<void> {
    this.ws?.send(JSON.stringify({
      type: 'conversation.item.create',
      item: { type: 'function_call_output', call_id: callId, output: result },
    }));
    this.ws?.send(JSON.stringify({ type: 'response.create' }));
  }

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
  if (format === 'g711_ulaw' || format === 'g711_alaw') return Math.floor(chunk.length / 8);
  if (format === 'pcm16') {
    // PCM16 at 24 kHz (OpenAI Realtime default): 2 bytes/sample, 24 samples/ms
    // → 48 bytes/ms. The previous divisor of 32 assumed 16 kHz which under-
    // estimated duration by 33% and inflated the apparent audio-send rate.
    // Note: session.created does not expose the negotiated sample rate in the
    // current OpenAI Realtime API, so 24 kHz is hardcoded as the known default.
    return Math.floor(chunk.length / 48);
  }
  return 0;
}

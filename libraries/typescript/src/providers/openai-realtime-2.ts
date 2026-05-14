/**
 * OpenAI Realtime adapter for the GA Realtime API (`gpt-realtime-2`).
 *
 * `gpt-realtime-2` is served from the same `wss://api.openai.com/v1/realtime`
 * endpoint as the v1-beta family, but the GA endpoint:
 *   - REJECTS the legacy `OpenAI-Beta: realtime=v1` header (returns
 *     `invalid_model` with message "Model X is only available on the GA API").
 *   - REQUIRES `session.type === "realtime"` at the root of `session.update`.
 *   - Uses `output_modalities` (was `modalities`).
 *   - Nests audio config under `audio.{input,output}` with MIME `type`
 *     strings (`audio/pcmu`, `audio/pcma`, `audio/pcm`) instead of the v1
 *     enum strings (`g711_ulaw`, `g711_alaw`, `pcm16`) and moves `voice`
 *     under `audio.output.voice`, `transcription` + `turn_detection`
 *     under `audio.input`.
 *
 * Everything ELSE (event names, audio delta dispatch, barge-in / truncate
 * semantics, heartbeat, tool calling) is API-compatible with the v1 family,
 * so this adapter subclasses {@link OpenAIRealtimeAdapter} and overrides
 * only `connect()`. The runtime behaviour (`sendAudio`, `cancelResponse`,
 * `sendText`, `sendFirstMessage`, …) is inherited unchanged.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';
import {
  OpenAIRealtimeAdapter,
  OpenAIRealtimeAudioFormat,
  OpenAIRealtimeVADType,
  OpenAITranscriptionModel,
} from './openai-realtime';

/**
 * Mapping from GA Realtime event names back to the v1 names the rest of
 * Patter (`StreamHandler`, metrics, dashboard) listens for. The GA API
 * renamed several events but kept payload shapes identical, so we can
 * translate at the WebSocket boundary and reuse the v1 message handler
 * untouched. Empty target means "pass through unchanged".
 */
const GA_TO_V1_EVENT_NAMES: Readonly<Record<string, string>> = {
  'response.output_audio.delta': 'response.audio.delta',
  'response.output_audio.done': 'response.audio.done',
  'response.output_audio_transcript.delta': 'response.audio_transcript.delta',
  'response.output_audio_transcript.done': 'response.audio_transcript.done',
};

/** Realtime WebSocket adapter speaking OpenAI's GA Realtime API. */
export class OpenAIRealtime2Adapter extends OpenAIRealtimeAdapter {
  /** Map Patter wire-format enums to GA-API MIME types. */
  private gaFormatMime(): string {
    if (this.audioFormat === OpenAIRealtimeAudioFormat.G711_ULAW) return 'audio/pcmu';
    if (this.audioFormat === OpenAIRealtimeAudioFormat.G711_ALAW) return 'audio/pcma';
    return 'audio/pcm';
  }

  /** GA-shape `session.update` payload. See module-level docstring. */
  private buildGASessionConfig(): Record<string, unknown> {
    const opts = this.options;
    const fmt = { type: this.gaFormatMime() };
    const config: Record<string, unknown> = {
      type: 'realtime',
      output_modalities: opts.modalities ?? ['audio'],
      audio: {
        input: {
          format: fmt,
          transcription: {
            model: opts.inputAudioTranscriptionModel ?? OpenAITranscriptionModel.WHISPER_1,
          },
          turn_detection: {
            type: opts.vadType ?? OpenAIRealtimeVADType.SERVER_VAD,
            threshold: 0.5,
            prefix_padding_ms: 300,
            silence_duration_ms: opts.silenceDurationMs ?? 300,
          },
        },
        output: {
          format: fmt,
          voice: this.voice,
        },
      },
      instructions: this.instructions || 'You are a helpful voice assistant. Be concise.',
    };
    if (opts.temperature !== undefined) config.temperature = opts.temperature;
    if (opts.maxResponseOutputTokens !== undefined) {
      config.max_output_tokens = opts.maxResponseOutputTokens;
    }
    if (opts.toolChoice !== undefined) config.tool_choice = opts.toolChoice;
    if (opts.reasoningEffort !== undefined) {
      config.reasoning = { effort: opts.reasoningEffort };
    }
    if (this.tools?.length) {
      config.tools = this.tools.map((t) => {
        const def: Record<string, unknown> = {
          type: 'function',
          name: t.name,
          description: t.description,
          parameters: t.parameters,
        };
        if ((t as { strict?: boolean }).strict === true) def.strict = true;
        return def;
      });
    }
    return config;
  }

  /**
   * Open the Realtime WebSocket against the GA endpoint and apply the GA
   * session configuration. Header `OpenAI-Beta: realtime=v1` is OMITTED
   * (the GA endpoint rejects it). Wire shape uses nested `audio.{input,
   * output}` + `output_modalities` + `session.type === "realtime"`.
   */
  async connect(): Promise<void> {
    const url = `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(this.model)}`;
    this.ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    // Install a wire-level translation shim BEFORE any listener is
    // attached. The shim intercepts every incoming WS frame, parses the
    // JSON, and rewrites the `type` field from the GA event names to the
    // v1 names that the parent `ensureMessageListener` (and downstream
    // `StreamHandler`) recognise. Payloads are byte-identical, so a
    // simple rename is sufficient — `response.output_audio.delta.delta`
    // is the same base64 audio chunk as the v1 `response.audio.delta.delta`.
    // Without this, the GA event types fall through to the catch-all
    // (no-op) branch of the parent dispatcher and audio is silently
    // dropped — manifesting as a "successful" call with zero audio bytes
    // forwarded to Twilio/Telnyx.
    const originalEmit = this.ws.emit.bind(this.ws);
    this.ws.emit = (event: string, ...args: unknown[]): boolean => {
      if (event === 'message' && args.length > 0) {
        const raw = args[0];
        try {
          const text = typeof raw === 'string' ? raw : (raw as Buffer).toString();
          const parsed = JSON.parse(text) as { type?: string };
          const t = parsed.type;
          if (t && t in GA_TO_V1_EVENT_NAMES) {
            parsed.type = GA_TO_V1_EVENT_NAMES[t];
            return originalEmit(event, Buffer.from(JSON.stringify(parsed)), ...args.slice(1));
          }
        } catch {
          /* not JSON or parse failed — pass through */
        }
      }
      return originalEmit(event, ...args);
    };

    await new Promise<void>((resolve, reject) => {
      let sessionCreated = false;
      let settled = false;
      const ws = this.ws!;

      const onSetupMessage = (raw: Buffer | string): void => {
        let msg: { type: string; error?: { message?: string } };
        try {
          msg = JSON.parse(raw.toString()) as { type: string; error?: { message?: string } };
        } catch (e) {
          getLogger().warn(`OpenAI Realtime 2: failed to parse message: ${String(e)}`);
          return;
        }
        if (msg.type === 'session.created' && !sessionCreated) {
          sessionCreated = true;
          ws.send(JSON.stringify({ type: 'session.update', session: this.buildGASessionConfig() }));
        } else if (msg.type === 'session.updated') {
          cleanup();
          resolve();
        } else if (msg.type === 'error') {
          // Surface real GA-side rejection ("invalid_model",
          // "missing_required_parameter") so the caller doesn't wait 15 s
          // for a meaningless timeout.
          cleanup();
          try { ws.close(); } catch { /* ignore */ }
          reject(new Error(`OpenAI Realtime 2 setup error: ${msg.error?.message ?? JSON.stringify(msg)}`));
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
        reject(new Error('OpenAI Realtime 2 connect timeout'));
      }, 15000);

      ws.on('message', onSetupMessage);
      ws.on('error', onSetupError);
    });

    this.armHeartbeatAndListener();
  }

  /**
   * GA-API variant of {@link OpenAIRealtimeAdapter.sendFirstMessage}. Two
   * differences from the v1 path:
   *
   * 1. The v1 implementation sends `response.modalities` which the GA
   *    endpoint rejects with `Unknown parameter: 'response.modalities'`.
   *    Use `output_modalities` to match the GA `session.update` shape.
   *
   * 2. The GA `response.create` does NOT inherit `audio.output.voice`
   *    from the session — it falls back to the server-side default
   *    (`marin`, female) when the field is omitted on the response
   *    itself. Session-level `voice: "alloy"` only affects subsequent
   *    server-VAD-triggered responses, NOT this explicit
   *    `response.create`. We re-inject the configured voice here so the
   *    first-message voice matches the rest of the call.
   */
  async sendFirstMessage(text: string): Promise<void> {
    // Bypass reasoning for the first message: this is a literal "say
    // exactly X" instruction, not an open question, so the reasoning
    // tier inherited from the session (`reasoningEffort` — typically
    // "low" for production voice) only adds time-to-first-audio without
    // changing the output. Forcing `minimal` here lets the first message
    // start streaming as fast as possible; subsequent VAD-triggered
    // `response.create`s continue to use the session's reasoning tier.
    this.ws?.send(JSON.stringify({
      type: 'response.create',
      response: {
        output_modalities: ['audio'],
        audio: { output: { voice: this.voice } },
        reasoning: { effort: 'minimal' },
        instructions: `Say exactly the following sentence as your first turn and nothing else: "${text}"`,
      },
    }));
  }
}

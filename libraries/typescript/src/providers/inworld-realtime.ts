/**
 * Inworld Realtime WebSocket adapter.
 *
 * Inworld's Realtime API is OpenAI-Realtime-compatible — Inworld documents an
 * "OpenAI Realtime migration" path where the event schema, session structure,
 * and client/server events match OpenAI's Realtime API, so migrating is
 * "swap the endpoint and auth credentials". This adapter therefore SUBCLASSES
 * {@link OpenAIRealtimeAdapter} and overrides ONLY the transport:
 *   - the WebSocket endpoint (`wss://api.inworld.ai/v1/realtime`), and
 *   - the auth header (`Authorization: Bearer <Inworld Realtime key>`).
 *
 * Everything else — the `session.created` → `session.update` → `session.updated`
 * handshake, audio-delta dispatch, barge-in / truncate semantics, heartbeat,
 * tool calling, `sendText` / `sendFirstMessage` / `sendReassurance` — is
 * inherited unchanged. Because it `extends OpenAIRealtimeAdapter`, every
 * `instanceof OpenAIRealtimeAdapter` feature gate in the stream handler fires
 * for Inworld too, with no per-provider branches.
 *
 * Audio: defaults to `g711_ulaw` pass-through (the Twilio/Telnyx carrier wire
 * format), matching OpenAI's v1-beta Realtime shape that Inworld mirrors. If a
 * given Inworld deployment only accepts PCM, pass `audioFormat: 'pcm16'` or
 * front this with a transcode — see the TODO below.
 *
 * TODO(inworld-spec): the exact production wire details (whether Inworld
 * requires a session-scoped `?key=<session-id>` obtained from a prior REST
 * call, and the accepted audio formats) are not fully public. The defaults
 * here are the OpenAI-compatible ones; override `baseUrl` / `audioFormat` per
 * your Inworld account, or extend `connect()` with the session-create step.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';
import {
  OpenAIRealtimeAdapter,
  OpenAIRealtimeAudioFormat,
  type OpenAIRealtimeOptions,
} from './openai-realtime';

/** Default Inworld Realtime WebSocket base URL (no query string). */
export const INWORLD_REALTIME_WS_URL = 'wss://api.inworld.ai/v1/realtime';
/** Default model id — override with the exact id from the Inworld dashboard. */
export const INWORLD_REALTIME_DEFAULT_MODEL = 'inworld-realtime';
/** Default voice — an Inworld voice name (mirrors the Inworld TTS default). */
export const INWORLD_REALTIME_DEFAULT_VOICE = 'Ashley';

/** Constructor options for {@link InworldRealtimeAdapter}. */
export interface InworldRealtimeAdapterOptions extends OpenAIRealtimeOptions {
  /** Realtime model id (passed through as `?model=`). */
  model?: string;
  /** Voice name. */
  voice?: string;
  /** System prompt / instructions. */
  instructions?: string;
  /** Tool/function declarations advertised to the model. */
  tools?: Array<{ name: string; description: string; parameters: Record<string, unknown>; strict?: boolean }>;
  /** Override the WebSocket base URL (no query string). */
  baseUrl?: string;
  /** Wire audio format. Defaults to `g711_ulaw` (carrier-native pass-through). */
  audioFormat?: OpenAIRealtimeAudioFormat;
}

/** Realtime WebSocket adapter for Inworld's OpenAI-compatible Realtime API. */
export class InworldRealtimeAdapter extends OpenAIRealtimeAdapter {
  /** Stable pricing/dashboard key — matches the Inworld TTS provider key. */
  static readonly providerKey = 'inworld';
  private readonly wsBase: string;

  constructor(apiKey: string, opts: InworldRealtimeAdapterOptions = {}) {
    super(
      apiKey,
      opts.model ?? INWORLD_REALTIME_DEFAULT_MODEL,
      opts.voice ?? INWORLD_REALTIME_DEFAULT_VOICE,
      opts.instructions ?? '',
      opts.tools,
      opts.audioFormat ?? OpenAIRealtimeAudioFormat.G711_ULAW,
      // The base reads only the OpenAIRealtimeOptions keys off this object;
      // the Inworld-specific extras (model/voice/baseUrl/...) are ignored there.
      opts,
    );
    this.wsBase = (opts.baseUrl ?? INWORLD_REALTIME_WS_URL).replace(/\/+$/, '');
  }

  /**
   * Open the Inworld Realtime WebSocket and apply the (OpenAI-compatible)
   * session configuration. Same `session.created` → `session.update` →
   * `session.updated` handshake the base v1 adapter performs, but against the
   * Inworld endpoint with a Bearer Realtime key. An explicit `error` frame
   * during setup is surfaced immediately so the caller gets an actionable
   * reason instead of a 15 s timeout.
   */
  async connect(): Promise<void> {
    const url = `${this.wsBase}?model=${encodeURIComponent(this.model)}`;
    this.ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    await new Promise<void>((resolve, reject) => {
      let sessionCreated = false;
      let settled = false;
      const ws = this.ws!;

      const onSetupMessage = (raw: Buffer | string): void => {
        if (settled) return;
        let msg: { type: string; error?: { message?: string } };
        try {
          msg = JSON.parse(raw.toString()) as { type: string; error?: { message?: string } };
        } catch (e) {
          getLogger().warn(`Inworld Realtime: failed to parse message: ${String(e)}`);
          return;
        }
        if (msg.type === 'session.created' && !sessionCreated) {
          sessionCreated = true;
          ws.send(JSON.stringify({ type: 'session.update', session: this.buildSessionConfig() }));
        } else if (msg.type === 'session.updated') {
          cleanup();
          resolve();
        } else if (msg.type === 'error') {
          cleanup();
          try { ws.close(); } catch { /* ignore */ }
          reject(
            new Error(
              `Inworld Realtime setup error: ${msg.error?.message ?? JSON.stringify(msg)}`,
            ),
          );
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
        reject(
          new Error(
            `Inworld Realtime connect timeout (no session.updated within 15s) for ` +
              `model "${this.model}" at ${this.wsBase}. Check the model id, the ` +
              `Realtime API key, and the endpoint.`,
          ),
        );
      }, 15000);

      ws.on('message', onSetupMessage);
      ws.on('error', onSetupError);
    });

    this.armHeartbeatAndListener();
  }

  /**
   * No-op warmup. The base {@link OpenAIRealtimeAdapter.warmup} opens a socket
   * to OpenAI's endpoint, which is wrong for an Inworld key. Inworld is not
   * wired into the prewarm pipeline, so we simply skip warmup here.
   */
  override async warmup(): Promise<void> {
    getLogger().debug('Inworld Realtime: warmup is a no-op (not parked)');
  }

  /**
   * Parking is not supported for Inworld — the base implementation targets the
   * OpenAI endpoint. Reject so any caller treats it as a cache miss and falls
   * through to the cold {@link connect} path.
   */
  override async openParkedConnection(): Promise<WebSocket> {
    throw new Error('Inworld Realtime: openParkedConnection is not supported');
  }
}

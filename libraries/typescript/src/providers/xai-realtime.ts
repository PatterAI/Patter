/**
 * xAI Grok Voice Agent realtime adapter.
 *
 * **Beta: validated against the xAI Voice Agent API spec; not yet exercised
 * against a live phone call.**
 *
 * xAI's Voice Agent API is OpenAI-Realtime-GA-compatible — xAI documents an
 * "Migrating from OpenAI Realtime" path where only the base URL, the API key,
 * and the model change; the GA `session.update` / `response.create` / streaming
 * event flow works as-is. This adapter therefore SUBCLASSES
 * {@link OpenAIRealtime2Adapter} (the GA adapter) and overrides only:
 *   - {@link realtimeBaseUrl} → `wss://api.x.ai/v1/realtime`, and
 *   - {@link buildGASessionConfig} → grafts the xAI-specific `session.update`
 *     keys (reasoning effort, language hint, keyterms, output speed,
 *     pronunciation `replace`, session `resumption`, extra turn-detection
 *     knobs) on top of the inherited GA body, plus appends any raw
 *     server-side tools.
 *
 * Everything else — the `session.created` → `session.update` → `session.updated`
 * handshake, the GA event-name translation shim, barge-in / truncate, heartbeat,
 * tool calling, and the inherited PCM-24 ↔ mulaw-8 telephony transcode — is
 * reused unchanged. Because it `instanceof OpenAIRealtimeAdapter`, every feature
 * gate in the stream handler fires for xAI with no per-provider branches.
 *
 * Audio (v1): inherits the GA `audio/pcm` @ 24 kHz path and the outbound
 * PCM24→mulaw8 transcode. xAI supports native `audio/pcmu` @ 8 kHz which would
 * let telephony calls skip the transcode entirely — that is a flagged
 * follow-up optimisation, NOT implemented here.
 */

import type WebSocket from 'ws';
import { getLogger } from '../logger';
import {
  OpenAIRealtimeAudioFormat,
  type OpenAIRealtimeOptions,
} from './openai-realtime';
import { OpenAIRealtime2Adapter } from './openai-realtime-2';
import { XAI_DEFAULT_VOICE, normalizeXaiVoice } from './xai-voices';

/** Default xAI Voice Agent WebSocket base URL (no query string). */
export const XAI_REALTIME_WS_URL = 'wss://api.x.ai/v1/realtime';
/** Default model — `grok-voice-latest` always points at the newest voice model. */
export const XAI_REALTIME_DEFAULT_MODEL = 'grok-voice-latest';
/** Default voice — xAI's built-in `eve` (case-insensitive). Alias of {@link XAI_DEFAULT_VOICE}. */
export const XAI_REALTIME_DEFAULT_VOICE = XAI_DEFAULT_VOICE;
/**
 * xAI's input-transcription model. Setting it keeps the session xAI-valid —
 * the GA base defaults to OpenAI's `whisper-1`, which the xAI endpoint rejects.
 * An explicit `inputAudioTranscriptionModel` from the caller still wins.
 */
export const XAI_REALTIME_TRANSCRIPTION_MODEL = 'grok-transcribe';

/**
 * Constructor options for {@link XaiRealtimeAdapter}.
 *
 * Extends the base {@link OpenAIRealtimeOptions} (VAD tuning, transcription
 * language, response gating, …) minus `reasoningEffort` — xAI's reasoning
 * field accepts only `"high"` / `"none"`, a different set from OpenAI's tiers —
 * and adds the xAI-specific `session.update` knobs. All optional with safe
 * defaults; unset knobs are omitted from the wire so xAI applies its own
 * server defaults.
 */
export interface XaiRealtimeAdapterOptions
  extends Omit<OpenAIRealtimeOptions, 'reasoningEffort'> {
  /** Voice-agent model id (passed through as `?model=`). */
  model?: string;
  /** Voice id (built-in or custom). */
  voice?: string;
  /** System prompt / instructions. */
  instructions?: string;
  /** Tool/function declarations advertised to the model. */
  tools?: Array<{ name: string; description: string; parameters: Record<string, unknown>; strict?: boolean }>;
  /** Override the WebSocket base URL (no query string). */
  baseUrl?: string;
  /** Wire audio format. Defaults to `g711_ulaw` (carrier-native pass-through). */
  audioFormat?: OpenAIRealtimeAudioFormat;
  /**
   * Reasoning effort. `"high"` (xAI server default) enables reasoning;
   * `"none"` disables it. Omitted from the wire when unset → server default.
   * Sent as `session.reasoning.effort`.
   */
  reasoningEffort?: 'high' | 'none';
  /**
   * Server VAD activation threshold in `[0.1, 0.9]` (xAI default `0.85`).
   * Higher requires louder audio to trigger. Sent as
   * `turn_detection.threshold`. Omitted when unset.
   */
  vadThreshold?: number;
  /**
   * Audio (ms) included before detected speech start, `[0, 10000]` (xAI
   * default `333`). Sent as `turn_detection.prefix_padding_ms`. Omitted when
   * unset.
   */
  prefixPaddingMs?: number;
  /**
   * When set, the server re-engages the user after this many ms of silence
   * following an assistant turn. Sent as `turn_detection.idle_timeout_ms`.
   * Omitted when unset.
   */
  idleTimeoutMs?: number;
  /**
   * BCP-47 code biasing input transcription toward one language. Sent as
   * `audio.input.transcription.language_hint`. Omitted when unset.
   */
  languageHint?: string;
  /**
   * Up to 100 key terms (≤50 chars each) biasing transcription toward
   * domain vocabulary. Sent as `audio.input.transcription.keyterms`. Omitted
   * when empty.
   */
  keyterms?: readonly string[];
  /**
   * Assistant playback speed multiplier in `[0.7, 1.5]` (default `1.0`). Sent
   * as `audio.output.speed`. Omitted when unset.
   */
  speed?: number;
  /**
   * Pronunciation map applied to the model's output before TTS, e.g.
   * `{ "Acme Mobile": "Acme Mobull" }`. Only the spoken audio changes; the
   * transcript keeps the original. Sent as `session.replace`. Omitted when unset.
   */
  replace?: Readonly<Record<string, string>>;
  /**
   * Opt in to session resumption — the server caches conversation turns and
   * replays them on reconnect. Sent as `session.resumption.enabled`. Defaults
   * to `false` (omitted).
   */
  resumption?: boolean;
  /**
   * Raw xAI server-side tool objects appended verbatim to `session.tools`
   * (e.g. `{ type: 'web_search' }`, `{ type: 'x_search' }`,
   * `{ type: 'mcp', server_url, server_label }`, `{ type: 'file_search', … }`).
   * These execute server-side at xAI — the client never handles their results.
   * Defaults to empty.
   */
  serverTools?: ReadonlyArray<Record<string, unknown>>;
}

/**
 * Realtime adapter for xAI's OpenAI-GA-compatible Grok Voice Agent API.
 *
 * @example
 * ```ts
 * import { Patter, Twilio, XaiRealtime } from "getpatter";
 *
 * const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+1..." });
 * const agent = phone.agent({
 *   engine: new XaiRealtime({ voice: "eve" }), // reads XAI_API_KEY
 *   systemPrompt: "You are a friendly receptionist.",
 *   firstMessage: "Hello! How can I help?",
 * });
 * ```
 */
export class XaiRealtimeAdapter extends OpenAIRealtime2Adapter {
  /** Stable pricing/dashboard key. */
  static readonly providerKey = 'xai_realtime';

  private readonly wsBase: string;
  private readonly xaiReasoningEffort?: 'high' | 'none';
  private readonly vadThreshold?: number;
  private readonly prefixPaddingMs?: number;
  private readonly idleTimeoutMs?: number;
  private readonly languageHint?: string;
  private readonly keyterms: readonly string[];
  private readonly outputSpeed?: number;
  private readonly replaceMap?: Readonly<Record<string, string>>;
  private readonly resumptionEnabled: boolean;
  private readonly serverTools: ReadonlyArray<Record<string, unknown>>;

  constructor(apiKey: string, opts: XaiRealtimeAdapterOptions = {}) {
    // Forward only the base OpenAIRealtimeOptions keys to the GA adapter —
    // `reasoningEffort` is intentionally EXCLUDED (xAI's "high"/"none" values
    // differ from OpenAI's tiers) and grafted in the session-config override.
    const baseOptions: OpenAIRealtimeOptions = {
      temperature: opts.temperature,
      maxResponseOutputTokens: opts.maxResponseOutputTokens,
      modalities: opts.modalities,
      toolChoice: opts.toolChoice,
      // Default the transcription model to xAI's `grok-transcribe` — the GA
      // base otherwise emits OpenAI's `whisper-1`, which xAI rejects. Matches
      // the Python adapter (`XAI_TRANSCRIPTION_MODEL`).
      inputAudioTranscriptionModel:
        opts.inputAudioTranscriptionModel ?? XAI_REALTIME_TRANSCRIPTION_MODEL,
      transcriptionLanguage: opts.transcriptionLanguage,
      vadType: opts.vadType,
      silenceDurationMs: opts.silenceDurationMs,
      noiseReduction: opts.noiseReduction,
      turnDetection: opts.turnDetection,
      gateResponseOnTranscript: opts.gateResponseOnTranscript,
    };
    super(
      apiKey,
      opts.model ?? XAI_REALTIME_DEFAULT_MODEL,
      // Normalized before it reaches the base adapter so the eventual
      // `session.update`'s `audio.output.voice` is already wire-correct.
      normalizeXaiVoice(opts.voice ?? XAI_REALTIME_DEFAULT_VOICE),
      opts.instructions ?? '',
      opts.tools,
      opts.audioFormat ?? OpenAIRealtimeAudioFormat.G711_ULAW,
      baseOptions,
    );
    this.wsBase = (opts.baseUrl ?? XAI_REALTIME_WS_URL).replace(/\/+$/, '');
    this.xaiReasoningEffort = opts.reasoningEffort;
    this.vadThreshold = opts.vadThreshold;
    this.prefixPaddingMs = opts.prefixPaddingMs;
    this.idleTimeoutMs = opts.idleTimeoutMs;
    this.languageHint = opts.languageHint;
    this.keyterms = opts.keyterms ?? [];
    this.outputSpeed = opts.speed;
    this.replaceMap = opts.replace;
    this.resumptionEnabled = opts.resumption ?? false;
    this.serverTools = opts.serverTools ?? [];
  }

  /** xAI Voice Agent endpoint — overrides the inherited OpenAI GA URL. */
  protected override realtimeBaseUrl(): string {
    return this.wsBase;
  }

  /**
   * GA-shape `session.update` body with the xAI-specific keys grafted on.
   *
   * Calls the inherited GA builder (which already emits the nested
   * `audio.{input,output}.format`, `turn_detection`, `voice`, `instructions`,
   * and function `tools`), then adds the xAI extensions ONLY when the caller
   * configured them so xAI's own server defaults apply otherwise.
   */
  protected override buildGASessionConfig(): Record<string, unknown> {
    const config = super.buildGASessionConfig();

    // Reasoning: xAI accepts "high" | "none". Grafted here (not inherited)
    // because the base option type carries OpenAI's tiers, not xAI's values.
    if (this.xaiReasoningEffort !== undefined) {
      config.reasoning = { effort: this.xaiReasoningEffort };
    }

    const audio = config.audio as
      | { input?: Record<string, unknown>; output?: Record<string, unknown> }
      | undefined;
    const audioInput = audio?.input;

    // Transcription biasing (language hint + keyterms) nests under
    // audio.input.transcription, which the base builder always emits.
    if (audioInput) {
      const transcription = (audioInput.transcription ??= {}) as Record<string, unknown>;
      // xAI biases ASR via `language_hint` (BCP-47), NOT OpenAI's `language`.
      // Drop the OpenAI-only key if the base builder emitted it (matches the
      // Python adapter, which pops `transcription["language"]`).
      delete transcription.language;
      if (this.languageHint !== undefined) {
        transcription.language_hint = this.languageHint;
      }
      if (this.keyterms.length > 0) {
        transcription.keyterms = [...this.keyterms];
      }
      // Extra turn-detection knobs overlay the base turn_detection dict.
      const turnDetection = audioInput.turn_detection as Record<string, unknown> | undefined;
      if (turnDetection) {
        if (this.vadThreshold !== undefined) turnDetection.threshold = this.vadThreshold;
        if (this.prefixPaddingMs !== undefined) {
          turnDetection.prefix_padding_ms = this.prefixPaddingMs;
        }
        if (this.idleTimeoutMs !== undefined) {
          turnDetection.idle_timeout_ms = this.idleTimeoutMs;
        }
      }
    }

    // Output playback speed nests under audio.output.
    if (this.outputSpeed !== undefined && audio?.output) {
      audio.output.speed = this.outputSpeed;
    }

    // Pronunciation replacements + session resumption live at the session root.
    if (this.replaceMap !== undefined) {
      config.replace = { ...this.replaceMap };
    }
    if (this.resumptionEnabled) {
      config.resumption = { enabled: true };
    }

    // Server-side tools (web_search / x_search / mcp / file_search) append to
    // whatever function tools the base already advertised.
    if (this.serverTools.length > 0) {
      const existing = Array.isArray(config.tools)
        ? (config.tools as unknown[])
        : [];
      config.tools = [...existing, ...this.serverTools.map((t) => ({ ...t }))];
    }

    return config;
  }

  /**
   * No-op warmup. The base {@link OpenAIRealtimeAdapter.warmup} opens a socket
   * to OpenAI's endpoint (wrong host for an xAI key). xAI is not wired into the
   * prewarm pipeline, so warmup is skipped.
   */
  override async warmup(): Promise<void> {
    getLogger().debug('xAI Realtime: warmup is a no-op (not parked)');
  }

  /**
   * Parking is not supported for xAI — the base park path would target the
   * OpenAI edge keepalive. Reject so any caller treats it as a cache miss and
   * falls through to the cold {@link connect} path.
   */
  override async openParkedConnection(): Promise<WebSocket> {
    throw new Error('xAI Realtime: openParkedConnection is not supported');
  }
}

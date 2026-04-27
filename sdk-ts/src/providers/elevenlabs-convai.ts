import WebSocket from 'ws';
import { getLogger } from '../logger';

const ELEVENLABS_CONVAI_URL = 'wss://api.elevenlabs.io/v1/convai/conversation';
const ELEVENLABS_SIGNED_URL =
  'https://api.elevenlabs.io/v1/convai/conversation/get-signed-url';

// Silence threshold: emit `response_done` if the agent stops producing audio
// chunks for this many ms after `agent_response`.
const AGENT_SILENCE_MS = 500;

export interface ElevenLabsConvAIOptions {
  apiKey: string;
  agentId?: string;
  voiceId?: string;
  modelId?: string;
  language?: string;
  firstMessage?: string;
  outputAudioFormat?: string;
  inputAudioFormat?: string;
  useSignedUrl?: boolean;
}

type EventCallback = (type: string, data: unknown) => void | Promise<void>;

export class ElevenLabsConvAIAdapter {
  private ws: WebSocket | null = null;
  private eventCallback: EventCallback | null = null;

  private readonly apiKey: string;
  private readonly agentId: string;
  private readonly voiceId: string;
  // Exposed for parity with Python SDK (`self.model_id`). ConvAI does not
  // accept a client-side model override today, but we preserve the value so
  // callers can introspect it and we can ship the override the day the
  // server exposes it.
  public readonly modelId: string;
  private readonly language: string;
  private readonly firstMessage: string;
  // Exposed publicly so the stream handler can detect μ-law negotiation
  // (``"ulaw_8000"``) and skip resampling / transcoding on the audio path.
  public readonly outputAudioFormat: string | undefined;
  public readonly inputAudioFormat: string | undefined;
  private readonly useSignedUrl: boolean;

  // Populated from `conversation_initiation_metadata`.
  public conversationId: string | null = null;
  public agentOutputAudioFormat: string | null = null;
  public userInputAudioFormat: string | null = null;

  private agentSpeaking = false;
  private silenceTimer: ReturnType<typeof setTimeout> | null = null;
  private closePromise: Promise<void> | null = null;

  // Overloaded: accept either positional args (back-compat with 4-arg form)
  // or a single options object (preferred for new args).
  constructor(
    apiKey: string,
    agentId?: string,
    voiceId?: string,
    firstMessage?: string,
  );
  constructor(options: ElevenLabsConvAIOptions);
  constructor(
    apiKeyOrOptions: string | ElevenLabsConvAIOptions,
    agentId: string = '',
    voiceId: string = 'EXAVITQu4vr4xnSDxMaL',
    firstMessage: string = '',
  ) {
    if (typeof apiKeyOrOptions === 'object') {
      const o = apiKeyOrOptions;
      this.apiKey = o.apiKey;
      this.agentId = o.agentId ?? '';
      this.voiceId = o.voiceId ?? 'EXAVITQu4vr4xnSDxMaL';
      this.modelId = o.modelId ?? 'eleven_flash_v2_5';
      this.language = o.language ?? 'it';
      this.firstMessage = o.firstMessage ?? '';
      this.outputAudioFormat = o.outputAudioFormat;
      this.inputAudioFormat = o.inputAudioFormat;
      this.useSignedUrl = o.useSignedUrl ?? false;
    } else {
      this.apiKey = apiKeyOrOptions;
      this.agentId = agentId;
      this.voiceId = voiceId;
      this.modelId = 'eleven_flash_v2_5';
      this.language = 'it';
      this.firstMessage = firstMessage;
      this.outputAudioFormat = undefined;
      this.inputAudioFormat = undefined;
      this.useSignedUrl = false;
    }
  }

  // ------------------------------------------------------------------
  // Telephony factories
  // ------------------------------------------------------------------

  /**
   * Build an adapter pre-configured for Twilio Media Streams.
   *
   * Negotiates `ulaw_8000` for both `outputAudioFormat` and
   * `inputAudioFormat`, matching Twilio's μ-law @ 8 kHz wire format. The
   * SDK's stream handler detects this and skips the 8 kHz → 16 kHz inbound
   * resample and the 16 kHz → 8 kHz / PCM → μ-law outbound transcode.
   * Saves ~30–80 ms first-byte plus per-frame CPU on every turn.
   */
  static forTwilio(
    apiKey: string,
    agentId: string,
    options: Omit<
      ElevenLabsConvAIOptions,
      'apiKey' | 'agentId' | 'outputAudioFormat' | 'inputAudioFormat'
    > = {},
  ): ElevenLabsConvAIAdapter {
    return new ElevenLabsConvAIAdapter({
      ...options,
      apiKey,
      agentId,
      outputAudioFormat: 'ulaw_8000',
      inputAudioFormat: 'ulaw_8000',
    });
  }

  /**
   * Build an adapter pre-configured for Telnyx bidirectional media.
   *
   * Telnyx negotiates PCMU @ 8 kHz when `streaming_start` sets
   * `stream_bidirectional_codec=PCMU` (the SDK default). Picking
   * `ulaw_8000` on both ConvAI directions removes every transcode on the
   * audio path — same optimization as `forTwilio`.
   */
  static forTelnyx(
    apiKey: string,
    agentId: string,
    options: Omit<
      ElevenLabsConvAIOptions,
      'apiKey' | 'agentId' | 'outputAudioFormat' | 'inputAudioFormat'
    > = {},
  ): ElevenLabsConvAIAdapter {
    return new ElevenLabsConvAIAdapter({
      ...options,
      apiKey,
      agentId,
      outputAudioFormat: 'ulaw_8000',
      inputAudioFormat: 'ulaw_8000',
    });
  }

  private async fetchSignedUrl(): Promise<string> {
    if (!this.agentId) {
      throw new Error('useSignedUrl=true requires agentId');
    }
    const url = `${ELEVENLABS_SIGNED_URL}?agent_id=${encodeURIComponent(this.agentId)}`;
    const resp = await fetch(url, {
      method: 'GET',
      headers: { 'xi-api-key': this.apiKey },
      signal: AbortSignal.timeout(15_000),
    });
    if (!resp.ok) {
      const body = await resp.text();
      throw new Error(`ElevenLabs signed-url error ${resp.status}: ${body}`);
    }
    const data = (await resp.json()) as { signed_url?: string };
    if (!data.signed_url) {
      throw new Error("ElevenLabs signed-url response missing 'signed_url'");
    }
    return data.signed_url;
  }

  async connect(): Promise<void> {
    let wsUrl: string;
    let wsOptions: WebSocket.ClientOptions | undefined;

    if (this.useSignedUrl) {
      wsUrl = await this.fetchSignedUrl();
      // Signed URL embeds auth — no header needed.
      wsOptions = undefined;
    } else {
      wsUrl = this.agentId
        ? `${ELEVENLABS_CONVAI_URL}?agent_id=${encodeURIComponent(this.agentId)}`
        : ELEVENLABS_CONVAI_URL;
      wsOptions = { headers: { 'xi-api-key': this.apiKey } };
    }

    this.ws = new WebSocket(wsUrl, wsOptions);

    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error('ElevenLabs ConvAI connect timeout')),
        15000,
      );

      this.ws!.once('open', () => {
        clearTimeout(timeout);

        // Build conversation_config_override with optional overrides plumbed.
        const agentCfg: Record<string, unknown> = {};
        if (this.firstMessage) agentCfg['first_message'] = this.firstMessage;
        if (this.language) agentCfg['language'] = this.language;

        const override: Record<string, unknown> = {
          tts: this.outputAudioFormat
            ? { voice_id: this.voiceId, output_format: this.outputAudioFormat }
            : { voice_id: this.voiceId },
        };
        if (this.inputAudioFormat) {
          override['asr'] = { input_format: this.inputAudioFormat };
        }
        if (Object.keys(agentCfg).length > 0) {
          override['agent'] = agentCfg;
        }

        const config: Record<string, unknown> = {
          type: 'conversation_initiation_client_data',
          conversation_config_override: override,
        };

        this.ws!.send(JSON.stringify(config));
        resolve();
      });

      this.ws!.once('error', (err) => {
        clearTimeout(timeout);
        reject(err);
      });
    });

    // Attach long-lived handlers AFTER the connect promise resolves. These
    // stay wired for the life of the session (not just during handshake).
    this.ws.on('error', (err) => {
      getLogger().error('ElevenLabs ConvAI WS error:', err);
      this.safeInvoke('error', err instanceof Error ? err.message : String(err));
    });

    this.ws.on('close', (code, reason) => {
      this.clearSilenceTimer();
      this.safeInvoke('close', {
        code,
        reason: reason?.toString() ?? '',
      });
    });

    this.ws.on('message', (raw) => {
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(raw.toString()) as Record<string, unknown>;
      } catch {
        return;
      }
      this.handleMessage(parsed);
    });
  }

  private safeInvoke(type: string, data: unknown): void {
    const cb = this.eventCallback;
    if (!cb) return;
    void Promise.resolve(cb(type, data)).catch((err) =>
      getLogger().error('onEvent callback error:', err),
    );
  }

  private respondToPing(eventId: unknown, delayMs: number): void {
    const send = (): void => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      try {
        this.ws.send(JSON.stringify({ type: 'pong', event_id: eventId }));
      } catch (err) {
        getLogger().warn('ElevenLabs ConvAI pong send failed:', err);
      }
    };
    if (delayMs && delayMs > 0) {
      setTimeout(send, delayMs);
    } else {
      send();
    }
  }

  private clearSilenceTimer(): void {
    if (this.silenceTimer) {
      clearTimeout(this.silenceTimer);
      this.silenceTimer = null;
    }
  }

  private finalizeAgentTurn(): void {
    this.clearSilenceTimer();
    if (this.agentSpeaking) {
      this.agentSpeaking = false;
      this.safeInvoke('response_done', null);
    }
  }

  private scheduleSilenceDone(): void {
    this.clearSilenceTimer();
    this.silenceTimer = setTimeout(() => {
      if (this.agentSpeaking) {
        this.agentSpeaking = false;
        this.safeInvoke('response_done', null);
      }
    }, AGENT_SILENCE_MS);
  }

  private handleMessage(parsed: Record<string, unknown>): void {
    const msgType = parsed['type'] as string | undefined;

    if (msgType === 'ping') {
      // Server terminates WS after ~20 s without a pong.
      const pingPayload =
        (parsed['ping_event'] as Record<string, unknown> | undefined) ??
        (parsed['ping'] as Record<string, unknown> | undefined) ??
        {};
      const eventId = pingPayload['event_id'] ?? parsed['event_id'];
      const pingMs = (pingPayload['ping_ms'] as number | undefined) ?? 0;
      this.respondToPing(eventId, pingMs);
      return;
    }

    if (msgType === 'conversation_initiation_metadata') {
      const meta =
        (parsed['conversation_initiation_metadata_event'] as
          | Record<string, unknown>
          | undefined) ?? parsed;
      this.conversationId =
        (meta['conversation_id'] as string | undefined) ?? this.conversationId;
      this.agentOutputAudioFormat =
        (meta['agent_output_audio_format'] as string | undefined) ??
        this.agentOutputAudioFormat;
      this.userInputAudioFormat =
        (meta['user_input_audio_format'] as string | undefined) ??
        this.userInputAudioFormat;
      // New turn boundary — finalize any dangling agent turn.
      this.finalizeAgentTurn();
      return;
    }

    if (msgType === 'audio') {
      const audioEvt = parsed['audio_event'] as
        | Record<string, unknown>
        | undefined;
      let audioB64: string | undefined;
      if (audioEvt) {
        audioB64 =
          (audioEvt['audio_base_64'] as string | undefined) ??
          (audioEvt['audio'] as string | undefined);
      }
      if (!audioB64) {
        audioB64 = parsed['audio'] as string | undefined;
      }
      if (audioB64) {
        this.agentSpeaking = true;
        this.safeInvoke('audio', Buffer.from(audioB64, 'base64'));
        this.scheduleSilenceDone();
      }
      return;
    }

    if (msgType === 'user_transcript') {
      const evt =
        (parsed['user_transcription_event'] as
          | Record<string, unknown>
          | undefined) ?? parsed;
      const text = evt['user_transcript'] ?? evt['text'] ?? '';
      this.finalizeAgentTurn();
      this.safeInvoke('transcript_input', text);
      return;
    }

    if (msgType === 'agent_response') {
      const evt =
        (parsed['agent_response_event'] as
          | Record<string, unknown>
          | undefined) ?? parsed;
      const text = evt['agent_response'] ?? evt['text'] ?? '';
      this.safeInvoke('transcript_output', text);
      // `agent_response` is the START of the agent turn — do NOT fire
      // response_done here. The silence watcher or a subsequent
      // interruption / user_transcript / metadata event will finalize.
      this.agentSpeaking = true;
      this.safeInvoke('response_start', { text });
      return;
    }

    if (msgType === 'interruption') {
      this.finalizeAgentTurn();
      this.safeInvoke('interruption', null);
      return;
    }

    if (msgType === 'error') {
      const errText =
        (parsed['message'] as string | undefined) ??
        (parsed['error'] as string | undefined) ??
        JSON.stringify(parsed);
      getLogger().error('ElevenLabs ConvAI error:', errText);
      this.safeInvoke('error', errText);
      return;
    }
  }

  sendAudio(audioBytes: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    // Per ElevenLabs ConvAI protocol: inbound caller audio is a JSON message
    // with a top-level `user_audio_chunk` key holding base64 PCM.
    this.ws.send(
      JSON.stringify({
        user_audio_chunk: audioBytes.toString('base64'),
      }),
    );
  }

  onEvent(callback: EventCallback): void {
    this.eventCallback = callback;
  }

  async close(): Promise<void> {
    this.clearSilenceTimer();
    if (!this.ws) {
      this.eventCallback = null;
      return;
    }
    if (this.closePromise) {
      await this.closePromise;
      return;
    }
    const ws = this.ws;
    this.closePromise = new Promise<void>((resolve) => {
      if (
        ws.readyState === WebSocket.CLOSED ||
        ws.readyState === WebSocket.CLOSING
      ) {
        resolve();
        return;
      }
      const done = (): void => {
        resolve();
      };
      ws.once('close', done);
      ws.once('error', done);
      try {
        ws.close();
      } catch {
        resolve();
      }
    });
    try {
      await this.closePromise;
    } finally {
      this.ws = null;
      this.eventCallback = null;
      this.closePromise = null;
    }
  }
}

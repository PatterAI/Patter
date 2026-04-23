import WebSocket from 'ws';
import { getLogger } from '../logger';

const ELEVENLABS_CONVAI_URL = 'wss://api.elevenlabs.io/v1/convai/conversation';

export class ElevenLabsConvAIAdapter {
  private ws: WebSocket | null = null;
  private eventCallback: ((type: string, data: unknown) => void | Promise<void>) | null = null;

  constructor(
    private readonly apiKey: string,
    private readonly agentId: string = '',
    private readonly voiceId: string = 'EXAVITQu4vr4xnSDxMaL',
    _modelId: string = 'eleven_flash_v2_5',
    _language: string = 'en',
    private readonly firstMessage: string = '',
  ) {}

  async connect(): Promise<void> {
    const url = this.agentId
      ? `${ELEVENLABS_CONVAI_URL}?agent_id=${encodeURIComponent(this.agentId)}`
      : ELEVENLABS_CONVAI_URL;

    this.ws = new WebSocket(url, {
      headers: { 'xi-api-key': this.apiKey },
    });

    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error('ElevenLabs ConvAI connect timeout')),
        15000,
      );

      this.ws!.once('open', () => {
        clearTimeout(timeout);

        const config: Record<string, unknown> = {
          type: 'conversation_initiation_client_data',
          conversation_config_override: {
            tts: { voice_id: this.voiceId },
          },
        };

        if (this.firstMessage) {
          (config['conversation_config_override'] as Record<string, unknown>)['agent'] = {
            first_message: this.firstMessage,
          };
        }

        this.ws!.send(JSON.stringify(config));
        resolve();
      });

      this.ws!.once('error', (err) => {
        clearTimeout(timeout);
        reject(err);
      });
    });

    this.ws.on('message', (raw) => {
      const cb = this.eventCallback;
      if (!cb) return;
      const safeInvoke = (type: string, data: unknown): void => {
        void Promise.resolve(cb(type, data)).catch((err) =>
          getLogger().error('onEvent callback error:', err),
        );
      };
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(raw.toString()) as Record<string, unknown>;
      } catch {
        return;
      }
      const msgType = parsed['type'] as string | undefined;

      if (msgType === 'audio') {
        const audioB64 = parsed['audio'] as string | undefined;
        if (audioB64) {
          safeInvoke('audio', Buffer.from(audioB64, 'base64'));
        }
      } else if (msgType === 'user_transcript') {
        safeInvoke('transcript_input', parsed['text'] ?? '');
      } else if (msgType === 'agent_response') {
        safeInvoke('transcript_output', parsed['text'] ?? '');
        // ElevenLabs agent_response contains the complete text (not a delta),
        // so signal turn completion immediately.
        safeInvoke('response_done', null);
      } else if (msgType === 'interruption') {
        safeInvoke('interruption', null);
      } else if (msgType === 'error') {
        safeInvoke('error', parsed);
      }
    });
  }

  sendAudio(audioBytes: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(
      JSON.stringify({
        type: 'audio',
        audio: audioBytes.toString('base64'),
      }),
    );
  }

  onEvent(callback: (type: string, data: unknown) => void | Promise<void>): void {
    this.eventCallback = callback;
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
    this.eventCallback = null;
  }
}

import WebSocket from 'ws';

const ELEVENLABS_CONVAI_URL = 'wss://api.elevenlabs.io/v1/convai/conversation';

export class ElevenLabsConvAIAdapter {
  private ws: WebSocket | null = null;
  private eventCallback: ((type: string, data: unknown) => void) | null = null;

  constructor(
    private readonly apiKey: string,
    private readonly agentId: string = '',
    private readonly voiceId: string = '21m00Tcm4TlvDq8ikWAM',
    _modelId: string = 'eleven_turbo_v2_5',
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
      if (!this.eventCallback) return;
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
          this.eventCallback('audio', Buffer.from(audioB64, 'base64'));
        }
      } else if (msgType === 'user_transcript') {
        this.eventCallback('transcript_input', parsed['text'] ?? '');
      } else if (msgType === 'agent_response') {
        this.eventCallback('transcript_output', parsed['text'] ?? '');
        // ElevenLabs agent_response contains the complete text (not a delta),
        // so signal turn completion immediately.
        this.eventCallback('response_done', null);
      } else if (msgType === 'interruption') {
        this.eventCallback('interruption', null);
      } else if (msgType === 'error') {
        this.eventCallback('error', parsed);
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

  onEvent(callback: (type: string, data: unknown) => void): void {
    this.eventCallback = callback;
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
    this.eventCallback = null;
  }
}

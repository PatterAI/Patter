import WebSocket from 'ws';
import { getLogger } from '../logger';

export class OpenAIRealtimeAdapter {
  private ws: WebSocket | null = null;

  constructor(
    private readonly apiKey: string,
    private readonly model: string = 'gpt-4o-mini-realtime-preview',
    private readonly voice: string = 'alloy',
    private readonly instructions: string = '',
    private readonly tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>,
  ) {}

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
      const timer = setTimeout(() => reject(new Error('OpenAI Realtime connect timeout')), 15000);
      this.ws!.on('message', (raw) => {
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
            input_audio_format: 'g711_ulaw',
            output_audio_format: 'g711_ulaw',
            voice: this.voice,
            instructions: this.instructions || 'You are a helpful voice assistant. Be concise.',
            turn_detection: { type: 'server_vad', threshold: 0.5, prefix_padding_ms: 300, silence_duration_ms: 500 },
            input_audio_transcription: { model: 'whisper-1' },
          };
          if (this.tools?.length) {
            config.tools = this.tools.map(t => ({
              type: 'function',
              name: t.name,
              description: t.description,
              parameters: t.parameters,
            }));
          }
          this.ws!.send(JSON.stringify({ type: 'session.update', session: config }));
        } else if (msg.type === 'session.updated') {
          clearTimeout(timer);
          resolve();
        }
      });
      this.ws!.on('error', (err) => {
        clearTimeout(timer);
        reject(err);
      });
    });
  }

  sendAudio(mulawAudio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: mulawAudio.toString('base64') }));
  }

  onEvent(callback: (type: string, data: unknown) => void): void {
    if (!this.ws) return;
    this.ws.on('message', (raw) => {
      let data: { type: string; delta?: string; transcript?: string; call_id?: string; name?: string; arguments?: string; error?: unknown };
      try {
        data = JSON.parse(raw.toString()) as typeof data;
      } catch (e) {
        getLogger().warn(`OpenAI Realtime: failed to parse event message: ${String(e)}`);
        return;
      }
      const t = data.type;
      if (t === 'response.audio.delta') {
        callback('audio', Buffer.from(data.delta ?? '', 'base64'));
      } else if (t === 'response.audio_transcript.delta') {
        callback('transcript_output', data.delta);
      } else if (t === 'input_audio_buffer.speech_started') {
        callback('speech_started', null);
      } else if (t === 'conversation.item.input_audio_transcription.completed') {
        callback('transcript_input', data.transcript);
      } else if (t === 'response.function_call_arguments.done') {
        callback('function_call', { call_id: data.call_id, name: data.name, arguments: data.arguments });
      } else if (t === 'response.done') {
        callback('response_done', null);
      } else if (t === 'error') {
        callback('error', data.error);
      }
    });
  }

  cancelResponse(): void {
    this.ws?.send(JSON.stringify({ type: 'response.cancel' }));
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
    this.ws?.close();
    this.ws = null;
  }
}

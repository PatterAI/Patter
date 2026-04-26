/**
 * Ultravox realtime adapter.
 *
 * Partially adapted (~70% port) from LiveKit Agents
 * (livekit-plugins-ultravox, Apache 2.0). Pure WebSocket protocol — no vendor SDK.
 *
 * Reframed to Patter's connect / sendAudio / onEvent / close surface,
 * matching OpenAIRealtimeAdapter.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

export const ULTRAVOX_DEFAULT_API_BASE = 'https://api.ultravox.ai/api';
export const ULTRAVOX_DEFAULT_SR = 16000;

export type UltravoxEventHandler = (
  type:
    | 'audio'
    | 'transcript_input'
    | 'transcript_output'
    | 'function_call'
    | 'speech_started'
    | 'response_done'
    | 'error',
  data: unknown,
) => void | Promise<void>;

interface UltravoxOptions {
  model?: string;
  voice?: string;
  instructions?: string;
  language?: string;
  tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>;
  apiBase?: string;
  sampleRate?: number;
  firstMessage?: string;
}

export class UltravoxRealtimeAdapter {
  private readonly model: string;
  private readonly voice: string;
  private readonly instructions: string;
  private readonly language: string;
  private readonly tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>;
  private readonly apiBase: string;
  private readonly sampleRate: number;
  private readonly firstMessage: string;

  private ws: WebSocket | null = null;
  private handlers: UltravoxEventHandler[] = [];
  /** Exposed for diagnostics — true while the underlying socket is open. */
  running = false;

  constructor(private readonly apiKey: string, options: UltravoxOptions = {}) {
    this.model = options.model ?? 'fixie-ai/ultravox';
    this.voice = options.voice ?? '';
    this.instructions = options.instructions ?? '';
    this.language = options.language ?? 'en';
    this.tools = options.tools;
    this.apiBase = (options.apiBase ?? ULTRAVOX_DEFAULT_API_BASE).replace(/\/$/, '');
    this.sampleRate = options.sampleRate ?? ULTRAVOX_DEFAULT_SR;
    this.firstMessage = options.firstMessage ?? '';
  }

  async connect(): Promise<void> {
    // Step 1: create the call and get the joinUrl.
    const body: Record<string, unknown> = {
      model: this.model,
      languageHint: this.language,
      medium: {
        serverWebSocket: {
          inputSampleRate: this.sampleRate,
          outputSampleRate: this.sampleRate,
        },
      },
      recordingEnabled: false,
    };
    if (this.voice) body.voice = this.voice;
    if (this.instructions) body.systemPrompt = this.instructions;
    // ``firstSpeaker`` and ``initialMessages`` are mutually exclusive on the
    // Ultravox API: setting both causes the server to reject the call.
    // Prefer ``initialMessages`` when a ``firstMessage`` is configured;
    // otherwise default to FIRST_SPEAKER_USER (user speaks first). Matches
    // the Python port in ``sdk-py/getpatter/providers/ultravox_realtime.py``.
    if (this.firstMessage) {
      body.initialOutputMedium = 'MESSAGE_MEDIUM_VOICE';
      body.initialMessages = [
        { role: 'MESSAGE_ROLE_AGENT', text: this.firstMessage },
      ];
    } else {
      body.firstSpeaker = 'FIRST_SPEAKER_USER';
    }
    if (this.tools?.length) {
      body.selectedTools = this.tools.map((t) => ({
        temporaryTool: {
          modelToolName: t.name,
          description: t.description,
          dynamicParameters: toolParamsToUltravox(t.parameters),
        },
      }));
    }

    const resp = await fetch(`${this.apiBase}/calls`, {
      method: 'POST',
      headers: {
        'X-API-Key': this.apiKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(`Ultravox create call failed: ${resp.status} ${text}`);
    }
    const call = (await resp.json()) as { joinUrl?: string };
    if (!call.joinUrl) throw new Error('Ultravox response missing joinUrl');

    // Step 2: open the WebSocket.
    this.ws = new WebSocket(call.joinUrl);
    await new Promise<void>((resolve, reject) => {
      const ws = this.ws!;
      const onOpen = (): void => {
        ws.off('error', onError);
        resolve();
      };
      const onError = (err: Error): void => {
        ws.off('open', onOpen);
        reject(err);
      };
      ws.once('open', onOpen);
      ws.once('error', onError);
    });
    this.running = true;

    this.ws.on('message', (raw, isBinary) => {
      void this.handleMessage(raw, isBinary).catch((err) =>
        getLogger().error(`Ultravox message handler error: ${String(err)}`),
      );
    });
    this.ws.on('close', () => {
      this.running = false;
    });
  }

  sendAudio(pcm: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(pcm, { binary: true });
  }

  async sendText(text: string): Promise<void> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: 'input_text_message', text }));
  }

  async sendFunctionResult(callId: string, result: string): Promise<void> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(
      JSON.stringify({
        type: 'client_tool_result',
        invocationId: callId,
        result,
        responseType: 'tool-response',
      }),
    );
  }

  cancelResponse(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: 'playback_clear_buffer' }));
  }

  onEvent(handler: UltravoxEventHandler): void {
    this.handlers.push(handler);
  }

  private async emit(
    type:
      | 'audio'
      | 'transcript_input'
      | 'transcript_output'
      | 'function_call'
      | 'speech_started'
      | 'response_done'
      | 'error',
    data: unknown,
  ): Promise<void> {
    for (const h of this.handlers) {
      try {
        await h(type, data);
      } catch (err) {
        getLogger().error(`Ultravox handler threw: ${String(err)}`);
      }
    }
  }

  private async handleMessage(raw: WebSocket.RawData, isBinary: boolean): Promise<void> {
    if (isBinary) {
      const buf = Buffer.isBuffer(raw) ? raw : Buffer.from(raw as ArrayBuffer);
      await this.emit('audio', buf);
      return;
    }
    let event: { type?: string; [k: string]: unknown };
    try {
      event = JSON.parse(raw.toString()) as typeof event;
    } catch {
      getLogger().warn('Ultravox: non-JSON text frame');
      return;
    }
    const etype = event.type ?? '';
    if (etype === 'transcript') {
      const role = event.role as string | undefined;
      const text = (event.text ?? event.delta ?? '') as string;
      const isFinal = Boolean(event.final);
      if (role === 'user' && isFinal && text) await this.emit('transcript_input', text);
      else if (role === 'agent' && text) await this.emit('transcript_output', text);
    } else if (etype === 'client_tool_invocation') {
      await this.emit('function_call', {
        call_id: event.invocationId ?? '',
        name: event.toolName ?? '',
        arguments: JSON.stringify(event.parameters ?? {}),
      });
    } else if (etype === 'state') {
      const state = event.state as string | undefined;
      if (state === 'listening') await this.emit('speech_started', null);
      else if (state === 'idle') await this.emit('response_done', null);
    } else if (etype === 'playback_clear_buffer') {
      await this.emit('speech_started', null);
    }
  }

  async close(): Promise<void> {
    this.running = false;
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }
}

function toolParamsToUltravox(parameters: Record<string, unknown>): Array<Record<string, unknown>> {
  const props = (parameters.properties as Record<string, unknown>) ?? {};
  const required = new Set<string>(
    Array.isArray(parameters.required) ? (parameters.required as string[]) : [],
  );
  return Object.entries(props).map(([name, schema]) => ({
    name,
    location: 'PARAMETER_LOCATION_BODY',
    schema,
    required: required.has(name),
  }));
}

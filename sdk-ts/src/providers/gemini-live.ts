/**
 * Gemini Live realtime adapter.
 *
 * Partially adapted (~65% port) from LiveKit Agents
 * (livekit-plugins-google, Apache 2.0). Reframed to Patter's realtime adapter
 * surface — connect / sendAudio / onEvent / close — matching OpenAIRealtimeAdapter.
 *
 * Uses the @google/genai SDK lazily imported at connect() so consumers that do
 * not use Gemini Live do not pay the load cost. Install with:
 *
 *    npm install @google/genai
 */

import { getLogger } from '../logger';

export const GEMINI_DEFAULT_INPUT_SR = 16000;
export const GEMINI_DEFAULT_OUTPUT_SR = 24000;

export type GeminiLiveEventHandler = (
  type:
    | 'audio'
    | 'transcript_output'
    | 'function_call'
    | 'speech_started'
    | 'response_done'
    | 'error',
  data: unknown,
) => void | Promise<void>;

interface GeminiLiveOptions {
  model?: string;
  voice?: string;
  instructions?: string;
  language?: string;
  tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>;
  inputSampleRate?: number;
  outputSampleRate?: number;
  temperature?: number;
}

export class GeminiLiveAdapter {
  private readonly model: string;
  private readonly voice: string;
  private readonly instructions: string;
  private readonly language: string;
  private readonly tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>;
  private readonly inputSampleRate: number;
  /** Output sample rate — exposed so callers can configure downstream transcoding. */
  readonly outputSampleRate: number;
  private readonly temperature: number;

  private client: unknown = null;
  private session: unknown = null;
  private receiveLoop: Promise<void> | null = null;
  private handlers: GeminiLiveEventHandler[] = [];
  private running = false;

  constructor(
    private readonly apiKey: string,
    options: GeminiLiveOptions = {},
  ) {
    this.model = options.model ?? 'gemini-2.0-flash-exp';
    this.voice = options.voice ?? 'Puck';
    this.instructions = options.instructions ?? '';
    this.language = options.language ?? 'en-US';
    this.tools = options.tools;
    this.inputSampleRate = options.inputSampleRate ?? GEMINI_DEFAULT_INPUT_SR;
    this.outputSampleRate = options.outputSampleRate ?? GEMINI_DEFAULT_OUTPUT_SR;
    this.temperature = options.temperature ?? 0.8;
  }

  async connect(): Promise<void> {
    let genaiModule: { GoogleGenAI: new (args: { apiKey: string; httpOptions?: Record<string, unknown> }) => unknown };
    try {
      // Lazy dynamic import — keeps @google/genai optional.
      // Variable module name avoids TS2307 when the peer dep is not installed.
      const modName = '@google/genai';
      genaiModule = (await import(modName)) as typeof genaiModule;
    } catch (err) {
      throw new Error(
        "Gemini Live requires the '@google/genai' package. Install with: npm install @google/genai",
      );
    }

    const { GoogleGenAI } = genaiModule;
    this.client = new GoogleGenAI({ apiKey: this.apiKey });

    const config: Record<string, unknown> = {
      responseModalities: ['AUDIO'],
      speechConfig: {
        voiceConfig: { prebuiltVoiceConfig: { voiceName: this.voice } },
        languageCode: this.language,
      },
      temperature: this.temperature,
    };
    if (this.instructions) {
      config.systemInstruction = { parts: [{ text: this.instructions }] };
    }
    if (this.tools?.length) {
      config.tools = [
        {
          functionDeclarations: this.tools.map((t) => ({
            name: t.name,
            description: t.description,
            parameters: t.parameters,
          })),
        },
      ];
    }

    // The genai live surface is organised as client.live.connect({model, config, callbacks?}).
    // Some SDK versions return a Session-like object with send*/receive methods.
    const liveApi = (this.client as { live?: { connect?: (args: unknown) => Promise<unknown> } }).live;
    if (!liveApi?.connect) {
      throw new Error('@google/genai: live.connect is not available in this version');
    }
    this.session = await liveApi.connect({ model: this.model, config });
    this.running = true;

    // Start the receive pump.
    this.receiveLoop = this.pumpReceive().catch((err) => {
      getLogger().error(`Gemini Live receive loop error: ${String(err)}`);
    });
  }

  sendAudio(pcm: Buffer): void {
    if (!this.session || !this.running) return;
    const mime = `audio/pcm;rate=${this.inputSampleRate}`;
    const sess = this.session as { sendRealtimeInput?: (args: unknown) => unknown };
    const result = sess.sendRealtimeInput?.({
      media: { data: pcm.toString('base64'), mimeType: mime },
    });
    if (result instanceof Promise) {
      void result.catch((err) =>
        getLogger().warn(`Gemini Live sendAudio error: ${String(err)}`),
      );
    }
  }

  async sendText(text: string): Promise<void> {
    if (!this.session) return;
    const sess = this.session as { sendClientContent?: (args: unknown) => Promise<void> };
    await sess.sendClientContent?.({
      turns: { role: 'user', parts: [{ text }] },
      turnComplete: true,
    });
  }

  async sendFunctionResult(callId: string, result: string): Promise<void> {
    if (!this.session) return;
    const sess = this.session as { sendToolResponse?: (args: unknown) => Promise<void> };
    await sess.sendToolResponse?.({
      functionResponses: [
        { id: callId, name: callId, response: { result } },
      ],
    });
  }

  cancelResponse(): void {
    // Gemini Live barge-in is VAD-driven — explicit cancel not in v1alpha wire protocol.
    getLogger().debug('Gemini Live: cancelResponse is implicit via VAD');
  }

  onEvent(handler: GeminiLiveEventHandler): void {
    this.handlers.push(handler);
  }

  private async emit(
    type:
      | 'audio'
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
        getLogger().error(`Gemini Live handler threw: ${String(err)}`);
      }
    }
  }

  private async pumpReceive(): Promise<void> {
    if (!this.session) return;
    const sess = this.session as { receive?: () => AsyncIterable<unknown> };
    if (typeof sess.receive !== 'function') {
      getLogger().warn('Gemini Live: session.receive() not available');
      return;
    }
    try {
      for await (const response of sess.receive()) {
        if (!this.running) break;
        const r = response as {
          serverContent?: {
            modelTurn?: {
              parts?: Array<{
                inlineData?: { data?: string };
                text?: string;
              }>;
            };
            turnComplete?: boolean;
            interrupted?: boolean;
          };
          toolCall?: {
            functionCalls?: Array<{
              id?: string;
              name?: string;
              args?: Record<string, unknown> | string;
            }>;
          };
        };

        const sc = r.serverContent;
        if (sc) {
          for (const part of sc.modelTurn?.parts ?? []) {
            if (part.inlineData?.data) {
              await this.emit('audio', Buffer.from(part.inlineData.data, 'base64'));
            }
            if (part.text) await this.emit('transcript_output', part.text);
          }
          if (sc.turnComplete) await this.emit('response_done', null);
          if (sc.interrupted) await this.emit('speech_started', null);
        }
        if (r.toolCall) {
          for (const fn of r.toolCall.functionCalls ?? []) {
            const args = fn.args ?? {};
            await this.emit('function_call', {
              call_id: fn.id ?? '',
              name: fn.name ?? '',
              arguments: typeof args === 'string' ? args : JSON.stringify(args),
            });
          }
        }
      }
    } catch (err) {
      if (this.running) await this.emit('error', err);
    } finally {
      this.running = false;
    }
  }

  async close(): Promise<void> {
    this.running = false;
    if (this.session) {
      const sess = this.session as { close?: () => Promise<void> | void };
      try {
        await sess.close?.();
      } catch {
        /* ignore */
      }
      this.session = null;
    }
    this.client = null;
    if (this.receiveLoop) {
      await this.receiveLoop.catch(() => undefined);
      this.receiveLoop = null;
    }
  }
}

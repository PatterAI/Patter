/**
 * Gemini Live realtime adapter.
 *
 * Implements Patter's realtime adapter surface — connect / sendAudio /
 * onEvent / close — matching OpenAIRealtimeAdapter.
 *
 * Uses the @google/genai SDK lazily imported at connect() so consumers that do
 * not use Gemini Live do not pay the load cost. Install with:
 *
 *    npm install @google/genai
 *
 * NOTE: Native-audio Gemini Live models are **v1alpha-only**. We pass
 * `httpOptions: { apiVersion: 'v1alpha' }` when constructing the client.
 * When Google promotes native audio to GA, switch to `v1beta` / `v1` and
 * update the default model below.
 * See: https://ai.google.dev/gemini-api/docs/live
 */

import { getLogger } from '../logger';

export const GEMINI_DEFAULT_INPUT_SR = 16000;
export const GEMINI_DEFAULT_OUTPUT_SR = 24000;

/** Callback signature for events emitted by {@link GeminiLiveAdapter}. */
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

/** Realtime adapter for Google's Gemini Live native-audio API. */
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
  /**
   * Tracks call_id -> function name so tool responses can be sent back with
   * the correct `name` field (Gemini expects the original function name,
   * not the call_id).
   */
  private pendingToolCalls: Map<string, string> = new Map();

  constructor(
    private readonly apiKey: string,
    options: GeminiLiveOptions = {},
  ) {
    // gemini-2.0-flash-exp was experimental preview retired Dec 2024.
    // gemini-live-2.5-flash-preview was shut down Dec 9, 2025.
    // Current native-audio live model (v1alpha-only) is the dated preview.
    // Callers can override via GeminiLive({ model: ... }).
    // Default model — see https://ai.google.dev/gemini-api/docs/live
    this.model = options.model ?? 'gemini-2.5-flash-native-audio-preview-09-2025';
    this.voice = options.voice ?? 'Puck';
    this.instructions = options.instructions ?? '';
    this.language = options.language ?? 'en-US';
    this.tools = options.tools;
    this.inputSampleRate = options.inputSampleRate ?? GEMINI_DEFAULT_INPUT_SR;
    this.outputSampleRate = options.outputSampleRate ?? GEMINI_DEFAULT_OUTPUT_SR;
    this.temperature = options.temperature ?? 0.8;
  }

  /** Lazily import @google/genai, open a Live session, and start the receive loop. */
  async connect(): Promise<void> {
    let genaiModule: { GoogleGenAI: new (args: { apiKey: string; httpOptions?: Record<string, unknown> }) => unknown };
    try {
      // Lazy dynamic import — keeps @google/genai optional.
      // Variable module name avoids TS2307 when the peer dep is not installed.
      const modName = '@google/genai';
      genaiModule = (await import(modName)) as typeof genaiModule;
    } catch {
      throw new Error(
        '\nGemini Live requires the "@google/genai" package, which is not installed.\n\n' +
          '  Install:  npm install @google/genai\n\n' +
          'This is an optional peer dependency of getpatter — it is only needed when\n' +
          'you use GeminiLive as an agent engine. Other LLM/engine providers do not\n' +
          'require it.\n',
      );
    }

    const { GoogleGenAI } = genaiModule;
    // Native-audio models require the v1alpha endpoint — see module doc.
    this.client = new GoogleGenAI({
      apiKey: this.apiKey,
      httpOptions: { apiVersion: 'v1alpha' },
    });

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

  /** Send a PCM audio chunk to Gemini as base64 inline data. */
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

  /** Send a text turn to Gemini and mark the turn complete. */
  async sendText(text: string): Promise<void> {
    if (!this.session) return;
    const sess = this.session as { sendClientContent?: (args: unknown) => Promise<void> };
    await sess.sendClientContent?.({
      turns: { role: 'user', parts: [{ text }] },
      turnComplete: true,
    });
  }

  /** Send a tool/function-call result back to Gemini. */
  async sendFunctionResult(callId: string, result: string): Promise<void> {
    if (!this.session) return;
    const sess = this.session as { sendToolResponse?: (args: unknown) => Promise<void> };
    // Gemini requires the original function name in the response, not the
    // call_id. Look it up from the map populated when the tool call was
    // emitted; fall back to callId if we never saw this id (defensive).
    const name = this.pendingToolCalls.get(callId) ?? callId;
    this.pendingToolCalls.delete(callId);
    await sess.sendToolResponse?.({
      functionResponses: [
        { id: callId, name, response: { result } },
      ],
    });
  }

  /** No-op — Gemini Live barge-in is VAD-driven, not client-cancelled. */
  cancelResponse(): void {
    // Gemini Live barge-in is VAD-driven — explicit cancel not in v1alpha wire protocol.
    getLogger().debug('Gemini Live: cancelResponse is implicit via VAD');
  }

  /** Register an event handler that receives every Gemini Live event. */
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
            const callId = fn.id ?? '';
            const fnName = fn.name ?? '';
            if (callId && fnName) {
              this.pendingToolCalls.set(callId, fnName);
            }
            await this.emit('function_call', {
              call_id: callId,
              name: fnName,
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

  /** Close the Gemini Live session and stop the receive loop. */
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
    this.pendingToolCalls.clear();
  }
}

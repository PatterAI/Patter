/**
 * Pipeline hook executor for pipeline mode.
 *
 * Runs user-defined hooks at each stage of the STT → LLM → TTS pipeline.
 * Fail-open: if a hook throws, the error is logged and the original value
 * passes through unchanged.
 */

import type { PipelineHooks, HookContext } from './types';
import { getLogger } from './logger';

export class PipelineHookExecutor {
  private readonly hooks: PipelineHooks | undefined;

  constructor(hooks: PipelineHooks | undefined) {
    this.hooks = hooks;
  }

  /**
   * Run beforeSendToStt hook. Returns null to drop the audio chunk.
   * If no hook is defined, returns the audio unchanged.
   * Fail-open: on exception, the original audio passes through.
   */
  async runBeforeSendToStt(audio: Buffer, ctx: HookContext): Promise<Buffer | null> {
    if (!this.hooks?.beforeSendToStt) return audio;
    try {
      return await this.hooks.beforeSendToStt(audio, ctx);
    } catch (e) {
      getLogger().error('Pipeline hook beforeSendToStt threw:', e);
      return audio;
    }
  }

  /**
   * Run afterTranscribe hook. Returns null if hook vetoes the turn.
   * If no hook is defined, returns the transcript unchanged.
   */
  async runAfterTranscribe(transcript: string, ctx: HookContext): Promise<string | null> {
    if (!this.hooks?.afterTranscribe) return transcript;
    try {
      return await this.hooks.afterTranscribe(transcript, ctx);
    } catch (e) {
      getLogger().error('Pipeline hook afterTranscribe threw:', e);
      return transcript;
    }
  }

  /**
   * Run beforeSynthesize hook. Returns null if hook vetoes TTS for this sentence.
   * If no hook is defined, returns the text unchanged.
   */
  async runBeforeSynthesize(text: string, ctx: HookContext): Promise<string | null> {
    if (!this.hooks?.beforeSynthesize) return text;
    try {
      return await this.hooks.beforeSynthesize(text, ctx);
    } catch (e) {
      getLogger().error('Pipeline hook beforeSynthesize threw:', e);
      return text;
    }
  }

  /**
   * Run afterSynthesize hook. Returns null if hook vetoes this audio chunk.
   * If no hook is defined, returns the audio unchanged.
   */
  async runAfterSynthesize(audio: Buffer, text: string, ctx: HookContext): Promise<Buffer | null> {
    if (!this.hooks?.afterSynthesize) return audio;
    try {
      return await this.hooks.afterSynthesize(audio, text, ctx);
    } catch (e) {
      getLogger().error('Pipeline hook afterSynthesize threw:', e);
      return audio;
    }
  }
}

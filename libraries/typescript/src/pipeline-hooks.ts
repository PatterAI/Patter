/**
 * Pipeline hook executor for pipeline mode.
 *
 * Runs user-defined hooks at each stage of the STT → LLM → TTS pipeline.
 * Fail-open: if a hook throws, the error is logged and the original value
 * passes through unchanged.
 */

import type {
  PipelineHooks,
  HookContext,
  AfterLLMHook,
  AfterLLMLegacy,
} from './types';
import { getLogger } from './logger';

let legacyAfterLlmWarned = false;

/**
 * Normalise the user's `afterLlm` value into the 3-tier object form.
 * Accepts either an `AfterLLMHook` object or a legacy `(text, ctx) => string`
 * callable. Returns `undefined` if the hook is not configured. Emits a
 * one-shot deprecation warning when the legacy callable form is detected.
 */
function normaliseAfterLlm(
  hook: AfterLLMHook | AfterLLMLegacy | undefined,
): AfterLLMHook | undefined {
  if (hook === undefined) return undefined;
  if (typeof hook === 'function') {
    if (!legacyAfterLlmWarned) {
      legacyAfterLlmWarned = true;
      getLogger().warn(
        '[patter] afterLlm: (text, ctx) => string is deprecated; pass an object ' +
          "with { onResponse } instead. The legacy form maps to onResponse and " +
          'blocks streaming TTS. Will be removed in v0.7.0.',
      );
    }
    return { onResponse: hook };
  }
  return hook;
}

/** Runs user-defined pipeline hooks (`beforeSendToStt`, `afterTranscribe`, …) with fail-open semantics. */
export class PipelineHookExecutor {
  private readonly hooks: PipelineHooks | undefined;
  private readonly afterLlm: AfterLLMHook | undefined;

  constructor(hooks: PipelineHooks | undefined) {
    this.hooks = hooks;
    this.afterLlm = normaliseAfterLlm(hooks?.afterLlm);
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
   * Run beforeLlm hook. Returns a possibly-modified messages list.
   * Returning ``null`` from the hook means "keep the original" — the LLM
   * call is too important to be silently vetoed.
   * Fail-open: on exception, the original messages pass through.
   */
  async runBeforeLlm(
    messages: Array<Record<string, unknown>>,
    ctx: HookContext,
  ): Promise<Array<Record<string, unknown>>> {
    if (!this.hooks?.beforeLlm) return messages;
    try {
      const result = await this.hooks.beforeLlm(messages, ctx);
      return result ?? messages;
    } catch (e) {
      getLogger().error('Pipeline hook beforeLlm threw:', e);
      return messages;
    }
  }

  /**
   * Tier 1 — per-token sync transform. Returns the (possibly transformed)
   * chunk. Fail-open: on exception or non-string return, the original chunk
   * passes through unchanged. Must be cheap (~0 ms budget).
   */
  runAfterLlmChunk(chunk: string): string {
    if (!this.afterLlm?.onChunk) return chunk;
    try {
      const result = this.afterLlm.onChunk(chunk);
      return typeof result === 'string' ? result : chunk;
    } catch (e) {
      getLogger().error('Pipeline hook afterLlm.onChunk threw:', e);
      return chunk;
    }
  }

  /**
   * Tier 2 — per-sentence rewrite. Returns rewritten sentence text, the
   * original sentence (if hook returned `null`), or `null` to drop the
   * sentence entirely (empty string is treated as drop). Fail-open.
   */
  async runAfterLlmSentence(sentence: string, ctx: HookContext): Promise<string | null> {
    if (!this.afterLlm?.onSentence) return sentence;
    try {
      const result = await this.afterLlm.onSentence(sentence, ctx);
      if (result === null) return sentence; // null = keep original
      if (result === '') return null; // empty string = drop
      return result;
    } catch (e) {
      getLogger().error('Pipeline hook afterLlm.onSentence threw:', e);
      return sentence;
    }
  }

  /**
   * Tier 3 — per-response rewrite. Returns the (possibly rewritten) full
   * response text. Triggered after the LLM stream completes. Caller is
   * responsible for buffering tokens before invocation. Fail-open.
   */
  async runAfterLlmResponse(text: string, ctx: HookContext): Promise<string> {
    if (!this.afterLlm?.onResponse) return text;
    try {
      const result = await this.afterLlm.onResponse(text, ctx);
      return result ?? text;
    } catch (e) {
      getLogger().error('Pipeline hook afterLlm.onResponse threw:', e);
      return text;
    }
  }

  /**
   * Backward-compatible alias for `runAfterLlmResponse`. Existing call sites
   * in the LLM loop continue to work unchanged.
   *
   * @deprecated Use `runAfterLlmResponse` directly.
   */
  async runAfterLlm(text: string, ctx: HookContext): Promise<string> {
    return this.runAfterLlmResponse(text, ctx);
  }

  /**
   * Whether a per-response (tier 3) `onResponse` transform is configured.
   * The LLM loop uses this to decide whether to buffer streaming tokens
   * before yielding them. Per-token (tier 1) and per-sentence (tier 2)
   * transforms do NOT require buffering.
   */
  hasAfterLlmResponse(): boolean {
    return Boolean(this.afterLlm?.onResponse);
  }

  /** Whether a per-sentence (tier 2) transform is configured. */
  hasAfterLlmSentence(): boolean {
    return Boolean(this.afterLlm?.onSentence);
  }

  /** Whether a per-token (tier 1) transform is configured. */
  hasAfterLlmChunk(): boolean {
    return Boolean(this.afterLlm?.onChunk);
  }

  /**
   * Backward-compatible alias for `hasAfterLlmResponse`. The legacy callable
   * form maps to `onResponse`, so this preserves the original semantic for
   * existing call sites.
   *
   * @deprecated Use `hasAfterLlmResponse` directly.
   */
  hasAfterLlm(): boolean {
    return this.hasAfterLlmResponse();
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

// Portions of this file are adapted from LiveKit Agents (Apache License 2.0):
//   https://github.com/livekit/agents
//   livekit-plugins/livekit-plugins-cartesia/livekit/plugins/cartesia/tts.py
//   Source commit: 78a66bcf79c5cea82989401c408f1dff4b961a5b
//
// Copyright 2023 LiveKit, Inc.
// Modifications (c) 2025 PatterAI
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

/**
 * Cartesia TTS provider — HTTP `/tts/bytes` endpoint.
 *
 * The upstream LiveKit plugin also offers a WebSocket streaming mode with
 * word timestamps; this port focuses on the chunked-bytes HTTP API which
 * maps cleanly onto Patter's `synthesize(text)` contract and keeps the
 * provider dependency-free (just `fetch`).
 *
 * Default model is `sonic-3` (GA snapshot `sonic-3-2026-01-12`) — Cartesia's
 * current GA model with a documented ~90 ms TTFB target. Voice IDs from the
 * sonic-2 generation (including the default Katie voice) remain compatible.
 */

const CARTESIA_BASE_URL = 'https://api.cartesia.ai';
// Cartesia API version pin — matches our STT integration and the Cartesia
// Line skill. `2025-04-16` is the current GA snapshot.
const CARTESIA_API_VERSION = '2025-04-16';
const CARTESIA_DEFAULT_VOICE_ID = 'f786b574-daa5-4673-aa0c-cbe3e8534c02';

export interface CartesiaTTSOptions {
  model?: string;
  voice?: string;
  language?: string;
  sampleRate?: number;
  speed?: string | number;
  emotion?: string | string[];
  volume?: number;
  baseUrl?: string;
  apiVersion?: string;
}

export class CartesiaTTS {
  private readonly apiKey: string;
  private readonly model: string;
  private readonly voice: string;
  private readonly language: string;
  private readonly sampleRate: number;
  private readonly speed?: string | number;
  private readonly emotion?: string[];
  private readonly volume?: number;
  private readonly baseUrl: string;
  private readonly apiVersion: string;

  constructor(apiKey: string, opts: CartesiaTTSOptions = {}) {
    this.apiKey = apiKey;
    this.model = opts.model ?? 'sonic-3';
    this.voice = opts.voice ?? CARTESIA_DEFAULT_VOICE_ID;
    this.language = opts.language ?? 'en';
    this.sampleRate = opts.sampleRate ?? 16000;
    this.speed = opts.speed;
    this.emotion =
      typeof opts.emotion === 'string' ? [opts.emotion] : opts.emotion;
    this.volume = opts.volume;
    this.baseUrl = opts.baseUrl ?? CARTESIA_BASE_URL;
    this.apiVersion = opts.apiVersion ?? CARTESIA_API_VERSION;
  }

  /** Build the JSON payload for the Cartesia bytes endpoint. */
  private buildPayload(text: string): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      model_id: this.model,
      voice: { mode: 'id', id: this.voice },
      transcript: text,
      output_format: {
        container: 'raw',
        encoding: 'pcm_s16le',
        sample_rate: this.sampleRate,
      },
      language: this.language,
    };

    const generationConfig: Record<string, unknown> = {};
    if (this.speed !== undefined) generationConfig.speed = this.speed;
    if (this.emotion && this.emotion.length > 0)
      generationConfig.emotion = this.emotion[0];
    if (this.volume !== undefined) generationConfig.volume = this.volume;
    if (Object.keys(generationConfig).length > 0) {
      payload.generation_config = generationConfig;
    }

    return payload;
  }

  /** Synthesize text and return the concatenated audio buffer. */
  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /**
   * Synthesize text and yield raw PCM_S16LE chunks at the configured
   * `sampleRate` as they arrive from Cartesia.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(`${this.baseUrl}/tts/bytes`, {
      method: 'POST',
      headers: {
        'X-API-Key': this.apiKey,
        'Cartesia-Version': this.apiVersion,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Cartesia TTS error ${response.status}: ${body}`);
    }

    if (!response.body) {
      throw new Error('Cartesia TTS: no response body');
    }

    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          yield Buffer.from(value);
        }
      }
    } finally {
      if (typeof reader.cancel === 'function')
        await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}

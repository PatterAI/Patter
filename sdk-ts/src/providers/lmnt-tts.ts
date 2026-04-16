// Portions of this file are adapted from LiveKit Agents (Apache License 2.0):
//   https://github.com/livekit/agents
//   livekit-plugins/livekit-plugins-lmnt/livekit/plugins/lmnt/tts.py
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
 * LMNT TTS provider — HTTP `/v1/ai/speech/bytes` endpoint.
 *
 * Defaults to `format='raw'` (PCM_S16LE) at 16 kHz so the output drops
 * directly into Patter's telephony pipeline without transcoding.
 */

const LMNT_BASE_URL = 'https://api.lmnt.com/v1/ai/speech/bytes';

export type LMNTAudioFormat = 'aac' | 'mp3' | 'mulaw' | 'raw' | 'wav';
export type LMNTModel = 'blizzard' | 'aurora';
export type LMNTSampleRate = 8000 | 16000 | 24000;

export interface LMNTTTSOptions {
  model?: LMNTModel;
  voice?: string;
  language?: string;
  format?: LMNTAudioFormat;
  sampleRate?: LMNTSampleRate;
  temperature?: number;
  topP?: number;
  baseUrl?: string;
}

export class LMNTTTS {
  private readonly apiKey: string;
  private readonly model: LMNTModel;
  private readonly voice: string;
  private readonly language: string;
  private readonly format: LMNTAudioFormat;
  private readonly sampleRate: LMNTSampleRate;
  private readonly temperature: number;
  private readonly topP: number;
  private readonly baseUrl: string;

  constructor(apiKey: string, opts: LMNTTTSOptions = {}) {
    this.apiKey = apiKey;
    this.model = opts.model ?? 'blizzard';
    this.voice = opts.voice ?? 'leah';
    // Mirror the upstream language defaults: blizzard => auto, else => en.
    this.language =
      opts.language ?? (this.model === 'blizzard' ? 'auto' : 'en');
    this.format = opts.format ?? 'raw';
    this.sampleRate = opts.sampleRate ?? 16000;
    this.temperature = opts.temperature ?? 1.0;
    this.topP = opts.topP ?? 0.8;
    this.baseUrl = opts.baseUrl ?? LMNT_BASE_URL;
  }

  private buildPayload(text: string): Record<string, unknown> {
    return {
      text,
      voice: this.voice,
      language: this.language,
      sample_rate: this.sampleRate,
      model: this.model,
      format: this.format,
      temperature: this.temperature,
      top_p: this.topP,
    };
  }

  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /** Yield audio chunks as they arrive — raw PCM_S16LE by default. */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(this.baseUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': this.apiKey,
      },
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`LMNT TTS error ${response.status}: ${body}`);
    }

    if (!response.body) {
      throw new Error('LMNT TTS: no response body');
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

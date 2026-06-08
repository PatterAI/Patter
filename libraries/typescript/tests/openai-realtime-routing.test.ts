import { describe, it, expect } from 'vitest';
import { Realtime as OpenAIRealtime } from '../src/engines/openai';
import { Realtime2 as OpenAIRealtime2 } from '../src/engines/openai-2';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import { buildAIAdapter, type LocalConfig } from '../src/server';
import type { AgentOptions } from '../src/types';

/**
 * Regression guard for issue #154 — "Twilio + OpenAI Realtime g711_ulaw
 * produces garbled/static audio on all models".
 *
 * The v1 `OpenAIRealtime()` engine used to route through the v1-beta
 * `OpenAIRealtimeAdapter`, which sent the legacy flat
 * `output_audio_format: g711_ulaw` session shape to the GA endpoint. OpenAI
 * deprecated the Beta Realtime API, so GA models (the v1 engine defaults to
 * `gpt-realtime-mini`) ignore the flat field and fall back to PCM16 @ 24 kHz.
 * Patter then forwarded those 24 kHz PCM bytes to Twilio framed as 8 kHz
 * mulaw — producing static + breaking inbound STT.
 *
 * The fix routes BOTH engines through the GA adapter, which sends the nested
 * `audio.{input,output}.format = { type: 'audio/pcm', rate: 24000 }` shape and
 * transcodes PCM24→mulaw8 internally — matching the Python SDK, which already
 * unified this routing in `stream_handler.py`.
 */

const CONFIG: LocalConfig = {
  phoneNumber: '+15555550100',
  webhookUrl: 'https://example.com/voice',
};

function buildGA(adapter: unknown): Record<string, unknown> {
  return (adapter as { buildGASessionConfig(): Record<string, unknown> }).buildGASessionConfig();
}

function audioFormat(
  config: Record<string, unknown>,
  dir: 'input' | 'output',
): { type?: string; rate?: number } | undefined {
  const audio = config.audio as
    | { input?: { format?: { type?: string; rate?: number } }; output?: { format?: { type?: string; rate?: number } } }
    | undefined;
  return audio?.[dir]?.format;
}

describe('[unit] OpenAI Realtime engine routing (issue #154)', () => {
  it('routes the v1 OpenAIRealtime() engine through the GA adapter, not the v1-beta adapter', () => {
    const agent: AgentOptions = {
      systemPrompt: 'You are helpful.',
      engine: new OpenAIRealtime({ apiKey: 'sk-test' }),
    };
    const adapter = buildAIAdapter(CONFIG, agent);
    expect(adapter).toBeInstanceOf(OpenAIRealtime2Adapter);
    // Still a subclass of the v1 adapter, so the stream-handler
    // `instanceof OpenAIRealtimeAdapter` feature gates (barge-in, sendText,
    // cancelResponse, image/progress) keep matching.
    expect(adapter).toBeInstanceOf(OpenAIRealtimeAdapter);
  });

  it('routes the GA OpenAIRealtime2() engine through the GA adapter', () => {
    const agent: AgentOptions = {
      systemPrompt: 'You are helpful.',
      engine: new OpenAIRealtime2({ apiKey: 'sk-test' }),
    };
    expect(buildAIAdapter(CONFIG, agent)).toBeInstanceOf(OpenAIRealtime2Adapter);
  });

  it('the v1-engine adapter builds the nested GA audio format (audio/pcm@24000), NOT the flat g711_ulaw field', () => {
    const adapter = buildAIAdapter(CONFIG, {
      systemPrompt: 'You are helpful.',
      engine: new OpenAIRealtime({ apiKey: 'sk-test' }),
    });
    const config = buildGA(adapter);
    expect(audioFormat(config, 'output')).toEqual({ type: 'audio/pcm', rate: 24000 });
    expect(audioFormat(config, 'input')).toEqual({ type: 'audio/pcm', rate: 24000 });
    // The bug was the flat field reaching a GA model — it must NOT be present.
    expect('output_audio_format' in config).toBe(false);
    expect('input_audio_format' in config).toBe(false);
    // GA shape markers.
    expect(config.type).toBe('realtime');
  });
});

describe('[unit] GA adapter outbound transcode yields mulaw 8 kHz for Twilio (issue #154)', () => {
  it('converts PCM16-LE 24 kHz → mulaw 8 kHz (3:1 decimation, 1 byte/sample)', () => {
    const adapter = buildAIAdapter(CONFIG, {
      systemPrompt: 'You are helpful.',
      engine: new OpenAIRealtime({ apiKey: 'sk-test' }),
    });

    // 1 s of a 440 Hz tone at 24 kHz, PCM16-LE — the format GA actually returns.
    const samples = 24000;
    const pcm24 = Buffer.alloc(samples * 2);
    for (let i = 0; i < samples; i++) {
      pcm24.writeInt16LE(Math.round(16383 * Math.sin((2 * Math.PI * 440 * i) / 24000)), i * 2);
    }

    const transcode = (
      adapter as unknown as { transcodeOutboundPcm24ToMulaw8Buffer(b64: string): Buffer }
    ).transcodeOutboundPcm24ToMulaw8Buffer.bind(adapter);
    const mulaw = transcode(pcm24.toString('base64'));

    expect(mulaw.length).toBeGreaterThan(0);
    // mulaw 8 kHz is far smaller than the PCM16 24 kHz input it came from.
    expect(mulaw.length).toBeLessThan(pcm24.length / 2);
    // 24 kHz → 8 kHz ≈ 8000 mulaw bytes for 1 s; allow ±10% for resampler warm-up.
    expect(Math.abs(mulaw.length - samples / 3)).toBeLessThan((samples / 3) * 0.1);
  });
});

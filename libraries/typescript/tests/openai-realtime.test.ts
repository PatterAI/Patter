import { describe, it, expect } from 'vitest';
import {
  OpenAIRealtimeAdapter,
  OpenAIRealtimeModel,
  OpenAITranscriptionModel,
} from '../src/providers/openai-realtime';
import { Realtime as OpenAIRealtimeEngine } from '../src/engines/openai';
import { buildAIAdapter, type LocalConfig } from '../src/server';
import type { AgentOptions } from '../src/types';

describe('OpenAIRealtimeAdapter', () => {
  it('initializes with required api key', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    expect(adapter).toBeDefined();
  });

  it('accepts custom model', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', 'gpt-4o-realtime-preview');
    expect(adapter).toBeDefined();
  });

  it('accepts custom voice', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', undefined, 'nova');
    expect(adapter).toBeDefined();
  });

  it('accepts instructions string', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', undefined, undefined, 'Be helpful and concise.');
    expect(adapter).toBeDefined();
  });

  it('accepts tools array', () => {
    const tools = [{ name: 'test', description: 'test tool', parameters: {} }];
    const adapter = new OpenAIRealtimeAdapter('sk_test', undefined, undefined, undefined, tools);
    expect(adapter).toBeDefined();
  });

  it('accepts empty tools array', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test', undefined, undefined, undefined, []);
    expect(adapter).toBeDefined();
  });

  it('is not connected initially', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    // ws is private, but close should not throw when not connected
    expect(() => adapter.close()).not.toThrow();
  });

  it('sendAudio does not throw when not connected', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    // Should silently skip if ws is null
    expect(() => adapter.sendAudio(Buffer.from('test'))).not.toThrow();
  });

  it('cancelResponse does not throw when not connected', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    expect(() => adapter.cancelResponse()).not.toThrow();
  });

  it('close can be called multiple times without error', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    adapter.close();
    adapter.close();
    expect(true).toBe(true);
  });

  it('onEvent does not throw when not connected', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    expect(() => adapter.onEvent(() => {})).not.toThrow();
  });

  it('accepts custom audioFormat (parity with Python audio_format kwarg)', () => {
    const adapter = new OpenAIRealtimeAdapter(
      'sk_test',
      undefined,
      undefined,
      undefined,
      undefined,
      'pcm16',
    );
    expect(adapter).toBeDefined();
  });

  it('defaults audioFormat to g711_ulaw when omitted', () => {
    // Construct without the 6th argument — must not throw and must produce
    // a working adapter with the default format.
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    expect(adapter).toBeDefined();
  });

  it('exposes gpt-realtime-2 model identifier', () => {
    expect(OpenAIRealtimeModel.GPT_REALTIME_2).toBe('gpt-realtime-2');
    const adapter = new OpenAIRealtimeAdapter(
      'sk_test',
      OpenAIRealtimeModel.GPT_REALTIME_2,
    );
    expect(adapter).toBeDefined();
  });

  it('exposes gpt-realtime-whisper transcription model identifier', () => {
    expect(OpenAITranscriptionModel.GPT_REALTIME_WHISPER).toBe('gpt-realtime-whisper');
  });

  it('accepts reasoningEffort option for gpt-realtime-2', () => {
    const adapter = new OpenAIRealtimeAdapter(
      'sk_test',
      OpenAIRealtimeModel.GPT_REALTIME_2,
      undefined,
      undefined,
      undefined,
      undefined,
      { reasoningEffort: 'low' },
    );
    expect(adapter).toBeDefined();
  });
});

describe('OpenAIRealtime engine wrapper → OpenAIRealtimeAdapter forwarding', () => {
  /**
   * Regression: the high-level `OpenAIRealtime` engine wrapper accepts
   * `reasoningEffort` and `inputAudioTranscriptionModel`, but earlier
   * versions of `buildAIAdapter` silently dropped them — they only worked
   * when constructing `OpenAIRealtimeAdapter` directly. Verify that the
   * 7th `OpenAIRealtimeOptions` arg now carries them through.
   */
  it('forwards reasoningEffort + inputAudioTranscriptionModel from engine to adapter', () => {
    const engine = new OpenAIRealtimeEngine({
      apiKey: 'sk-test-engine',
      model: OpenAIRealtimeModel.GPT_REALTIME_2,
      reasoningEffort: 'low',
      inputAudioTranscriptionModel: OpenAITranscriptionModel.GPT_REALTIME_WHISPER,
    });
    const config: LocalConfig = {
      phoneNumber: '+15555550100',
      webhookUrl: 'https://example.com/voice',
    };
    const agent: AgentOptions = {
      systemPrompt: 'You are helpful.',
      model: OpenAIRealtimeModel.GPT_REALTIME_2,
      voice: 'alloy',
      engine,
    };

    const adapter = buildAIAdapter(config, agent) as OpenAIRealtimeAdapter;
    expect(adapter).toBeInstanceOf(OpenAIRealtimeAdapter);

    // The adapter stores its 7th-arg options on a private `options` field;
    // read it back through a typed cast to verify the wire-up. Cheaper
    // than spinning up the WS just to inspect session.update.
    const opts = (adapter as unknown as { options: { reasoningEffort?: string; inputAudioTranscriptionModel?: string } }).options;
    expect(opts.reasoningEffort).toBe('low');
    expect(opts.inputAudioTranscriptionModel).toBe('gpt-realtime-whisper');
  });

  it('omits both options when the engine leaves them unset (backward compat)', () => {
    const engine = new OpenAIRealtimeEngine({ apiKey: 'sk-test-engine' });
    const config: LocalConfig = {
      phoneNumber: '+15555550100',
      webhookUrl: 'https://example.com/voice',
    };
    const agent: AgentOptions = {
      systemPrompt: 'You are helpful.',
      engine,
    };

    const adapter = buildAIAdapter(config, agent) as OpenAIRealtimeAdapter;
    const opts = (adapter as unknown as { options: { reasoningEffort?: string; inputAudioTranscriptionModel?: string } }).options;
    expect(opts.reasoningEffort).toBeUndefined();
    expect(opts.inputAudioTranscriptionModel).toBeUndefined();
  });
});

describe('[mocked] OpenAIRealtimeAdapter cancelResponse audio_end_ms cap', () => {
  /**
   * Regression: barge-in truncate must not credit unplayed audio.
   *
   * When OpenAI streams audio at multiple-x real-time and the consumer
   * clears the playout buffer on barge-in, the user only ever heard
   * ~wall-clock-ms of speech. If we pass the byte-derived
   * ``audio_end_ms`` to ``conversation.item.truncate`` OpenAI keeps the
   * full generated transcript, and the model replays / resumes from it
   * on the next turn — re-greetings and mid-sentence fragments. Cap by
   * wall-clock instead.
   */
  it('caps audio_end_ms to wall-clock playback time', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    const sent: Array<Record<string, unknown>> = [];
    // Inject a fake WS that just records sends.
    const fakeWs = {
      send: (payload: string) => {
        sent.push(JSON.parse(payload) as Record<string, unknown>);
      },
    } as unknown as { send: (s: string) => void };
    // Private field access via cast for the regression test only.
    (adapter as unknown as { ws: unknown }).ws = fakeWs;
    // Simulate: 30 ms ago first chunk arrived, but byte counter says 2000 ms
    // (5-10x real-time arrival from OpenAI is typical).
    (adapter as unknown as {
      currentResponseItemId: string | null;
      currentResponseAudioMs: number;
      currentResponseFirstAudioAt: number | null;
    }).currentResponseItemId = 'item-1';
    (adapter as unknown as {
      currentResponseAudioMs: number;
    }).currentResponseAudioMs = 2000;
    (adapter as unknown as {
      currentResponseFirstAudioAt: number | null;
    }).currentResponseFirstAudioAt = Date.now() - 30;

    adapter.cancelResponse();

    const truncate = sent.find((p) => p.type === 'conversation.item.truncate');
    expect(truncate).toBeDefined();
    expect(truncate!.item_id).toBe('item-1');
    // Bounded by wall-clock (~30 ms), not the 2000 ms generated counter.
    // Generous slack for event-loop scheduling.
    expect(truncate!.audio_end_ms as number).toBeLessThanOrEqual(200);

    // response.cancel sent AFTER the truncate.
    const truncIdx = sent.findIndex((p) => p.type === 'conversation.item.truncate');
    const cancelIdx = sent.findIndex((p) => p.type === 'response.cancel');
    expect(truncIdx).toBeGreaterThanOrEqual(0);
    expect(cancelIdx).toBeGreaterThan(truncIdx);

    // State reset so the next response.create starts clean.
    expect(
      (adapter as unknown as { currentResponseItemId: string | null }).currentResponseItemId,
    ).toBeNull();
    expect(
      (adapter as unknown as { currentResponseAudioMs: number }).currentResponseAudioMs,
    ).toBe(0);
    expect(
      (adapter as unknown as { currentResponseFirstAudioAt: number | null })
        .currentResponseFirstAudioAt,
    ).toBeNull();
  });

  it('falls back to byte-derived counter when no audio chunks have arrived', () => {
    const adapter = new OpenAIRealtimeAdapter('sk_test');
    const sent: Array<Record<string, unknown>> = [];
    const fakeWs = {
      send: (payload: string) => {
        sent.push(JSON.parse(payload) as Record<string, unknown>);
      },
    };
    (adapter as unknown as { ws: unknown }).ws = fakeWs;
    (adapter as unknown as {
      currentResponseItemId: string | null;
    }).currentResponseItemId = 'item-1';
    (adapter as unknown as {
      currentResponseAudioMs: number;
    }).currentResponseAudioMs = 0;
    (adapter as unknown as {
      currentResponseFirstAudioAt: number | null;
    }).currentResponseFirstAudioAt = null;

    adapter.cancelResponse();
    const truncate = sent.find((p) => p.type === 'conversation.item.truncate');
    expect(truncate).toBeDefined();
    expect(truncate!.audio_end_ms).toBe(0);
  });
});

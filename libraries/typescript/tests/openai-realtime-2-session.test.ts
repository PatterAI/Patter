import { describe, it, expect } from 'vitest';
import { OpenAIRealtime2Adapter } from '../src/providers/openai-realtime-2';
import type { OpenAIRealtimeOptions } from '../src/providers/openai-realtime';

/**
 * Build the GA (`gpt-realtime-2`) `session.update` payload directly via the
 * adapter's private builder. The `private` keyword in TypeScript is a
 * compile-time-only annotation — at runtime the method is a plain property,
 * so an `as` cast reaches it without spinning up a WebSocket. This keeps the
 * assertion authentic: it exercises the real builder, not a re-implementation.
 */
function buildGA(opts: OpenAIRealtimeOptions): Record<string, unknown> {
  const adapter = new OpenAIRealtime2Adapter(
    'sk_test',
    'gpt-realtime-2',
    'alloy',
    'You are helpful.',
    [],
    undefined,
    opts,
  );
  return (adapter as unknown as { buildGASessionConfig(): Record<string, unknown> })
    .buildGASessionConfig();
}

function audioInput(config: Record<string, unknown>): Record<string, unknown> {
  return (config.audio as { input: Record<string, unknown> }).input;
}

describe('[unit] OpenAIRealtime2Adapter GA noise reduction', () => {
  it("nests input_audio_noise_reduction under session.audio.input when set to 'far_field'", () => {
    const config = buildGA({ noiseReduction: 'far_field' });
    expect(audioInput(config).input_audio_noise_reduction).toEqual({ type: 'far_field' });
  });

  it("accepts 'near_field'", () => {
    const config = buildGA({ noiseReduction: 'near_field' });
    expect(audioInput(config).input_audio_noise_reduction).toEqual({ type: 'near_field' });
  });

  it('OMITS the noise-reduction key entirely when unset (today behavior)', () => {
    const config = buildGA({});
    expect('input_audio_noise_reduction' in audioInput(config)).toBe(false);
  });
});

describe('[unit] OpenAIRealtime2Adapter GA turn detection', () => {
  it('respects an explicit server_vad threshold + silence over the defaults', () => {
    const config = buildGA({
      turnDetection: { type: 'server_vad', threshold: 0.6, silenceDurationMs: 500 },
    });
    const td = audioInput(config).turn_detection as Record<string, unknown>;
    expect(td.type).toBe('server_vad');
    expect(td.threshold).toBe(0.6);
    expect(td.silence_duration_ms).toBe(500);
    // Unset prefix falls back to the adapter default.
    expect(td.prefix_padding_ms).toBe(300);
    // Client-gated barge-in safety values stay false (never exposed).
    expect(td.create_response).toBe(false);
    expect(td.interrupt_response).toBe(false);
  });

  it('falls back each unset server_vad field to the current adapter default', () => {
    const config = buildGA({ turnDetection: { type: 'server_vad', threshold: 0.7 } });
    const td = audioInput(config).turn_detection as Record<string, unknown>;
    expect(td.threshold).toBe(0.7);
    expect(td.prefix_padding_ms).toBe(300);
    expect(td.silence_duration_ms).toBe(300);
  });

  it("emits semantic_vad with eagerness and NO threshold/prefix/silence", () => {
    const config = buildGA({ turnDetection: { type: 'semantic_vad', eagerness: 'low' } });
    const td = audioInput(config).turn_detection as Record<string, unknown>;
    expect(td.type).toBe('semantic_vad');
    expect(td.eagerness).toBe('low');
    expect('threshold' in td).toBe(false);
    expect('prefix_padding_ms' in td).toBe(false);
    expect('silence_duration_ms' in td).toBe(false);
    // Safety values still emitted.
    expect(td.create_response).toBe(false);
    expect(td.interrupt_response).toBe(false);
  });

  it('omits semantic_vad eagerness when not provided (server default applies)', () => {
    const config = buildGA({ turnDetection: { type: 'semantic_vad' } });
    const td = audioInput(config).turn_detection as Record<string, unknown>;
    expect(td.type).toBe('semantic_vad');
    expect('eagerness' in td).toBe(false);
  });
});

describe('[unit] OpenAIRealtime2Adapter GA defaults unchanged (regression guard)', () => {
  it('produces the exact pre-change turn_detection literal and no noise key when no knobs are set', () => {
    const config = buildGA({});
    const td = audioInput(config).turn_detection as Record<string, unknown>;
    expect(td).toEqual({
      type: 'server_vad',
      threshold: 0.5,
      prefix_padding_ms: 300,
      silence_duration_ms: 300,
      create_response: false,
      interrupt_response: false,
    });
    expect('input_audio_noise_reduction' in audioInput(config)).toBe(false);
  });

  it('honors a pre-existing silenceDurationMs option as the server_vad default', () => {
    const config = buildGA({ silenceDurationMs: 700 });
    const td = audioInput(config).turn_detection as Record<string, unknown>;
    expect(td.silence_duration_ms).toBe(700);
  });
});

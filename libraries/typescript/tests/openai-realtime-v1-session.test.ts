import { describe, it, expect } from 'vitest';
import { OpenAIRealtimeAdapter } from '../src/providers/openai-realtime';
import type { OpenAIRealtimeOptions } from '../src/providers/openai-realtime';

/**
 * Build the v1-beta `session.update` payload directly via the adapter's
 * private builder. `private` is compile-time only, so an `as` cast reaches
 * the real method at runtime without opening a WebSocket.
 */
function buildV1(opts: OpenAIRealtimeOptions): Record<string, unknown> {
  const adapter = new OpenAIRealtimeAdapter(
    'sk_test',
    'gpt-realtime',
    'alloy',
    'You are helpful.',
    [],
    undefined,
    opts,
  );
  return (adapter as unknown as { buildSessionConfig(): Record<string, unknown> })
    .buildSessionConfig();
}

describe('[unit] OpenAIRealtimeAdapter v1 noise reduction', () => {
  it('injects input_audio_noise_reduction at the TOP LEVEL of session (not nested)', () => {
    const config = buildV1({ noiseReduction: 'far_field' });
    expect(config.input_audio_noise_reduction).toEqual({ type: 'far_field' });
    // v1 has flat audio config — there is no `audio.input` nesting.
    expect(config.audio).toBeUndefined();
  });

  it('omits the noise-reduction key entirely when unset', () => {
    const config = buildV1({});
    expect('input_audio_noise_reduction' in config).toBe(false);
  });
});

describe('[unit] OpenAIRealtimeAdapter v1 turn detection', () => {
  it('respects an explicit server_vad threshold over the default', () => {
    const config = buildV1({ turnDetection: { type: 'server_vad', threshold: 0.6 } });
    const td = config.turn_detection as Record<string, unknown>;
    expect(td.type).toBe('server_vad');
    expect(td.threshold).toBe(0.6);
    expect(td.prefix_padding_ms).toBe(300);
    expect(td.silence_duration_ms).toBe(300);
    // v1 turn_detection carries NO create_response / interrupt_response keys.
    expect('create_response' in td).toBe(false);
    expect('interrupt_response' in td).toBe(false);
  });

  it('emits semantic_vad with eagerness and no threshold', () => {
    const config = buildV1({ turnDetection: { type: 'semantic_vad', eagerness: 'low' } });
    const td = config.turn_detection as Record<string, unknown>;
    expect(td.type).toBe('semantic_vad');
    expect(td.eagerness).toBe('low');
    expect('threshold' in td).toBe(false);
  });

  it('produces the exact pre-change literal when no knobs are set (regression guard)', () => {
    const config = buildV1({});
    expect(config.turn_detection).toEqual({
      type: 'server_vad',
      threshold: 0.5,
      prefix_padding_ms: 300,
      silence_duration_ms: 300,
    });
  });

  it('OMITS create_response / interrupt_response in DEFAULT (server-managed) mode — v1 server defaults are already true (issue #154)', () => {
    const config = buildV1({});
    const td = config.turn_detection as Record<string, unknown>;
    // v1 carries no gating keys. The OpenAI v1 server defaults
    // (create_response: true, interrupt_response: true) ARE the server-managed
    // behaviour we want, so omitting is equivalent to sending true.
    expect('create_response' in td).toBe(false);
    expect('interrupt_response' in td).toBe(false);
  });

  it('STILL omits the gating keys even in legacy opt-out mode (v1 has no response-gating wire shape)', () => {
    // gateResponseOnTranscript only affects the GA wire keys. On v1 the keys
    // are never emitted; the stream handler reads the flag via
    // getGateResponseOnTranscript() to decide the client-managed path. (Note:
    // buildAIAdapter routes the v1 engine through the GA adapter for telephony;
    // buildSessionConfig is exercised by warmup / parked-connection paths.)
    const config = buildV1({ gateResponseOnTranscript: true });
    const td = config.turn_detection as Record<string, unknown>;
    expect('create_response' in td).toBe(false);
    expect('interrupt_response' in td).toBe(false);
  });
});

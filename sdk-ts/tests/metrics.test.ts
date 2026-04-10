import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { CallMetricsAccumulator } from '../src/metrics';

describe('CallMetricsAccumulator', () => {
  it('creates with required fields', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c1',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      sttProvider: 'deepgram',
      ttsProvider: 'elevenlabs',
    });
    expect(acc.callId).toBe('c1');
    expect(acc.providerMode).toBe('pipeline');
    expect(acc.telephonyProvider).toBe('twilio');
  });

  it('tracks a complete turn lifecycle', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c2',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      sttProvider: 'deepgram',
      ttsProvider: 'elevenlabs',
    });

    acc.startTurn();
    acc.recordSttComplete('Hello', 2.0);
    acc.recordLlmComplete();
    acc.recordTtsFirstByte();
    acc.recordTtsComplete('Hi there');
    const turn = acc.recordTurnComplete('Hi there');

    expect(turn.turn_index).toBe(0);
    expect(turn.user_text).toBe('Hello');
    expect(turn.agent_text).toBe('Hi there');
    expect(turn.stt_audio_seconds).toBe(2.0);
    expect(turn.tts_characters).toBe(8);
    expect(turn.latency.total_ms).toBeGreaterThanOrEqual(0);
  });

  it('handles interrupted turns', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c3',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
    });

    // No turn in progress
    expect(acc.recordTurnInterrupted()).toBeNull();

    // Start turn then interrupt
    acc.startTurn();
    acc.recordSttComplete('Hey');
    const turn = acc.recordTurnInterrupted();
    expect(turn).not.toBeNull();
    expect(turn!.agent_text).toBe('[interrupted]');
    expect(turn!.tts_characters).toBe(0);
  });

  it('computes cost for pipeline mode', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c4',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      sttProvider: 'deepgram',
      ttsProvider: 'elevenlabs',
    });

    // Simulate a 60-second call with STT audio and TTS text
    acc.startTurn();
    acc.recordSttComplete('Test', 30);
    acc.recordLlmComplete();
    acc.recordTtsFirstByte();
    acc.recordTtsComplete('Response text here'); // 18 chars
    acc.recordTurnComplete('Response text here');

    const cost = acc.getCostSoFar();
    expect(cost.stt).toBeGreaterThan(0); // deepgram cost for 30s
    expect(cost.tts).toBeGreaterThan(0); // elevenlabs cost for 18 chars
    // telephony cost may be ~0 due to sub-millisecond elapsed time in tests
    expect(cost.telephony).toBeGreaterThanOrEqual(0);
    expect(cost.total).toBeGreaterThan(0); // stt + tts dominate
  });

  it('computes cost for openai_realtime mode', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c5',
      providerMode: 'openai_realtime',
      telephonyProvider: 'twilio',
    });

    acc.recordRealtimeUsage({
      input_token_details: { audio_tokens: 100, text_tokens: 0 },
      output_token_details: { audio_tokens: 50, text_tokens: 0 },
    });

    const cost = acc.getCostSoFar();
    expect(cost.llm).toBeGreaterThan(0);
    expect(cost.stt).toBe(0);
    expect(cost.tts).toBe(0);
  });

  it('endCall returns final metrics with averages and p95', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c6',
      providerMode: 'pipeline',
      telephonyProvider: 'telnyx',
      sttProvider: 'deepgram',
      ttsProvider: 'openai_tts',
    });

    // Record two turns
    for (let i = 0; i < 2; i++) {
      acc.startTurn();
      acc.recordSttComplete(`turn ${i}`, 1);
      acc.recordLlmComplete();
      acc.recordTtsFirstByte();
      acc.recordTtsComplete(`response ${i}`);
      acc.recordTurnComplete(`response ${i}`);
    }

    const metrics = acc.endCall();
    expect(metrics.call_id).toBe('c6');
    expect(metrics.turns).toHaveLength(2);
    expect(metrics.duration_seconds).toBeGreaterThanOrEqual(0);
    expect(metrics.cost.total).toBeGreaterThanOrEqual(0);
    expect(metrics.latency_avg.total_ms).toBeGreaterThanOrEqual(0);
    expect(metrics.latency_p95.total_ms).toBeGreaterThanOrEqual(0);
    expect(metrics.provider_mode).toBe('pipeline');
    expect(metrics.telephony_provider).toBe('telnyx');
  });

  it('respects actual cost overrides', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c7',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      sttProvider: 'deepgram',
      ttsProvider: 'elevenlabs',
    });

    acc.setActualTelephonyCost(0.05);
    acc.setActualSttCost(0.02);

    const cost = acc.getCostSoFar();
    expect(cost.telephony).toBe(0.05);
    expect(cost.stt).toBe(0.02);
  });

  it('computes STT audio from bytes when not tracked', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'c8',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      sttProvider: 'deepgram',
      ttsProvider: 'elevenlabs',
    });

    // 16000 Hz * 2 bytes/sample * 10 seconds = 320000 bytes
    acc.addSttAudioBytes(320000);
    const metrics = acc.endCall();
    // STT cost should be for ~10 seconds
    expect(metrics.cost.stt).toBeGreaterThan(0);
  });
});

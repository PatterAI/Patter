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

  it('recordTurnComplete is a no-op after recordTurnInterrupted on the same turn', () => {
    // Repro of the VAD-barge-in / pipeline-LLM race documented in
    // BUGS.md (2026-05-05). The barge-in path closes the turn with
    // recordTurnInterrupted while the in-flight pipeline LLM stream
    // eventually unwinds and reaches recordTurnComplete. Without the
    // guard, the late call would push a phantom turn with user_text=''
    // (since _resetTurnState cleared the field) and agent_text from
    // the cancelled LLM stream.
    const acc = new CallMetricsAccumulator({
      callId: 'race1',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
    });

    acc.startTurn();
    acc.recordSttComplete('Hello');
    const interrupted = acc.recordTurnInterrupted();
    expect(interrupted).not.toBeNull();
    expect(interrupted!.user_text).toBe('Hello');
    expect(interrupted!.agent_text).toBe('[interrupted]');

    // Late pipeline-LLM unwind reaches recordTurnComplete with the
    // cancelled responseText — must be silently dropped.
    const late = acc.recordTurnComplete('partial LLM output');
    expect(late).toBeNull();

    // Only the interrupted turn is recorded.
    const result = acc.endCall();
    expect(result.turns).toHaveLength(1);
    expect(result.turns[0].agent_text).toBe('[interrupted]');
    expect(result.turns[0].user_text).toBe('Hello');
  });

  it('recordTurnInterrupted is a no-op after recordTurnComplete on the same turn', () => {
    // Bidirectional parity: a late recordTurnInterrupted after
    // recordTurnComplete on the same turn must also be a no-op. The
    // current caller ordering can't trigger this (the VAD bargein path
    // fires the interrupt FIRST and the LLM-unwind path then calls
    // complete second, guarded by the existing one-directional guard).
    // The symmetric guard hardens the accumulator against a future
    // refactor that reorders those paths.
    const acc = new CallMetricsAccumulator({
      callId: 'race-bi',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
    });

    acc.startTurn();
    acc.recordSttComplete('Hello');
    const completed = acc.recordTurnComplete('Hi there');
    expect(completed).not.toBeNull();
    expect(completed!.user_text).toBe('Hello');
    expect(completed!.agent_text).toBe('Hi there');

    // Late VAD-bargein interruption arrives after the complete —
    // must be silently dropped.
    const late = acc.recordTurnInterrupted();
    expect(late).toBeNull();

    // Only the completed turn is recorded.
    const result = acc.endCall();
    expect(result.turns).toHaveLength(1);
    expect(result.turns[0].agent_text).toBe('Hi there');
  });

  it('startTurn re-arms the accumulator after an interrupted turn', () => {
    const acc = new CallMetricsAccumulator({
      callId: 'race2',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
    });

    acc.startTurn();
    acc.recordSttComplete('Hello');
    acc.recordTurnInterrupted();
    expect(acc.recordTurnComplete('dropped')).toBeNull();

    // New turn begins.
    acc.startTurn();
    acc.recordSttComplete('Second turn');
    const completed = acc.recordTurnComplete('Reply');
    expect(completed).not.toBeNull();
    expect(completed!.user_text).toBe('Second turn');
    expect(completed!.agent_text).toBe('Reply');
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

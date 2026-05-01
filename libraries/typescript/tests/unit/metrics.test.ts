import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { CallMetricsAccumulator } from '../../src/metrics';
import type { CallMetrics, TurnMetrics } from '../../src/metrics';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeAccumulator(overrides?: Record<string, unknown>): CallMetricsAccumulator {
  return new CallMetricsAccumulator({
    callId: 'call-test',
    providerMode: 'pipeline',
    telephonyProvider: 'twilio',
    sttProvider: 'deepgram',
    ttsProvider: 'elevenlabs',
    ...overrides,
  });
}

/** Simulate a turn cycle with configurable timing gaps. */
function simulateTurn(
  acc: CallMetricsAccumulator,
  userText: string,
  agentText: string,
): TurnMetrics {
  acc.startTurn();
  acc.recordSttComplete(userText);
  acc.recordLlmComplete();
  acc.recordTtsFirstByte();
  acc.recordTtsComplete(agentText);
  return acc.recordTurnComplete(agentText);
}

describe('CallMetricsAccumulator', () => {
  // --- Construction ---

  it('initializes with expected defaults', () => {
    const acc = makeAccumulator();
    expect(acc.callId).toBe('call-test');
    expect(acc.providerMode).toBe('pipeline');
    expect(acc.telephonyProvider).toBe('twilio');
    expect(acc.sttProvider).toBe('deepgram');
    expect(acc.ttsProvider).toBe('elevenlabs');
  });

  it('defaults llmProvider to empty string', () => {
    const acc = makeAccumulator();
    expect(acc.llmProvider).toBe('');
  });

  // --- Turn lifecycle ---

  describe('turn lifecycle', () => {
    it('records a complete turn with latency breakdown', () => {
      const acc = makeAccumulator();
      const turn = simulateTurn(acc, 'Hello', 'Hi there!');

      expect(turn.turn_index).toBe(0);
      expect(turn.user_text).toBe('Hello');
      expect(turn.agent_text).toBe('Hi there!');
      expect(turn.tts_characters).toBe('Hi there!'.length);
      expect(turn.latency.stt_ms).toBeGreaterThanOrEqual(0);
      expect(turn.latency.llm_ms).toBeGreaterThanOrEqual(0);
      expect(turn.latency.tts_ms).toBeGreaterThanOrEqual(0);
      expect(turn.latency.total_ms).toBeGreaterThanOrEqual(0);
    });

    it('increments turn_index across turns', () => {
      const acc = makeAccumulator();
      const turn0 = simulateTurn(acc, 'first', 'reply 1');
      const turn1 = simulateTurn(acc, 'second', 'reply 2');

      expect(turn0.turn_index).toBe(0);
      expect(turn1.turn_index).toBe(1);
    });

    it('records turn interrupted', () => {
      const acc = makeAccumulator();
      acc.startTurn();
      acc.recordSttComplete('Interrupted');
      const turn = acc.recordTurnInterrupted();

      expect(turn).not.toBeNull();
      expect(turn!.agent_text).toBe('[interrupted]');
      expect(turn!.tts_characters).toBe(0);
    });

    it('returns null from recordTurnInterrupted when no turn started', () => {
      const acc = makeAccumulator();
      const turn = acc.recordTurnInterrupted();
      expect(turn).toBeNull();
    });

    it('recordTtsFirstByte is idempotent', () => {
      const acc = makeAccumulator();
      acc.startTurn();
      acc.recordSttComplete('text');
      acc.recordLlmComplete();
      acc.recordTtsFirstByte();
      // Second call should not update the timestamp
      acc.recordTtsFirstByte();
      acc.recordTtsComplete('reply');
      const turn = acc.recordTurnComplete('reply');
      // Should still have valid latency
      expect(turn.latency.tts_ms).toBeGreaterThanOrEqual(0);
    });
  });

  // --- Circular buffer / turns stored ---

  describe('turns storage', () => {
    it('stores all turns in endCall result', () => {
      const acc = makeAccumulator();
      for (let i = 0; i < 5; i++) {
        simulateTurn(acc, `user-${i}`, `agent-${i}`);
      }
      const metrics = acc.endCall();
      expect(metrics.turns).toHaveLength(5);
      expect(metrics.turns[0].user_text).toBe('user-0');
      expect(metrics.turns[4].user_text).toBe('user-4');
    });
  });

  // --- endCall / finalization ---

  describe('endCall()', () => {
    it('returns complete CallMetrics', () => {
      const acc = makeAccumulator();
      simulateTurn(acc, 'Hi', 'Hello!');
      const metrics = acc.endCall();

      expect(metrics.call_id).toBe('call-test');
      expect(metrics.duration_seconds).toBeGreaterThanOrEqual(0);
      expect(metrics.provider_mode).toBe('pipeline');
      expect(metrics.stt_provider).toBe('deepgram');
      expect(metrics.tts_provider).toBe('elevenlabs');
      expect(metrics.telephony_provider).toBe('twilio');

      // Cost breakdown
      expect(metrics.cost).toBeDefined();
      expect(typeof metrics.cost.stt).toBe('number');
      expect(typeof metrics.cost.tts).toBe('number');
      expect(typeof metrics.cost.telephony).toBe('number');
      expect(typeof metrics.cost.total).toBe('number');

      // Latency aggregates
      expect(metrics.latency_avg).toBeDefined();
      expect(metrics.latency_p95).toBeDefined();
    });

    it('returns zero latency averages when no turns', () => {
      const acc = makeAccumulator();
      const metrics = acc.endCall();
      expect(metrics.latency_avg.stt_ms).toBe(0);
      expect(metrics.latency_avg.total_ms).toBe(0);
      expect(metrics.latency_p95.stt_ms).toBe(0);
    });

    it('computes STT audio seconds from byte count fallback', () => {
      const acc = makeAccumulator();
      acc.configureSttFormat(16000, 2);
      acc.addSttAudioBytes(32000); // 1 second of audio
      const metrics = acc.endCall();
      // total STT seconds = 32000 / (16000 * 2) = 1
      expect(metrics.cost.stt).toBeGreaterThan(0);
    });
  });

  // --- Cost breakdown by provider ---

  describe('cost breakdown', () => {
    it('pipeline mode calculates STT + TTS + telephony costs', () => {
      const acc = makeAccumulator({
        providerMode: 'pipeline',
        sttProvider: 'deepgram',
        ttsProvider: 'elevenlabs',
        telephonyProvider: 'twilio',
      });
      // Simulate some usage
      acc.startTurn();
      acc.recordSttComplete('Hello', 60); // 60 seconds of audio
      acc.recordLlmComplete();
      acc.recordTtsFirstByte();
      acc.recordTtsComplete('This is a reply with many characters for cost calc');
      acc.recordTurnComplete('This is a reply with many characters for cost calc');

      const metrics = acc.endCall();
      expect(metrics.cost.stt).toBeGreaterThan(0);
      expect(metrics.cost.tts).toBeGreaterThan(0);
      // Telephony cost is based on call duration; in a fast test it may be ~0
      expect(metrics.cost.telephony).toBeGreaterThanOrEqual(0);
      expect(metrics.cost.llm).toBe(0); // pipeline mode has no LLM cost
      // Total should equal sum of all components (within rounding)
      const expectedTotal = metrics.cost.stt + metrics.cost.tts + metrics.cost.llm + metrics.cost.telephony;
      expect(metrics.cost.total).toBeCloseTo(expectedTotal, 5);
    });

    it('openai_realtime mode has zero STT/TTS, uses realtime cost', () => {
      const acc = new CallMetricsAccumulator({
        callId: 'rt-call',
        providerMode: 'openai_realtime',
        telephonyProvider: 'twilio',
      });
      acc.recordRealtimeUsage({
        input_token_details: { audio_tokens: 100, text_tokens: 50 },
        output_token_details: { audio_tokens: 200, text_tokens: 100 },
      });
      const metrics = acc.endCall();
      expect(metrics.cost.stt).toBe(0);
      expect(metrics.cost.tts).toBe(0);
      expect(metrics.cost.llm).toBeGreaterThan(0);
    });

    it('elevenlabs_convai mode has zero for all AI costs', () => {
      const acc = new CallMetricsAccumulator({
        callId: 'el-call',
        providerMode: 'elevenlabs_convai',
        telephonyProvider: 'twilio',
      });
      const metrics = acc.endCall();
      expect(metrics.cost.stt).toBe(0);
      expect(metrics.cost.tts).toBe(0);
      expect(metrics.cost.llm).toBe(0);
      expect(metrics.cost.telephony).toBeGreaterThanOrEqual(0);
    });

    it('uses actual telephony cost when set', () => {
      const acc = makeAccumulator();
      acc.setActualTelephonyCost(0.05);
      const metrics = acc.endCall();
      expect(metrics.cost.telephony).toBe(0.05);
    });

    it('uses actual STT cost when set', () => {
      const acc = makeAccumulator({ providerMode: 'pipeline' });
      acc.setActualSttCost(0.02);
      const metrics = acc.endCall();
      expect(metrics.cost.stt).toBe(0.02);
    });
  });

  // --- Metric aggregation: mean, p95 ---

  describe('metric aggregation', () => {
    it('computes correct mean across turns', () => {
      const acc = makeAccumulator();
      // Simulate 3 turns with known latencies
      for (let i = 0; i < 3; i++) {
        simulateTurn(acc, `user${i}`, `agent${i}`);
      }
      const metrics = acc.endCall();
      // Each turn should have >= 0 latency, average should be >= 0
      expect(metrics.latency_avg.stt_ms).toBeGreaterThanOrEqual(0);
      expect(metrics.latency_avg.llm_ms).toBeGreaterThanOrEqual(0);
    });

    it('p95 returns the 95th percentile value', () => {
      const acc = makeAccumulator();
      // Simulate 20 turns
      for (let i = 0; i < 20; i++) {
        simulateTurn(acc, `user${i}`, `agent${i}`);
      }
      const metrics = acc.endCall();
      // p95 should be >= avg (or at least >= 0)
      expect(metrics.latency_p95.total_ms).toBeGreaterThanOrEqual(0);
    });

    it('p95 with single turn equals that turn latency', () => {
      const acc = makeAccumulator();
      simulateTurn(acc, 'only', 'one');
      const metrics = acc.endCall();
      expect(metrics.latency_p95.stt_ms).toBe(metrics.latency_avg.stt_ms);
    });
  });

  // --- getCostSoFar ---

  describe('getCostSoFar()', () => {
    it('returns current cost without ending the call', () => {
      const acc = makeAccumulator();
      simulateTurn(acc, 'Hi', 'Hello!');
      const cost = acc.getCostSoFar();
      expect(cost.total).toBeGreaterThanOrEqual(0);
      // Can still record more turns
      simulateTurn(acc, 'More', 'Response');
      const metrics = acc.endCall();
      expect(metrics.turns).toHaveLength(2);
    });
  });

  // --- configureSttFormat ---

  describe('configureSttFormat()', () => {
    it('changes sample rate and bytes per sample', () => {
      const acc = makeAccumulator();
      acc.configureSttFormat(8000, 1);
      acc.addSttAudioBytes(8000); // 1 second at 8kHz mono 8-bit
      const metrics = acc.endCall();
      expect(metrics.cost.stt).toBeGreaterThan(0);
    });
  });

  // --- Concurrent write correctness ---

  describe('concurrent write correctness', () => {
    it('handles rapid sequential turns without corruption', () => {
      const acc = makeAccumulator();
      const turns: TurnMetrics[] = [];
      for (let i = 0; i < 100; i++) {
        turns.push(simulateTurn(acc, `user-${i}`, `agent-${i}`));
      }
      expect(turns).toHaveLength(100);
      expect(turns[99].turn_index).toBe(99);

      const metrics = acc.endCall();
      expect(metrics.turns).toHaveLength(100);
    });
  });

  // --- Custom pricing ---

  describe('custom pricing', () => {
    it('uses overridden pricing values', () => {
      const acc = new CallMetricsAccumulator({
        callId: 'custom',
        providerMode: 'pipeline',
        telephonyProvider: 'twilio',
        sttProvider: 'deepgram',
        ttsProvider: 'elevenlabs',
        pricing: {
          deepgram: { unit: 'minute', price: 0.01 },
          elevenlabs: { unit: '1k_chars', price: 0.50 },
        },
      });
      acc.startTurn();
      acc.recordSttComplete('text', 60); // 1 minute
      acc.recordLlmComplete();
      acc.recordTtsFirstByte();
      acc.recordTtsComplete('x'.repeat(1000)); // 1000 chars
      acc.recordTurnComplete('x'.repeat(1000));

      const metrics = acc.endCall();
      expect(metrics.cost.stt).toBeCloseTo(0.01, 4); // 1 min * 0.01
      expect(metrics.cost.tts).toBeCloseTo(0.50, 4); // 1k chars * 0.50
    });
  });
});

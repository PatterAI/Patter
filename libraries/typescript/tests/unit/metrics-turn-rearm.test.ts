import { describe, it, expect } from 'vitest';
import { CallMetricsAccumulator } from '../../src/metrics';

// Regression for the pipeline-mode bug where every user→agent turn after the
// firstMessage emitted no per-turn metrics (and no live SSE transcript).
// `anchorUserSpeechStart()` re-opened a turn (set `_turnStart`) but left the
// `_turnAlreadyClosed` guard set from the previous turn's `recordTurnComplete`,
// so the next `recordTurnComplete` short-circuited to `null`.
describe('[unit] CallMetricsAccumulator — turn re-arm after VAD anchor', () => {
  function makeAcc(): CallMetricsAccumulator {
    return new CallMetricsAccumulator({
      callId: 'rearm-test',
      providerMode: 'pipeline',
      telephonyProvider: 'twilio',
      sttProvider: 'deepgram',
      ttsProvider: 'elevenlabs',
      llmProvider: 'custom',
    });
  }

  it('records a turn opened via anchorUserSpeechStart after a prior turn completed', () => {
    const acc = makeAcc();

    // Turn 0 — opened with startTurn (the firstMessage path). This clears the
    // guard, so it records fine even today.
    acc.startTurn();
    acc.recordSttComplete('hello');
    expect(acc.recordTurnComplete('hi there')).not.toBeNull();

    // Turn 1 — opened via the real pipeline path: a legitimate VAD
    // speech_start anchors the turn (NOT startTurn). startTurnIfIdle is then a
    // no-op because _turnStart is already set. recordTurnComplete must still
    // return a turn — pre-fix it returned null (guard never re-armed).
    acc.anchorUserSpeechStart();
    acc.startTurnIfIdle();
    acc.recordSttComplete('how are you');
    expect(acc.recordTurnComplete('good, thanks')).not.toBeNull();
  });

  it('keeps recording across several anchor-opened turns', () => {
    const acc = makeAcc();
    acc.startTurn();
    acc.recordSttComplete('q0');
    acc.recordTurnComplete('a0');

    for (let i = 1; i <= 3; i++) {
      acc.anchorUserSpeechStart();
      acc.startTurnIfIdle();
      acc.recordSttComplete(`q${i}`);
      expect(acc.recordTurnComplete(`a${i}`)).not.toBeNull();
    }
    // turn 0 + 3 anchor-opened turns all recorded.
    expect(acc.endCall().turns.length).toBe(4);
  });
});

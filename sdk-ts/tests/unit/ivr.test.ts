import { describe, it, expect, vi } from 'vitest';
import {
  IVRActivity,
  TfidfLoopDetector,
  DTMF_EVENTS,
  formatDtmf,
  type DtmfEvent,
} from '../../src/services/ivr';
import type { CallControl } from '../../src/metrics';

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

/**
 * MOCK: no real call. Records sendDtmf invocations so tests can assert
 * the correct digits were forwarded from the tool handler.
 */
function makeCallControl(overrides: Partial<CallControl> = {}): CallControl {
  const sendDtmf = vi.fn(async (_digits: string, _opts?: { delayMs?: number }) => {});
  return {
    callId: 'test-call-id',
    caller: '+15551234567',
    callee: '+15559876543',
    transfer: vi.fn(async () => {}),
    hangup: vi.fn(async () => {}),
    sendDtmf,
    ...overrides,
  } as CallControl;
}

// ---------------------------------------------------------------------------
// DtmfEvent / formatDtmf
// ---------------------------------------------------------------------------

describe('DTMF_EVENTS + formatDtmf', () => {
  it('covers the full keypad', () => {
    const expected = new Set([
      '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
      '*', '#', 'A', 'B', 'C', 'D',
    ]);
    expect(new Set(DTMF_EVENTS)).toEqual(expected);
  });

  it('formatDtmf joins with single spaces', () => {
    expect(formatDtmf(['1', '2', '#'] as DtmfEvent[])).toBe('1 2 #');
  });
});

// ---------------------------------------------------------------------------
// IVRActivity.tools
// ---------------------------------------------------------------------------

describe('IVRActivity.tools', () => {
  it('exposes a send_dtmf_events tool with keypad enum', () => {
    const cc = makeCallControl();
    const ivr = new IVRActivity(cc, { loopDetector: false });

    const tools = ivr.tools;
    expect(tools).toHaveLength(1);

    const tool = tools[0];
    expect(tool.name).toBe('send_dtmf_events');
    expect(tool.description.length).toBeGreaterThan(0);
    expect(tool.parameters.type).toBe('object');
    expect(tool.parameters.required).toEqual(['events']);

    const items = (tool.parameters.properties.events as {
      items: { enum: string[] };
    }).items;
    expect(new Set(items.enum)).toEqual(new Set(DTMF_EVENTS));
  });

  it('forwards digits to CallControl.sendDtmf (MOCK: no real call)', async () => {
    const cc = makeCallControl();
    const ivr = new IVRActivity(cc, { loopDetector: false });

    const result = await ivr.tools[0].handler({ events: ['1', '2', '3', '#'] });

    expect(cc.sendDtmf).toHaveBeenCalledOnce();
    expect(cc.sendDtmf).toHaveBeenCalledWith('123#', { delayMs: 300 });
    expect(result).toContain('Successfully');
  });

  it('rejects invalid digits without hitting sendDtmf', async () => {
    const cc = makeCallControl();
    const ivr = new IVRActivity(cc, { loopDetector: false });

    const result = await ivr.tools[0].handler({ events: ['1', 'Z'] });

    expect(cc.sendDtmf).not.toHaveBeenCalled();
    expect(result.toLowerCase()).toContain('invalid');
  });

  it('reports errors from CallControl.sendDtmf', async () => {
    const cc = makeCallControl({
      sendDtmf: vi.fn(async () => {
        throw new Error('carrier rejected');
      }),
    });
    const ivr = new IVRActivity(cc, { loopDetector: false });

    const result = await ivr.tools[0].handler({ events: ['1'] });

    expect(result).toContain('Failed');
    expect(result).toContain('carrier rejected');
  });

  it('gracefully handles CallControl without sendDtmf implementation', async () => {
    const cc = makeCallControl();
    // Remove the method to simulate an older provider.
    delete (cc as Partial<CallControl>).sendDtmf;
    const ivr = new IVRActivity(cc, { loopDetector: false });

    const result = await ivr.tools[0].handler({ events: ['1'] });
    expect(result).toContain('not implemented');
  });
});

// ---------------------------------------------------------------------------
// TfidfLoopDetector — authentic: real bag-of-words path, synthetic but
// realistic IVR prompts. No mocks.
// ---------------------------------------------------------------------------

describe('TfidfLoopDetector', () => {
  it('fires on 3 consecutive duplicate IVR prompts', () => {
    const detector = new TfidfLoopDetector();
    const prompt = 'Press 1 for sales, 2 for support, or 3 for billing';

    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);

    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);

    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);

    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(true);
  });

  it('resets the consecutive counter on a different prompt', () => {
    const detector = new TfidfLoopDetector();
    const prompt = 'Please enter your account number followed by pound';

    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);
    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);

    // Unrelated chunk breaks the streak.
    detector.addChunk('We are experiencing longer than usual wait times');
    expect(detector.checkLoopDetection()).toBe(false);

    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);
  });

  it('reset() clears buffered history and counter', () => {
    const detector = new TfidfLoopDetector({ consecutiveThreshold: 2 });
    const prompt = 'For new customers press 1';

    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);
    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);
    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(true);

    detector.reset();
    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);
    detector.addChunk(prompt);
    expect(detector.checkLoopDetection()).toBe(false);
  });

  it('validates constructor arguments', () => {
    expect(() => new TfidfLoopDetector({ windowSize: 0 })).toThrow();
    expect(() => new TfidfLoopDetector({ similarityThreshold: 1.5 })).toThrow();
    expect(() => new TfidfLoopDetector({ consecutiveThreshold: 0 })).toThrow();
  });
});

// ---------------------------------------------------------------------------
// IVRActivity transcript → loop callback wiring
// ---------------------------------------------------------------------------

describe('IVRActivity.onUserTranscribed', () => {
  it('invokes onLoopDetected when the detector trips', async () => {
    const cc = makeCallControl();
    const onLoopDetected = vi.fn(async () => {});
    const ivr = new IVRActivity(cc, {
      loopDetector: true,
      onLoopDetected,
    });
    await ivr.start();

    const prompt = 'Press 1 for sales, 2 for support, or 3 for billing';
    for (let i = 0; i < 4; i++) {
      await ivr.onUserTranscribed(prompt);
    }

    expect(onLoopDetected).toHaveBeenCalledOnce();
    await ivr.stop();
  });

  it('ignores transcripts before start()', async () => {
    const cc = makeCallControl();
    const onLoopDetected = vi.fn();
    const ivr = new IVRActivity(cc, { loopDetector: true, onLoopDetected });

    for (let i = 0; i < 5; i++) {
      await ivr.onUserTranscribed('Press 1 for sales');
    }

    expect(onLoopDetected).not.toHaveBeenCalled();
  });

  it('silently accepts empty transcripts', async () => {
    const cc = makeCallControl();
    const ivr = new IVRActivity(cc, { loopDetector: false });
    await ivr.start();
    await expect(ivr.onUserTranscribed('')).resolves.toBeUndefined();
    await ivr.stop();
  });
});

// ---------------------------------------------------------------------------
// Silence debounce — real timers, short delay, NO mocking of setTimeout
// ---------------------------------------------------------------------------

describe('IVRActivity silence debounce', () => {
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

  it('fires onSilence after maxSilenceDuration seconds of combined silence', async () => {
    const cc = makeCallControl();
    let fired = false;
    const ivr = new IVRActivity(cc, {
      maxSilenceDuration: 0.1,
      loopDetector: false,
      onSilence: async () => {
        fired = true;
      },
    });
    await ivr.start();

    ivr.noteUserState('listening');
    ivr.noteAgentState('idle');

    await sleep(200);
    expect(fired).toBe(true);
    await ivr.stop();
  });

  it('cancels the silence timer when the user starts speaking', async () => {
    const cc = makeCallControl();
    const onSilence = vi.fn();
    const ivr = new IVRActivity(cc, {
      maxSilenceDuration: 0.2,
      loopDetector: false,
      onSilence,
    });
    await ivr.start();

    ivr.noteUserState('listening');
    ivr.noteAgentState('idle');

    await sleep(50);
    ivr.noteUserState('speaking');

    await sleep(300);
    expect(onSilence).not.toHaveBeenCalled();
    await ivr.stop();
  });

  it('does not restart the timer on repeated silent-state updates', async () => {
    const cc = makeCallControl();
    const fireTimes: number[] = [];
    const ivr = new IVRActivity(cc, {
      maxSilenceDuration: 0.15,
      loopDetector: false,
      onSilence: async () => {
        fireTimes.push(Date.now());
      },
    });
    await ivr.start();

    const t0 = Date.now();
    ivr.noteUserState('listening');
    ivr.noteAgentState('idle');

    await sleep(80);
    ivr.noteAgentState('idle');

    await sleep(150);
    expect(fireTimes).toHaveLength(1);
    const elapsed = fireTimes[0] - t0;
    expect(elapsed).toBeLessThan(250);

    await ivr.stop();
  });

  it('stop() cancels a pending silence timer', async () => {
    const cc = makeCallControl();
    const onSilence = vi.fn();
    const ivr = new IVRActivity(cc, {
      maxSilenceDuration: 0.15,
      loopDetector: false,
      onSilence,
    });
    await ivr.start();
    ivr.noteUserState('listening');
    ivr.noteAgentState('idle');
    await ivr.stop();

    await sleep(250);
    expect(onSilence).not.toHaveBeenCalled();
  });
});

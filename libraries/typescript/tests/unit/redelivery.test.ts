/**
 * [unit] Opt-in barge-in RE-DELIVERY.
 *
 * When the agent is interrupted mid-answer and the un-heard remainder still
 * matters, ``AgentOptions.redeliverInterrupted`` arms a one-shot system nudge
 * on the next turn so the LLM answers the caller's new message first and then
 * resumes the unfinished idea naturally — instead of silently dropping what
 * the caller never heard.
 *
 * Capture happens in ``cancelSpeaking`` (before the playback cursor is reset);
 * injection happens on the next turn via ``armPendingRedeliveryNudge`` +
 * ``LLMLoop.setRedeliveryNudge`` / ``buildMessages``. Parity with Python
 * tests/unit/test_redelivery.py.
 */

import { describe, it, expect, vi } from 'vitest';
import type { TelephonyBridge, StreamHandlerDeps } from '../../src/stream-handler';
import { StreamHandler } from '../../src/stream-handler';
import { LLMLoop } from '../../src/llm-loop';
import { MetricsStore } from '../../src/dashboard/store';
import { RemoteMessageHandler } from '../../src/remote-message';
import type { WebSocket as WSWebSocket } from 'ws';
import type { AgentOptions } from '../../src/types';
import {
  DEFAULT_MIN_UNSAID_WORDS,
  MinUnsaidWordsPolicy,
  buildRedeliveryNudge,
  type RedeliveryPolicy,
} from '../../src/services/redelivery';

function makeMockBridge(): TelephonyBridge {
  return {
    label: 'TestBridge',
    telephonyProvider: 'twilio',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  } as unknown as TelephonyBridge;
}

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(),
    close: vi.fn(),
    on: vi.fn(),
    once: vi.fn(),
    readyState: 1,
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}

function makeDeps(agent: Partial<AgentOptions>): StreamHandlerDeps {
  return {
    config: { openaiKey: 'test-oai-key' },
    agent: { systemPrompt: 'Test agent', provider: 'pipeline', ...agent },
    bridge: makeMockBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(null),
    sanitizeVariables: vi.fn(() => ({})),
    resolveVariables: vi.fn((tpl: string) => tpl),
  } as unknown as StreamHandlerDeps;
}

// A three-sentence reply, half heard: heard = "Sentence one here. The second
// sentence"; unsaid = "goes here. Third and final sentence." (5 words).
const HALF_HEARD = [
  { text: 'Sentence one here.', startMs: 0, durMs: 2000 },
  { text: 'The second sentence goes here.', startMs: 2000, durMs: 2000 },
  { text: 'Third and final sentence.', startMs: 4000, durMs: 0 },
];

/* eslint-disable @typescript-eslint/no-explicit-any */

interface OaMsg {
  role: string;
  content: string;
}

function makeHandler(agent: Partial<AgentOptions>, withLoop = true): any {
  const h = new StreamHandler(
    makeDeps(agent),
    makeMockWs(),
    '+15551111111',
    '+15552222222',
  ) as any;
  h.llmLoop = withLoop ? new LLMLoop('sk-test', 'gpt-4o-mini', 'SYSTEM PROMPT') : null;
  return h;
}

function setupInterruptedTurn(
  h: any,
  segments: Array<{ text: string; startMs: number; durMs: number }>,
  totalMs: number,
  remainingMs: number,
): void {
  h.isSpeaking = true;
  h.turnSpokenSegments = segments;
  h.turnPlaybackTotalMs = totalMs;
  h.playbackBufferedUntil = Date.now() + remainingMs;
}

function buildMessages(loop: LLMLoop, history: OaMsg[], text: string): OaMsg[] {
  return (
    loop as unknown as {
      buildMessages: (h: Array<{ role: string; text: string }>, t: string) => OaMsg[];
    }
  ).buildMessages(history as unknown as Array<{ role: string; text: string }>, text);
}

function redeliveryMessages(messages: OaMsg[]): OaMsg[] {
  return messages.filter(
    (m) => m.role === 'system' && m.content.includes('You were interrupted while speaking.'),
  );
}

// ---------------------------------------------------------------------------
// Pure policy + nudge builder
// ---------------------------------------------------------------------------

describe('[unit] RedeliveryPolicy + nudge builder', () => {
  it('DEFAULT_MIN_UNSAID_WORDS is a small threshold', () => {
    expect(DEFAULT_MIN_UNSAID_WORDS).toBeGreaterThanOrEqual(2);
  });

  it('MinUnsaidWordsPolicy arms on a substantial remainder', () => {
    const policy = new MinUnsaidWordsPolicy();
    expect(
      policy.shouldRedeliver({ heard: 'a b', unsaid: 'one two three four', heardEverything: false }),
    ).toBe(true);
  });

  it('MinUnsaidWordsPolicy declines a trivial remainder', () => {
    const policy = new MinUnsaidWordsPolicy();
    expect(policy.shouldRedeliver({ heard: 'a b', unsaid: 'one', heardEverything: false })).toBe(
      false,
    );
  });

  it('MinUnsaidWordsPolicy declines when everything was heard', () => {
    const policy = new MinUnsaidWordsPolicy();
    expect(policy.shouldRedeliver({ heard: 'all', unsaid: '', heardEverything: true })).toBe(false);
  });

  it('MinUnsaidWordsPolicy rejects minWords below 1', () => {
    expect(() => new MinUnsaidWordsPolicy({ minWords: 0 })).toThrow();
  });

  it('nudge quotes heard and unsaid', () => {
    const nudge = buildRedeliveryNudge('the heard part', 'the unsaid part');
    expect(nudge).toContain('Recently heard by the user: the heard part.');
    expect(nudge).toContain('Not yet heard by the user: the unsaid part.');
    expect(nudge).toContain("Answer the user's latest message first.");
  });

  it('nudge phrases empty heard as nothing-yet-heard', () => {
    const nudge = buildRedeliveryNudge('', 'the unsaid part');
    expect(nudge).toContain('had not yet heard any of it');
    expect(nudge).not.toContain('Recently heard by the user:');
  });
});

// ---------------------------------------------------------------------------
// LLMLoop one-shot injection
// ---------------------------------------------------------------------------

describe('[unit] LLMLoop re-delivery nudge injection', () => {
  it('(a) default off: no re-delivery system message is ever built', () => {
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'SYSTEM PROMPT');
    const messages = buildMessages(loop, [], 'hi');
    expect(redeliveryMessages(messages)).toHaveLength(0);
  });

  it('(b) armed nudge appears exactly once, then is gone the next build', () => {
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'SYSTEM PROMPT');
    loop.setRedeliveryNudge(buildRedeliveryNudge('heard bit', 'unsaid bit'));

    const first = buildMessages(loop, [], 'new question');
    const nudges = redeliveryMessages(first);
    expect(nudges).toHaveLength(1);
    expect(nudges[0].content).toContain('Recently heard by the user: heard bit.');
    expect(nudges[0].content).toContain('Not yet heard by the user: unsaid bit.');

    const second = buildMessages(loop, [], 'another question');
    expect(redeliveryMessages(second)).toHaveLength(0);
  });

  it('(f) nudge is a separate system message and coexists with a summary', () => {
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'SYSTEM PROMPT');
    loop.setContextSummary('older turns condensed here');
    loop.setRedeliveryNudge(buildRedeliveryNudge('h', 'u u u'));

    const messages = buildMessages(loop, [], 'q');
    const systemMsgs = messages.filter((m) => m.role === 'system');
    expect(systemMsgs).toHaveLength(3); // main + summary + redelivery
    // Not concatenated into the main system prompt (index 0).
    expect(messages[0].content).not.toContain('You were interrupted');
    expect(systemMsgs.filter((m) => m.content.includes('condensed'))).toHaveLength(1);
    expect(redeliveryMessages(messages)).toHaveLength(1);
  });

  it('setRedeliveryNudge("") clears an armed nudge', () => {
    const loop = new LLMLoop('sk-test', 'gpt-4o-mini', 'SYSTEM PROMPT');
    loop.setRedeliveryNudge('something');
    loop.setRedeliveryNudge('');
    expect(redeliveryMessages(buildMessages(loop, [], 'q'))).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Handler capture at barge-in + injection on the next turn
// ---------------------------------------------------------------------------

describe('[unit] handler re-delivery capture', () => {
  it('(a) default off: cancelSpeaking never arms', () => {
    const h = makeHandler({ redeliverInterrupted: false });
    setupInterruptedTurn(h, HALF_HEARD, 6000, 3000);

    h.cancelSpeaking();

    expect(h.pendingRedelivery).toBeNull();
    h.armPendingRedeliveryNudge();
    expect(redeliveryMessages(buildMessages(h.llmLoop, [], 'new question'))).toHaveLength(0);
  });

  it('(b) on + substantial unsaid: armed, injected once, gone after (one-shot)', () => {
    const h = makeHandler({ redeliverInterrupted: true });
    setupInterruptedTurn(h, HALF_HEARD, 6000, 3000);

    h.cancelSpeaking();

    expect(h.pendingRedelivery).not.toBeNull();
    expect(h.pendingRedelivery.heard).toBe('Sentence one here. The second sentence');
    expect(h.pendingRedelivery.unsaid).toBe('goes here. Third and final sentence.');

    h.armPendingRedeliveryNudge();
    expect(h.pendingRedelivery).toBeNull(); // disarmed

    const first = buildMessages(h.llmLoop, [], 'what about billing?');
    const nudges = redeliveryMessages(first);
    expect(nudges).toHaveLength(1);
    expect(nudges[0].content).toContain(
      'Not yet heard by the user: goes here. Third and final sentence.',
    );

    // One-shot: nothing re-armed → no nudge on the following turn.
    h.armPendingRedeliveryNudge();
    expect(redeliveryMessages(buildMessages(h.llmLoop, [], 'and shipping?'))).toHaveLength(0);
  });

  it('(c) on but everything heard: no arm', () => {
    const h = makeHandler({ redeliverInterrupted: true });
    setupInterruptedTurn(h, HALF_HEARD, 6000, 0); // remaining 0 → all heard

    h.cancelSpeaking();

    expect(h.pendingRedelivery).toBeNull();
  });

  it('(d) on but trivial unsaid below threshold: no arm', () => {
    const h = makeHandler({ redeliverInterrupted: true });
    const segments = [
      { text: 'Alpha bravo charlie delta echo foxtrot', startMs: 0, durMs: 5000 },
      { text: 'golf hotel', startMs: 5000, durMs: 1000 },
    ];
    setupInterruptedTurn(h, segments, 6000, 400); // heard ~5.6 s → tail word(s) only

    const split = h.heardResponseSplit();
    expect(split.heardEverything).toBe(false);
    const unheardWords = split.unsaid.trim().split(/\s+/).filter(Boolean).length;
    expect(unheardWords).toBeGreaterThan(0);
    expect(unheardWords).toBeLessThan(DEFAULT_MIN_UNSAID_WORDS);

    h.cancelSpeaking();

    expect(h.pendingRedelivery).toBeNull();
  });

  it('(e) a custom policy overrides the default gate', () => {
    const alwaysRedeliver: RedeliveryPolicy = {
      shouldRedeliver: ({ unsaid }) => unsaid.length > 0,
    };
    const h = makeHandler({ redeliverInterrupted: true, redeliveryPolicy: alwaysRedeliver });
    const segments = [
      { text: 'Alpha bravo charlie delta echo foxtrot', startMs: 0, durMs: 5000 },
      { text: 'golf hotel', startMs: 5000, durMs: 1000 },
    ];
    setupInterruptedTurn(h, segments, 6000, 400); // trivial tail the default skips

    h.cancelSpeaking();
    expect(h.pendingRedelivery).not.toBeNull();

    // And a never-arm policy suppresses even a substantial remainder.
    const neverRedeliver: RedeliveryPolicy = { shouldRedeliver: () => false };
    const h2 = makeHandler({ redeliverInterrupted: true, redeliveryPolicy: neverRedeliver });
    setupInterruptedTurn(h2, HALF_HEARD, 6000, 3000);
    h2.cancelSpeaking();
    expect(h2.pendingRedelivery).toBeNull();
  });

  it('armPendingRedeliveryNudge with no loop is safe and still disarms', () => {
    const h = makeHandler({ redeliverInterrupted: true }, false);
    setupInterruptedTurn(h, HALF_HEARD, 6000, 3000);
    h.cancelSpeaking();
    expect(h.pendingRedelivery).not.toBeNull();
    h.armPendingRedeliveryNudge();
    expect(h.pendingRedelivery).toBeNull();
  });
});

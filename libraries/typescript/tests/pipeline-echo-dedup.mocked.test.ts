/**
 * [mocked] Echo guard + back-to-back dedup for the pipeline turn-taking path —
 * parity with Python test_pipeline_echo_dedup.py. Stops the agent's own TTS
 * echo (forwarded to STT during TTS without AEC) from firing a phantom barge-in
 * or becoming a user turn, and keeps a genuinely different fast follow-up from
 * being swallowed by the back-to-back filter.
 */
import { describe, it, expect, vi } from 'vitest';
import {
  StreamHandler,
  looksLikeEcho,
  normalizeForEcho,
  isNearDuplicate,
} from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { AgentOptions } from '../src/types';
import type { WebSocket as WSWebSocket } from 'ws';

function makeMockWs(): WSWebSocket {
  return {
    send: vi.fn(), close: vi.fn(), on: vi.fn(), once: vi.fn(), readyState: 1,
    removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as WSWebSocket;
}
function makeBridge(): TelephonyBridge {
  return {
    label: 'Twilio', telephonyProvider: 'twilio',
    sendAudio: vi.fn(), sendMark: vi.fn(), sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  } as unknown as TelephonyBridge;
}
function makeDeps(): StreamHandlerDeps {
  const agent: AgentOptions = {
    systemPrompt: 'test', provider: 'pipeline', model: 'gpt-4o-mini', voice: 'alloy',
  };
  return {
    config: {}, agent, bridge: makeBridge(), metricsStore: new MetricsStore(),
    pricing: null, remoteHandler: new RemoteMessageHandler(), recording: false,
    buildAIAdapter: vi.fn().mockReturnValue(null),
    sanitizeVariables: vi.fn((r: Record<string, unknown>) => r),
    resolveVariables: vi.fn((t: string) => t),
  } as unknown as StreamHandlerDeps;
}
interface CommitHandle {
  forwardSttWhileSpeaking: boolean;
  isSpeaking: boolean;
  currentAgentSpokenText: string;
  lastCommitText: string;
  lastCommitAt: number;
  commitTranscript(text: string): boolean;
}
function makeHandler(): CommitHandle {
  return new StreamHandler(makeDeps(), makeMockWs(), '+1', '+2') as unknown as CommitHandle;
}

describe('[mocked] echo + dedup helpers', () => {
  it('normalizeForEcho strips punctuation and case', () => {
    expect(normalizeForEcho('Ciao, come VA?!')).toBe('ciao come va');
  });
  it('looksLikeEcho: long substring fragment is echo', () => {
    expect(
      looksLikeEcho('ti racconto una storia molto', 'Certo, ti racconto una storia molto lunga'),
    ).toBe(true);
  });
  it('looksLikeEcho: long high-word-overlap fragment is echo', () => {
    expect(
      looksLikeEcho('che tu lo voglia detto', "che tu lo voglia o no, te l'ho già detto"),
    ).toBe(true);
  });
  it('looksLikeEcho: short answer repeating the agent is NOT echo', () => {
    const agent = "preferisci lunedì o martedì per l'appuntamento";
    expect(looksLikeEcho('lunedì', agent)).toBe(false);
    expect(looksLikeEcho('monday at two', agent)).toBe(false);
    expect(looksLikeEcho('sì va bene', agent)).toBe(false);
  });
  it('looksLikeEcho: unrelated user speech is not echo', () => {
    expect(looksLikeEcho('fermati dimmi solo interrotto', 'Sto bene grazie sono pronto ad aiutarti')).toBe(false);
  });
  it('looksLikeEcho: empty inputs are not echo', () => {
    expect(looksLikeEcho('', 'qualcosa')).toBe(false);
    expect(looksLikeEcho('qualcosa', '')).toBe(false);
  });
  it('isNearDuplicate: exact and substring', () => {
    expect(isNearDuplicate('ciao come va', 'ciao come va')).toBe(true);
    expect(isNearDuplicate('ciao come', 'ciao come va')).toBe(true);
    expect(isNearDuplicate('fermati subito', 'dimmi una storia')).toBe(false);
  });
});

describe('[mocked] commitTranscript echo + dedup', () => {
  it('drops echo while speaking with the forward flag', () => {
    const h = makeHandler();
    h.forwardSttWhileSpeaking = true;
    h.isSpeaking = true;
    h.currentAgentSpokenText = 'ti racconto una storia lunga sul mare';
    expect(h.commitTranscript('ti racconto una storia lunga')).toBe(false);
  });
  it('does NOT drop echo when the flag is off (default)', () => {
    const h = makeHandler();
    h.forwardSttWhileSpeaking = false;
    h.isSpeaking = true;
    h.currentAgentSpokenText = 'ti racconto una storia lunga sul mare';
    expect(h.commitTranscript('ti racconto una storia lunga')).toBe(true);
  });
  it('does NOT drop when idle (post-turn user reply)', () => {
    const h = makeHandler();
    h.forwardSttWhileSpeaking = true;
    h.isSpeaking = false;
    h.currentAgentSpokenText = 'ti racconto una storia lunga sul mare';
    expect(h.commitTranscript('ti racconto una storia lunga')).toBe(true);
  });
  it('does NOT drop a short answer repeating the agent (false-positive guard)', () => {
    const h = makeHandler();
    h.forwardSttWhileSpeaking = true;
    h.isSpeaking = true;
    h.currentAgentSpokenText = "preferisci lunedì o martedì per l'appuntamento";
    expect(h.commitTranscript('lunedì')).toBe(true);
  });
  it('keeps a different follow-up within 500ms (empty-[interrupted]-turn fix)', () => {
    const h = makeHandler();
    h.lastCommitText = 'dimmi una storia';
    h.lastCommitAt = Date.now();
    expect(h.commitTranscript('fermati dimmi solo interrotto')).toBe(true);
  });
  it('drops a near-duplicate within 500ms (Deepgram double-final)', () => {
    const h = makeHandler();
    h.lastCommitText = 'fermati dimmi solo';
    h.lastCommitAt = Date.now();
    expect(h.commitTranscript('fermati dimmi solo interrotto')).toBe(false);
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GeminiLiveAdapter, GEMINI_LIVE_3_1_FLASH_PREVIEW } from '../src/providers/gemini-live';

// [mocked] GeminiLiveAdapter — async-mode tool-call fixes for the
// gemini-3.1-flash-live family. In the default BLOCKING function-call mode 3.1
// halts audio at the toolCall and never resumes after the response (silent
// stall until the caller hangs up). The fix opts flash-live into the async
// mode: `behavior: NON_BLOCKING` on the declaration + `scheduling: INTERRUPT`
// on the response. Every other model stays byte-identical on the wire.
//
// Mocks the @google/genai boundary only — the config assembly and the
// tool-response shaping under test are the real adapter code.

const NATIVE_AUDIO_2_5 = 'gemini-2.5-flash-native-audio-preview-09-2025';

interface LiveCbs {
  onopen?: () => void;
  onmessage?: (e: unknown) => void;
}

interface ConnectCapture {
  configs: Array<Record<string, unknown>>;
}

function makeFakeSession() {
  return {
    sendRealtimeInput: vi.fn(),
    sendClientContent: vi.fn(),
    sendToolResponse: vi.fn(),
    close: vi.fn(),
  };
}

/** Mock of @google/genai — mirrors the real handshake (open, then setupComplete). */
function makeGenAIMock(session: unknown, capture: ConnectCapture) {
  return {
    GoogleGenAI: vi.fn().mockImplementation(() => ({
      live: {
        connect: vi.fn(async (args: { config?: Record<string, unknown>; callbacks?: LiveCbs }) => {
          if (args.config) capture.configs.push(args.config);
          args.callbacks?.onopen?.();
          args.callbacks?.onmessage?.({ setupComplete: {} });
          return session;
        }),
      },
    })),
  };
}

const TOOLS = [
  { name: 'qualifica_lead', description: 'Qualify the inbound lead.', parameters: { type: 'object', properties: {} } },
];

type FnDecl = { behavior?: string };
type ToolCfg = { functionDeclarations: FnDecl[] };

describe('[mocked] GeminiLiveAdapter — async tool-call mode (3.1 flash-live wedge fix)', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('opts flash-live function declarations into NON_BLOCKING behavior', async () => {
    const capture: ConnectCapture = { configs: [] };
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture));

    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
      tools: TOOLS,
    });
    await adapter.connect();

    const cfg = capture.configs[0] as { tools?: ToolCfg[] };
    expect(cfg.tools?.[0].functionDeclarations[0].behavior).toBe('NON_BLOCKING');
    await adapter.close();
  });

  it('leaves non-flash-live declarations byte-identical (no behavior key)', async () => {
    const capture: ConnectCapture = { configs: [] };
    vi.doMock('@google/genai', () => makeGenAIMock(makeFakeSession(), capture));

    const adapter = new GeminiLiveAdapter('test-key', { model: NATIVE_AUDIO_2_5, tools: TOOLS });
    await adapter.connect();

    const cfg = capture.configs[0] as { tools?: ToolCfg[] };
    expect(cfg.tools?.[0].functionDeclarations[0]).not.toHaveProperty('behavior');
    await adapter.close();
  });

  it('sends INTERRUPT scheduling on the flash-live tool response', async () => {
    const session = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(session, { configs: [] }));

    const adapter = new GeminiLiveAdapter('test-key', {
      model: GEMINI_LIVE_3_1_FLASH_PREVIEW,
      tools: TOOLS,
    });
    await adapter.connect();
    await adapter.sendFunctionResult('call-1', '{"ok":true}');

    expect(session.sendToolResponse).toHaveBeenCalledTimes(1);
    const arg = session.sendToolResponse.mock.calls[0][0] as {
      functionResponses: Array<Record<string, unknown>>;
    };
    const fr = arg.functionResponses[0];
    expect(fr.id).toBe('call-1');
    expect(fr.response).toEqual({ result: '{"ok":true}' });
    expect(fr.scheduling).toBe('INTERRUPT');
    await adapter.close();
  });

  it('omits scheduling on non-flash-live tool responses', async () => {
    const session = makeFakeSession();
    vi.doMock('@google/genai', () => makeGenAIMock(session, { configs: [] }));

    const adapter = new GeminiLiveAdapter('test-key', { model: NATIVE_AUDIO_2_5, tools: TOOLS });
    await adapter.connect();
    await adapter.sendFunctionResult('call-2', 'result');

    const arg = session.sendToolResponse.mock.calls[0][0] as {
      functionResponses: Array<Record<string, unknown>>;
    };
    expect(arg.functionResponses[0]).not.toHaveProperty('scheduling');
    await adapter.close();
  });
});

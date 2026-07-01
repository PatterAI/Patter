/**
 * [unit] transfer_call DESTINATION POLICY (allowlist) — TypeScript parity
 * with `libraries/python/tests/test_transfer_allowlist.py`.
 *
 * `transfer_call`'s `number` argument is chosen by the LLM, which is driven
 * by caller speech — a prompt-injected caller can steer the agent into
 * dialing an arbitrary (premium-rate / international) E.164 number billed to
 * the operator. The destination policy is the deterministic
 * defense-in-depth control: an exact-number allowlist and/or a prefix
 * allowlist enforced at every transfer guard site (pipeline built-in
 * handler, OpenAI Realtime function_call path, ElevenLabs ConvAI
 * client-tool path) BEFORE any carrier REST call. No policy configured
 * (default) keeps today's format-only E.164 gate byte-identical.
 */
import { describe, it, expect, vi } from 'vitest';
import {
  StreamHandler,
  augmentWithBuiltinHandoffTools,
  isTransferDestinationAllowed,
} from '../src/stream-handler';
import type { TelephonyBridge, StreamHandlerDeps } from '../src/stream-handler';
import { Patter, Twilio, OpenAIRealtime } from '../src/index';
import { MetricsStore } from '../src/dashboard/store';
import { RemoteMessageHandler } from '../src/remote-message';
import type { WebSocket as WSWebSocket } from 'ws';
import type { AgentOptions, ToolDefinition } from '../src/types';

const POLICY_REJECTION = {
  error: 'Transfer destination not allowed by policy',
  status: 'rejected',
};
const ALLOWED = '+15551230000';
const DENIED = '+19005551234'; // premium-rate-looking number NOT in any policy below

// ---------------------------------------------------------------------------
// Harness (mirroring skills.test.ts)
// ---------------------------------------------------------------------------

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

function makeBridge(): TelephonyBridge {
  return {
    label: 'Twilio',
    telephonyProvider: 'twilio',
    inputWireFormat: 'ulaw_8000',
    sendAudio: vi.fn(),
    sendMark: vi.fn(),
    sendClear: vi.fn(),
    transferCall: vi.fn().mockResolvedValue(undefined),
    endCall: vi.fn().mockResolvedValue(undefined),
    createStt: vi.fn().mockReturnValue(null),
    queryTelephonyCost: vi.fn().mockResolvedValue(undefined),
  } as unknown as TelephonyBridge;
}

function makeDeps(agent: AgentOptions, overrides?: Partial<StreamHandlerDeps>): StreamHandlerDeps {
  return {
    config: { openaiKey: 'sk-test' },
    agent,
    bridge: makeBridge(),
    metricsStore: new MetricsStore(),
    pricing: null,
    remoteHandler: new RemoteMessageHandler(),
    recording: false,
    buildAIAdapter: vi.fn(),
    sanitizeVariables: vi.fn((raw: Record<string, unknown>) => {
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(raw)) safe[k] = String(v);
      return safe;
    }),
    resolveVariables: vi.fn((tpl: string) => tpl),
    ...overrides,
  } as unknown as StreamHandlerDeps;
}

function makeHandler(agent: AgentOptions): StreamHandler {
  return new StreamHandler(makeDeps(agent), makeMockWs(), '+15551234567', '+15559876543');
}

function makeFakeAdapter(): { sendFunctionResult: ReturnType<typeof vi.fn> } {
  return { sendFunctionResult: vi.fn().mockResolvedValue(undefined) };
}

function makePhone(): Patter {
  return new Patter({
    carrier: new Twilio({ accountSid: 'AC' + '0'.repeat(32), authToken: 'token' }),
    phoneNumber: '+15550000000',
    webhookUrl: 'example.ngrok.io',
  });
}

const ENGINE = () => new OpenAIRealtime({ apiKey: 'sk-test' });

// ---------------------------------------------------------------------------
// isTransferDestinationAllowed — pure policy semantics
// ---------------------------------------------------------------------------

describe('[unit] isTransferDestinationAllowed', () => {
  it('allows any destination when no policy is configured', () => {
    expect(isTransferDestinationAllowed(DENIED, undefined, undefined)).toBe(true);
  });

  it('enforces the exact-number allowlist', () => {
    expect(isTransferDestinationAllowed(ALLOWED, [ALLOWED], undefined)).toBe(true);
    expect(isTransferDestinationAllowed('+15551230001', [ALLOWED], undefined)).toBe(false);
  });

  it('enforces the prefix allowlist', () => {
    const prefixes = ['+1415', '+44'];
    expect(isTransferDestinationAllowed('+14155551234', undefined, prefixes)).toBe(true);
    expect(isTransferDestinationAllowed('+442071234567', undefined, prefixes)).toBe(true);
    expect(isTransferDestinationAllowed(DENIED, undefined, prefixes)).toBe(false);
  });

  it('applies union semantics — number OR prefix passes', () => {
    expect(isTransferDestinationAllowed(ALLOWED, [ALLOWED], ['+44'])).toBe(true);
    expect(isTransferDestinationAllowed('+442071234567', [ALLOWED], ['+44'])).toBe(true);
    expect(isTransferDestinationAllowed(DENIED, [ALLOWED], ['+44'])).toBe(false);
  });

  it('denies all destinations on a configured-but-empty policy', () => {
    expect(isTransferDestinationAllowed(ALLOWED, [], undefined)).toBe(false);
    expect(isTransferDestinationAllowed(ALLOWED, undefined, [])).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// phone.agent() factory — validation + passthrough
// ---------------------------------------------------------------------------

describe('[unit] phone.agent() transfer policy options', () => {
  it('defaults to undefined (backward compat)', () => {
    const agent = makePhone().agent({ systemPrompt: 'hi', engine: ENGINE() });
    expect(agent.transferAllowedNumbers).toBeUndefined();
    expect(agent.transferAllowedPrefixes).toBeUndefined();
  });

  it('passes valid allowlists through', () => {
    const agent = makePhone().agent({
      systemPrompt: 'hi',
      engine: ENGINE(),
      transferAllowedNumbers: [ALLOWED],
      transferAllowedPrefixes: ['+1'],
    });
    expect(agent.transferAllowedNumbers).toEqual([ALLOWED]);
    expect(agent.transferAllowedPrefixes).toEqual(['+1']);
  });

  it('rejects a non-E.164 allowlist number', () => {
    expect(() =>
      makePhone().agent({
        systemPrompt: 'hi',
        engine: ENGINE(),
        transferAllowedNumbers: ['555-1234'],
      }),
    ).toThrow(/transferAllowedNumbers/);
  });

  it('rejects a malformed prefix', () => {
    expect(() =>
      makePhone().agent({
        systemPrompt: 'hi',
        engine: ENGINE(),
        transferAllowedPrefixes: ['1415'], // missing leading '+'
      }),
    ).toThrow(/transferAllowedPrefixes/);
    expect(() =>
      makePhone().agent({
        systemPrompt: 'hi',
        engine: ENGINE(),
        transferAllowedPrefixes: ['+'], // no digits
      }),
    ).toThrow(/transferAllowedPrefixes/);
  });
});

// ---------------------------------------------------------------------------
// Pipeline mode — built-in transfer handler enforces the policy
// ---------------------------------------------------------------------------

describe('[unit] pipeline built-in transfer handler policy', () => {
  function makeTools(
    transferCall: (number: string, options?: unknown) => Promise<void>,
    policy?: { transferAllowedNumbers?: readonly string[]; transferAllowedPrefixes?: readonly string[] },
  ): ToolDefinition[] {
    return augmentWithBuiltinHandoffTools(null, { transferCall }, policy);
  }

  it('rejects a denied destination before the carrier call', async () => {
    const transferCall = vi.fn().mockResolvedValue(undefined);
    const tools = makeTools(transferCall, { transferAllowedNumbers: [ALLOWED] });
    const result = JSON.parse(
      await tools[0].handler!({ number: DENIED, mode: 'cold' }, {} as never),
    );
    expect(result).toEqual(POLICY_REJECTION);
    expect(transferCall).not.toHaveBeenCalled();
  });

  it('transfers an allowed destination', async () => {
    const transferCall = vi.fn().mockResolvedValue(undefined);
    const tools = makeTools(transferCall, { transferAllowedNumbers: [ALLOWED] });
    const result = JSON.parse(
      await tools[0].handler!({ number: ALLOWED, mode: 'cold' }, {} as never),
    );
    expect(result).toEqual({ status: 'transferring', to: ALLOWED });
    expect(transferCall).toHaveBeenCalledWith(ALLOWED);
  });

  it('gates warm mode too', async () => {
    const transferCall = vi.fn().mockResolvedValue(undefined);
    const tools = makeTools(transferCall, { transferAllowedPrefixes: ['+1415'] });
    const result = JSON.parse(
      await tools[0].handler!({ number: DENIED, mode: 'warm', summary: 's' }, {} as never),
    );
    expect(result).toEqual(POLICY_REJECTION);
    expect(transferCall).not.toHaveBeenCalled();
  });

  it('keeps existing behaviour when no policy is configured', async () => {
    const transferCall = vi.fn().mockResolvedValue(undefined);
    const tools = makeTools(transferCall);
    const result = JSON.parse(
      await tools[0].handler!({ number: DENIED, mode: 'cold' }, {} as never),
    );
    expect(result).toEqual({ status: 'transferring', to: DENIED });
    expect(transferCall).toHaveBeenCalledWith(DENIED);
  });
});

// ---------------------------------------------------------------------------
// OpenAI Realtime mode — function_call guard site enforces the policy
// ---------------------------------------------------------------------------

describe('[unit] realtime function_call transfer policy', () => {
  it('rejects a denied destination and never touches the bridge', async () => {
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      transferAllowedNumbers: [ALLOWED],
    };
    const handler = makeHandler(agent);
    const adapter = makeFakeAdapter();
    (handler as unknown as { adapter: unknown }).adapter = adapter;
    const bridge = (handler as unknown as { deps: StreamHandlerDeps }).deps.bridge;

    await (
      handler as unknown as {
        handleFunctionCall(fc: { call_id: string; name: string; arguments: string }): Promise<void>;
      }
    ).handleFunctionCall({
      call_id: 'fc-1',
      name: 'transfer_call',
      arguments: JSON.stringify({ number: DENIED }),
    });

    expect(bridge.transferCall).not.toHaveBeenCalled();
    expect(adapter.sendFunctionResult).toHaveBeenCalledTimes(1);
    const [callId, payload] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    expect(callId).toBe('fc-1');
    expect(JSON.parse(payload)).toEqual(POLICY_REJECTION);
  });

  it('transfers an allowed destination (prefix match)', async () => {
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      transferAllowedPrefixes: ['+1555'],
    };
    const handler = makeHandler(agent);
    const adapter = makeFakeAdapter();
    (handler as unknown as { adapter: unknown }).adapter = adapter;
    const bridge = (handler as unknown as { deps: StreamHandlerDeps }).deps.bridge;

    await (
      handler as unknown as {
        handleFunctionCall(fc: { call_id: string; name: string; arguments: string }): Promise<void>;
      }
    ).handleFunctionCall({
      call_id: 'fc-2',
      name: 'transfer_call',
      arguments: JSON.stringify({ number: ALLOWED }),
    });

    expect(bridge.transferCall).toHaveBeenCalledWith(expect.anything(), ALLOWED);
    const [, payload] = adapter.sendFunctionResult.mock.calls[0] as [string, string];
    expect(JSON.parse(payload)).toEqual({ status: 'transferring', to: ALLOWED });
  });
});

// ---------------------------------------------------------------------------
// ElevenLabs ConvAI mode — client-tool guard site enforces the policy
// ---------------------------------------------------------------------------

describe('[unit] ConvAI client-tool transfer policy', () => {
  it('rejects a denied destination with an error client_tool_result', async () => {
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      transferAllowedNumbers: [ALLOWED],
    };
    const handler = makeHandler(agent);
    const sendClientToolResult = vi.fn();
    (handler as unknown as { adapter: unknown }).adapter = { sendClientToolResult };
    const bridge = (handler as unknown as { deps: StreamHandlerDeps }).deps.bridge;

    await (
      handler as unknown as {
        handleConvAIClientTool(fc: {
          call_id: string;
          name: string;
          arguments: Record<string, unknown>;
        }): Promise<void>;
      }
    ).handleConvAIClientTool({
      call_id: 'ct-1',
      name: 'transfer_call',
      arguments: { number: DENIED },
    });

    expect(bridge.transferCall).not.toHaveBeenCalled();
    expect(sendClientToolResult).toHaveBeenCalledTimes(1);
    const [callId, payload, isError] = sendClientToolResult.mock.calls[0] as [
      string,
      string,
      boolean,
    ];
    expect(callId).toBe('ct-1');
    expect(JSON.parse(payload)).toEqual(POLICY_REJECTION);
    expect(isError).toBe(true);
  });

  it('transfers an allowed destination', async () => {
    const agent: AgentOptions = {
      systemPrompt: 'hi',
      transferAllowedNumbers: [ALLOWED],
    };
    const handler = makeHandler(agent);
    const sendClientToolResult = vi.fn();
    (handler as unknown as { adapter: unknown }).adapter = { sendClientToolResult };
    const bridge = (handler as unknown as { deps: StreamHandlerDeps }).deps.bridge;

    await (
      handler as unknown as {
        handleConvAIClientTool(fc: {
          call_id: string;
          name: string;
          arguments: Record<string, unknown>;
        }): Promise<void>;
      }
    ).handleConvAIClientTool({
      call_id: 'ct-2',
      name: 'transfer_call',
      arguments: { number: ALLOWED },
    });

    expect(bridge.transferCall).toHaveBeenCalledWith(expect.anything(), ALLOWED);
    const [, payload] = sendClientToolResult.mock.calls[0] as [string, string];
    expect(payload).toBe(`Transferring to ${ALLOWED}`);
  });
});

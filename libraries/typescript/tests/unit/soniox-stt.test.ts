/**
 * Unit tests for Soniox v5 real-time STT adoption (TypeScript).
 *
 * Mocks the `ws` module so the test can assert the model string and the new
 * opt-in v5 endpoint controls land in the initial Soniox config frame sent
 * over the WebSocket — without any network I/O. Mirrors the Python
 * `test_soniox_stt_v5.py` coverage.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { SonioxSTT, SonioxModel } from '../../src/providers/soniox-stt';
import type { SonioxSTTOptions } from '../../src/providers/soniox-stt';

// ---------------------------------------------------------------------------
// Mock the ws module
// ---------------------------------------------------------------------------

const mockWsInstances: Array<{
  handlers: Map<string, Array<(...args: unknown[]) => void>>;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  readyState: number;
  once: (event: string, cb: (...args: unknown[]) => void) => void;
  emit: (event: string, ...args: unknown[]) => void;
}> = [];

vi.mock('ws', () => {
  const OPEN = 1;
  const CLOSED = 3;

  class MockWebSocket {
    static OPEN = OPEN;
    static CLOSED = CLOSED;
    readyState = OPEN;
    handlers = new Map<string, Array<(...args: unknown[]) => void>>();
    send = vi.fn();
    close = vi.fn();

    constructor(_url: string, _opts?: unknown) {
      mockWsInstances.push(this as unknown as (typeof mockWsInstances)[number]);
    }

    on(event: string, cb: (...args: unknown[]) => void) {
      if (!this.handlers.has(event)) {
        this.handlers.set(event, []);
      }
      this.handlers.get(event)!.push(cb);
    }

    once(event: string, cb: (...args: unknown[]) => void) {
      this.on(event, cb);
    }

    emit(event: string, ...args: unknown[]) {
      const cbs = this.handlers.get(event) ?? [];
      for (const cb of cbs) {
        cb(...args);
      }
    }
  }

  return { default: MockWebSocket, __esModule: true };
});

function latestWs() {
  return mockWsInstances[mockWsInstances.length - 1];
}

/** Connect a SonioxSTT against the mock ws and return the parsed config frame. */
async function connectAndCapture(
  apiKey: string,
  options: SonioxSTTOptions = {},
): Promise<Record<string, unknown>> {
  const stt = new SonioxSTT(apiKey, options);
  const connectPromise = stt.connect();
  const ws = latestWs();
  ws.emit('open');
  await connectPromise;
  // First send after open is the JSON config frame.
  const firstSend = ws.send.mock.calls[0]?.[0] as string;
  const config = JSON.parse(firstSend) as Record<string, unknown>;
  stt.close(); // clear the keepalive interval
  return config;
}

describe('SonioxSTT v5', () => {
  beforeEach(() => {
    mockWsInstances.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('exposes the v5 model id and keeps older models reachable', () => {
    expect(SonioxModel.STT_RT_V5).toBe('stt-rt-v5');
    expect(SonioxModel.STT_RT_V4).toBe('stt-rt-v4');
    expect(SonioxModel.STT_RT_V3).toBe('stt-rt-v3');
    expect(SonioxModel.STT_RT_V2).toBe('stt-rt-v2');
  });

  it('sends the v5 model in the config by default', async () => {
    const config = await connectAndCapture('sx_test');
    expect(config.model).toBe('stt-rt-v5');
  });

  it('forTwilio() defaults to the v5 model', () => {
    const stt = SonioxSTT.forTwilio('sx_test');
    expect(stt).toBeInstanceOf(SonioxSTT);
  });

  it('honors an explicit older model', async () => {
    const config = await connectAndCapture('sx_test', { model: SonioxModel.STT_RT_V4 });
    expect(config.model).toBe('stt-rt-v4');
  });

  it('omits the v5 endpoint controls unless set, then forwards them', async () => {
    const base = await connectAndCapture('sx_test');
    expect(base.endpoint_sensitivity).toBeUndefined();
    expect(base.endpoint_latency_adjustment_level).toBeUndefined();

    const tuned = await connectAndCapture('sx_test', {
      endpointSensitivity: 0.5,
      endpointLatencyAdjustmentLevel: 2,
    });
    expect(tuned.endpoint_sensitivity).toBe(0.5);
    expect(tuned.endpoint_latency_adjustment_level).toBe(2);
  });

  it('validates the v5 endpoint controls', () => {
    expect(() => new SonioxSTT('sx_test', { endpointSensitivity: 1.5 })).toThrow(
      /endpointSensitivity/,
    );
    expect(
      () => new SonioxSTT('sx_test', { endpointLatencyAdjustmentLevel: 4 }),
    ).toThrow(/endpointLatencyAdjustmentLevel/);
  });
});

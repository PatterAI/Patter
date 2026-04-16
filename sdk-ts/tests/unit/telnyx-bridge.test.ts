/**
 * Tests for TelnyxBridge — DTMF send, transfer validation (E.164 + SIP URI),
 * recording start/stop.
 *
 * MOCK: global `fetch` is mocked so no real Telnyx API call is made.
 * For real API verification, run the integration suite with TELNYX_API_KEY.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TelnyxBridge } from '../../src/server';
import type { LocalConfig } from '../../src/server';

function makeConfig(): LocalConfig {
  return {
    telephonyProvider: 'telnyx',
    telnyxKey: 'KEY-test',
    telnyxConnectionId: 'conn-abc',
    phoneNumber: '+15551234567',
    webhookUrl: 'example.ngrok.io',
  };
}

describe('TelnyxBridge.sendDtmf', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => '', status: 200 });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('sends one POST per digit with send_dtmf action', async () => {
    const bridge = new TelnyxBridge(makeConfig());
    await bridge.sendDtmf('1234', '12#4', 0); // delayMs=0 for fast test

    expect(fetchMock).toHaveBeenCalledTimes(4);

    const firstCall = fetchMock.mock.calls[0];
    expect(firstCall[0]).toContain('/actions/send_dtmf');
    const body = JSON.parse((firstCall[1] as { body: string }).body);
    expect(body.digits).toBe('1');
    expect(body.duration_millis).toBeGreaterThanOrEqual(100);
    expect(body.duration_millis).toBeLessThanOrEqual(500);
  });

  it('filters invalid characters before sending', async () => {
    const bridge = new TelnyxBridge(makeConfig());
    await bridge.sendDtmf('call-id', 'ab!!XY1', 0); // only a, b, 1 survive (XY out)
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const digits = fetchMock.mock.calls.map(
      (c) => JSON.parse((c[1] as { body: string }).body).digits,
    );
    expect(digits).toEqual(['a', 'b', '1']);
  });

  it('no-ops when digits string is empty', async () => {
    const bridge = new TelnyxBridge(makeConfig());
    await bridge.sendDtmf('call-id', '', 0);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('no-ops when telnyxKey is missing', async () => {
    const bridge = new TelnyxBridge({ ...makeConfig(), telnyxKey: undefined });
    await bridge.sendDtmf('call-id', '123', 0);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('delays between digits (observed via setTimeout calls)', async () => {
    vi.useFakeTimers();
    const bridge = new TelnyxBridge(makeConfig());
    const setTimeoutSpy = vi.spyOn(global, 'setTimeout');

    const promise = bridge.sendDtmf('call-id', '12', 500);
    // Advance through fake timers in between digits.
    await vi.advanceTimersByTimeAsync(500);
    await promise;

    // At least one setTimeout call for the delay between 2 digits.
    expect(setTimeoutSpy).toHaveBeenCalled();
    vi.useRealTimers();
  });
});

describe('TelnyxBridge.transferCall', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('accepts E.164 numbers', async () => {
    const bridge = new TelnyxBridge(makeConfig());
    await bridge.transferCall('call-id', '+15551234567');
    expect(fetchMock).toHaveBeenCalled();
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain('/actions/transfer');
  });

  it('accepts SIP URIs', async () => {
    const bridge = new TelnyxBridge(makeConfig());
    await bridge.transferCall('call-id', 'sip:agent@example.com');
    expect(fetchMock).toHaveBeenCalled();
  });

  it('rejects malformed targets', async () => {
    const bridge = new TelnyxBridge(makeConfig());
    await bridge.transferCall('call-id', 'not a phone number');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('TelnyxBridge.startRecording / stopRecording', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => '', status: 200 });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('posts to /actions/record_start with mp3 format', async () => {
    const bridge = new TelnyxBridge(makeConfig());
    await bridge.startRecording('call-id');
    expect(fetchMock).toHaveBeenCalled();
    const body = JSON.parse((fetchMock.mock.calls[0][1] as { body: string }).body);
    expect(body.format).toBe('mp3');
    expect(body.channels).toBe('single');
  });

  it('posts to /actions/record_stop with empty body', async () => {
    const bridge = new TelnyxBridge(makeConfig());
    await bridge.stopRecording('call-id');
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain('/actions/record_stop');
  });
});

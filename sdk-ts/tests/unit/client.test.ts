import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Patter } from '../../src/client';
import { Twilio, Telnyx } from '../../src/index';
import { ProvisionError } from '../../src/errors';

// ---------------------------------------------------------------------------
// Mock external dependencies at the module boundary
// ---------------------------------------------------------------------------

vi.mock('../../src/server', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../../src/server')>();
  class MockEmbeddedServer {
    voicemailMessage = '';
    start = vi.fn().mockResolvedValue(undefined);
    stop = vi.fn().mockResolvedValue(undefined);
    constructor(..._args: unknown[]) {}
  }
  return {
    ...orig,
    EmbeddedServer: MockEmbeddedServer,
  };
});

// We need to mock the dynamic import of test-mode
vi.mock('../../src/test-mode', () => ({
  TestSession: class MockTestSession {
    async run() { return undefined; }
  },
}));

function makeTwilioCarrier() {
  return new Twilio({ accountSid: 'AC123', authToken: 'tok' });
}

function makeTelnyxCarrier() {
  return new Telnyx({ apiKey: 'KEY_123', connectionId: 'conn-1' });
}

describe('Patter (cloud rejection)', () => {
  it('throws a clear error when constructed with apiKey (cloud mode unavailable)', () => {
    expect(
      () => new Patter({ apiKey: 'pt_xxx' } as never),
    ).toThrow(/Patter Cloud is not yet available/);
  });

  it('rejection message hints at local mode', () => {
    expect(
      () => new Patter({ apiKey: 'pt_xxx' } as never),
    ).toThrow(/`carrier:` and `phoneNumber:`/);
  });
});

describe('Patter (local mode)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('constructs in local mode with a Twilio carrier', () => {
    const client = new Patter({
      carrier: makeTwilioCarrier(),
      phoneNumber: '+15551234567',
      webhookUrl: 'example.com/wh',
    });
    expect(client).toBeDefined();
  });

  it('throws if phoneNumber missing in local mode', () => {
    expect(
      () =>
        new Patter({
          carrier: makeTwilioCarrier(),
          phoneNumber: '',
          webhookUrl: 'example.com/wh',
        }),
    ).toThrow('Local mode requires phoneNumber');
  });

  it('accepts missing webhookUrl in constructor (deferred to serve)', () => {
    const phone = new Patter({
      carrier: makeTwilioCarrier(),
      phoneNumber: '+15551234567',
    });
    expect(phone).toBeDefined();
  });

  it('throws if carrier missing', () => {
    expect(
      () =>
        new Patter({
          phoneNumber: '+15551234567',
          webhookUrl: 'example.com/wh',
        } as never),
    ).toThrow(/carrier/);
  });

  // --- agent() ---

  describe('agent()', () => {
    const client = new Patter({
      carrier: makeTwilioCarrier(),
      phoneNumber: '+15551234567',
      webhookUrl: 'example.com/wh',
    });

    it('returns a copy of agent options', () => {
      const opts = { systemPrompt: 'Hi', provider: 'pipeline' as const };
      const result = client.agent(opts);
      expect(result.systemPrompt).toBe('Hi');
      expect(result).not.toBe(opts); // immutable copy
    });

    it('throws for invalid provider', () => {
      expect(() =>
        client.agent({ systemPrompt: 'Hi', provider: 'invalid' as never }),
      ).toThrow('provider must be one of');
    });

    it('throws for invalid tools (not an array)', () => {
      expect(() =>
        client.agent({ systemPrompt: 'Hi', tools: 'bad' as never }),
      ).toThrow('tools must be an array');
    });

    it('throws for tool missing name', () => {
      expect(() =>
        client.agent({
          systemPrompt: 'Hi',
          tools: [{ name: '', description: 'x', parameters: {} } as never],
        }),
      ).toThrow("tools[0] missing required 'name' field");
    });

    it('throws for tool missing both webhookUrl and handler', () => {
      expect(() =>
        client.agent({
          systemPrompt: 'Hi',
          tools: [{ name: 'test', description: 'x', parameters: {} } as never],
        }),
      ).toThrow("tools[0] requires either 'webhookUrl' or 'handler'");
    });

    it('throws for non-object variables', () => {
      expect(() =>
        client.agent({
          systemPrompt: 'Hi',
          variables: 'bad' as never,
        }),
      ).toThrow('variables must be an object');
    });

    it('throws for array variables', () => {
      expect(() =>
        client.agent({
          systemPrompt: 'Hi',
          variables: [] as never,
        }),
      ).toThrow('variables must be an object');
    });
  });

  // --- serve() ---

  describe('serve()', () => {
    it('throws if agent is missing', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      await expect(
        client.serve({ agent: null as never }),
      ).rejects.toThrow('agent is required');
    });

    it('throws if systemPrompt missing (non-pipeline)', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      await expect(
        client.serve({
          agent: { systemPrompt: '', provider: 'openai_realtime' },
        }),
      ).rejects.toThrow('agent.systemPrompt is required');
    });

    it('throws for invalid port', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      await expect(
        client.serve({
          agent: { systemPrompt: 'Hi' },
          port: 0,
        }),
      ).rejects.toThrow(RangeError);
      await expect(
        client.serve({
          agent: { systemPrompt: 'Hi' },
          port: 70000,
        }),
      ).rejects.toThrow(RangeError);
    });

    it('starts the embedded server', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      await client.serve({ agent: { systemPrompt: 'Hello' } });
      // No throw means success — EmbeddedServer is mocked
    });
  });

  // --- test() ---

  describe('test()', () => {
    it('runs a test session in local mode', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      await expect(
        client.test({ agent: { systemPrompt: 'Hi' } }),
      ).resolves.toBeUndefined();
    });
  });

  // --- call() local mode ---

  describe('call() in local mode', () => {
    it('throws if "to" is missing', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      await expect(
        client.call({ to: '', agent: { systemPrompt: 'Hi' } }),
      ).rejects.toThrow("'to' phone number is required");
    });

    it('throws if "to" is not E.164', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      await expect(
        client.call({ to: '5551234567', agent: { systemPrompt: 'Hi' } }),
      ).rejects.toThrow("'to' must be in E.164 format");
    });

    it('makes a Twilio outbound call via fetch with inline TwiML', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({}),
        text: async () => '',
      } as Response);

      await client.call({ to: '+15559999999', agent: { systemPrompt: 'Hi' } });
      expect(fetchSpy).toHaveBeenCalledOnce();
      expect(fetchSpy.mock.calls[0][0]).toContain('api.twilio.com');
      // Body must use inline ``Twiml`` (parity with the Python adapter) and
      // must NOT use ``Url`` — that would force Twilio to make a webhook
      // round-trip back to us, adding 100-200ms of dial latency.
      const init = fetchSpy.mock.calls[0][1] as { body?: string };
      const body = init?.body ?? '';
      const params = new URLSearchParams(body);
      expect(params.get('Url')).toBeNull();
      const twiml = params.get('Twiml');
      expect(twiml).not.toBeNull();
      expect(twiml).toContain('<Connect>');
      expect(twiml).toContain('<Stream url="wss://example.com/wh/ws/stream/outbound"');
    });

    it('throws ProvisionError on Twilio call failure', async () => {
      const client = new Patter({
        carrier: makeTwilioCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      fetchSpy.mockResolvedValueOnce({
        ok: false,
        status: 500,
        json: async () => ({}),
        text: async () => 'Call failed',
      } as Response);

      await expect(
        client.call({ to: '+15559999999', agent: { systemPrompt: 'Hi' } }),
      ).rejects.toThrow(ProvisionError);
    });

    it('makes a Telnyx outbound call via fetch', async () => {
      const client = new Patter({
        carrier: makeTelnyxCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({}),
        text: async () => '',
      } as Response);

      await client.call({ to: '+15559999999', agent: { systemPrompt: 'Hi' } });
      expect(fetchSpy).toHaveBeenCalledOnce();
      expect(fetchSpy.mock.calls[0][0]).toContain('api.telnyx.com');
    });

    it('throws ProvisionError on Telnyx call failure', async () => {
      const client = new Patter({
        carrier: makeTelnyxCarrier(),
        phoneNumber: '+15551234567',
        webhookUrl: 'example.com/wh',
      });
      fetchSpy.mockResolvedValueOnce({
        ok: false,
        status: 500,
        json: async () => ({}),
        text: async () => 'Error',
      } as Response);

      await expect(
        client.call({ to: '+15559999999', agent: { systemPrompt: 'Hi' } }),
      ).rejects.toThrow(ProvisionError);
    });
  });
});

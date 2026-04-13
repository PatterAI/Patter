import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Patter } from '../../src/client';
import { PatterConnectionError, ProvisionError } from '../../src/errors';

// ---------------------------------------------------------------------------
// Mock external dependencies at the module boundary
// ---------------------------------------------------------------------------

vi.mock('../../src/connection', () => {
  class PatterConnection {
    isConnected = false;
    connect = vi.fn().mockResolvedValue(undefined);
    disconnect = vi.fn().mockResolvedValue(undefined);
    requestCall = vi.fn().mockResolvedValue(undefined);
    constructor(_apiKey: string, _backendUrl: string) {}
  }
  return { PatterConnection };
});

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
  TestSession: vi.fn().mockImplementation(() => ({
    run: vi.fn().mockResolvedValue(undefined),
  })),
}));

describe('Patter (cloud mode)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('constructs in cloud mode with apiKey', () => {
    const client = new Patter({ apiKey: 'test-key-123' });
    expect(client.apiKey).toBe('test-key-123');
  });

  it('uses custom backendUrl and restUrl', () => {
    const client = new Patter({
      apiKey: 'key',
      backendUrl: 'wss://custom.example.com',
      restUrl: 'https://custom.example.com',
    });
    expect(client.apiKey).toBe('key');
  });

  // --- createAgent ---

  describe('createAgent()', () => {
    it('creates an agent and returns mapped response', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 201,
        ok: true,
        json: async () => ({
          id: 'agent-1',
          name: 'TestBot',
          system_prompt: 'Hello',
          model: 'gpt-4o-mini-realtime-preview',
          voice: 'alloy',
          voice_provider: 'openai',
          language: 'en',
          first_message: null,
          tools: null,
        }),
        text: async () => '',
      } as Response);

      const agent = await client.createAgent({
        name: 'TestBot',
        systemPrompt: 'Hello',
      });

      expect(agent.id).toBe('agent-1');
      expect(agent.name).toBe('TestBot');
      expect(agent.systemPrompt).toBe('Hello');
      expect(agent.voice).toBe('alloy');
    });

    it('throws ProvisionError on non-201 status', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 500,
        ok: false,
        json: async () => ({}),
        text: async () => 'Internal Server Error',
      } as Response);

      await expect(
        client.createAgent({ name: 'Bot', systemPrompt: 'Sys' }),
      ).rejects.toThrow(ProvisionError);
    });
  });

  // --- listAgents ---

  describe('listAgents()', () => {
    it('returns an array of agents', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 200,
        ok: true,
        json: async () => [
          {
            id: 'a1',
            name: 'Agent1',
            system_prompt: 'p',
            model: 'm',
            voice: 'v',
            voice_provider: 'openai',
            language: 'en',
            first_message: null,
            tools: null,
          },
        ],
        text: async () => '',
      } as Response);

      const agents = await client.listAgents();
      expect(agents).toHaveLength(1);
      expect(agents[0].id).toBe('a1');
    });

    it('throws ProvisionError on failure', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 403,
        ok: false,
        json: async () => ({}),
        text: async () => 'Forbidden',
      } as Response);

      await expect(client.listAgents()).rejects.toThrow(ProvisionError);
    });
  });

  // --- buyNumber ---

  describe('buyNumber()', () => {
    it('buys a number and returns mapped response', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 201,
        ok: true,
        json: async () => ({
          id: 'num-1',
          number: '+15551234567',
          provider: 'twilio',
          country: 'US',
          status: 'active',
          agent_id: null,
        }),
        text: async () => '',
      } as Response);

      const num = await client.buyNumber({ country: 'US' });
      expect(num.number).toBe('+15551234567');
      expect(num.provider).toBe('twilio');
    });

    it('throws ProvisionError on failure', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 400,
        ok: false,
        json: async () => ({}),
        text: async () => 'Bad Request',
      } as Response);

      await expect(client.buyNumber()).rejects.toThrow(ProvisionError);
    });
  });

  // --- assignAgent ---

  describe('assignAgent()', () => {
    it('assigns an agent to a number', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 200,
        ok: true,
        json: async () => ({}),
        text: async () => '',
      } as Response);

      await expect(
        client.assignAgent('num-1', 'agent-1'),
      ).resolves.toBeUndefined();
    });

    it('throws ProvisionError on failure', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 500,
        ok: false,
        json: async () => ({}),
        text: async () => 'Error',
      } as Response);

      await expect(
        client.assignAgent('num-1', 'agent-1'),
      ).rejects.toThrow(ProvisionError);
    });
  });

  // --- listCalls ---

  describe('listCalls()', () => {
    it('returns an array of calls', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 200,
        ok: true,
        json: async () => [
          {
            id: 'call-1',
            direction: 'inbound',
            caller: '+15551111111',
            callee: '+15552222222',
            started_at: '2025-01-01T00:00:00Z',
            ended_at: null,
            duration_seconds: null,
            status: 'in-progress',
            transcript: null,
          },
        ],
        text: async () => '',
      } as Response);

      const calls = await client.listCalls(10);
      expect(calls).toHaveLength(1);
      expect(calls[0].caller).toBe('+15551111111');
    });

    it('throws RangeError for invalid limit', async () => {
      const client = new Patter({ apiKey: 'key' });
      await expect(client.listCalls(0)).rejects.toThrow(RangeError);
      await expect(client.listCalls(1001)).rejects.toThrow(RangeError);
      await expect(client.listCalls(1.5)).rejects.toThrow(RangeError);
    });

    it('throws ProvisionError on failure', async () => {
      const client = new Patter({ apiKey: 'key' });
      fetchSpy.mockResolvedValueOnce({
        status: 500,
        ok: false,
        json: async () => ({}),
        text: async () => 'Error',
      } as Response);

      await expect(client.listCalls()).rejects.toThrow(ProvisionError);
    });
  });

  // --- disconnect ---

  describe('disconnect()', () => {
    it('delegates to connection.disconnect', async () => {
      const client = new Patter({ apiKey: 'key' });
      await expect(client.disconnect()).resolves.toBeUndefined();
    });
  });

  // --- static tool() ---

  describe('Patter.tool()', () => {
    it('creates a tool definition with handler', () => {
      const tool = Patter.tool({
        name: 'lookup',
        description: 'Look up data',
        handler: async () => 'result',
      });
      expect(tool.name).toBe('lookup');
      expect(tool.handler).toBeDefined();
    });

    it('creates a tool definition with webhookUrl', () => {
      const tool = Patter.tool({
        name: 'lookup',
        webhookUrl: 'https://example.com/hook',
      });
      expect(tool.webhookUrl).toBe('https://example.com/hook');
    });

    it('throws if neither handler nor webhookUrl provided', () => {
      expect(() => Patter.tool({ name: 'bad' })).toThrow(
        'tool() requires either handler or webhookUrl',
      );
    });
  });

  // --- static guardrail() ---

  describe('Patter.guardrail()', () => {
    it('creates a guardrail with default replacement', () => {
      const g = Patter.guardrail({ name: 'profanity', blockedTerms: ['bad'] });
      expect(g.name).toBe('profanity');
      expect(g.replacement).toBe("I'm sorry, I can't respond to that.");
    });

    it('uses custom replacement', () => {
      const g = Patter.guardrail({ name: 'test', replacement: 'Nope.' });
      expect(g.replacement).toBe('Nope.');
    });
  });

  // --- static provider helpers ---

  describe('static provider helpers', () => {
    it('Patter.deepgram returns STTConfig', () => {
      const cfg = Patter.deepgram({ apiKey: 'dg-key' });
      expect(cfg.provider).toBe('deepgram');
      expect(cfg.apiKey).toBe('dg-key');
    });

    it('Patter.whisper returns STTConfig', () => {
      const cfg = Patter.whisper({ apiKey: 'w-key' });
      expect(cfg.provider).toBe('whisper');
    });

    it('Patter.elevenlabs returns TTSConfig', () => {
      const cfg = Patter.elevenlabs({ apiKey: 'el-key' });
      expect(cfg.provider).toBe('elevenlabs');
    });

    it('Patter.openaiTts returns TTSConfig', () => {
      const cfg = Patter.openaiTts({ apiKey: 'oai-key' });
      expect(cfg.provider).toBe('openai');
    });
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

  it('constructs in local mode with required fields', () => {
    const client = new Patter({
      mode: 'local',
      phoneNumber: '+15551234567',
      webhookUrl: 'https://example.com/wh',
      twilioSid: 'AC123',
      twilioToken: 'tok',
    });
    expect(client.apiKey).toBe('');
  });

  it('throws if phoneNumber missing in local mode', () => {
    expect(
      () =>
        new Patter({
          mode: 'local',
          phoneNumber: '',
          webhookUrl: 'https://example.com/wh',
          twilioSid: 'AC123',
          twilioToken: 'tok',
        }),
    ).toThrow('Local mode requires phoneNumber');
  });

  it('accepts missing webhookUrl in constructor (deferred to serve)', () => {
    const phone = new Patter({
      mode: 'local',
      phoneNumber: '+15551234567',
      webhookUrl: '',
      twilioSid: 'AC123',
      twilioToken: 'tok',
    });
    expect(phone).toBeDefined();
  });

  it('throws if neither twilioSid nor telnyxKey provided', () => {
    expect(
      () =>
        new Patter({
          mode: 'local',
          phoneNumber: '+15551234567',
          webhookUrl: 'https://example.com/wh',
        }),
    ).toThrow('Local mode requires twilioSid or telnyxKey');
  });

  it('throws if twilioSid without twilioToken', () => {
    expect(
      () =>
        new Patter({
          mode: 'local',
          phoneNumber: '+15551234567',
          webhookUrl: 'https://example.com/wh',
          twilioSid: 'AC123',
        }),
    ).toThrow('twilioToken is required when using twilioSid');
  });

  // --- agent() ---

  describe('agent()', () => {
    const client = new Patter({
      mode: 'local',
      phoneNumber: '+15551234567',
      webhookUrl: 'https://example.com/wh',
      twilioSid: 'AC123',
      twilioToken: 'tok',
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
          tools: [{ name: '', description: 'x', parameters: {} }],
        }),
      ).toThrow("tools[0] missing required 'name' field");
    });

    it('throws for tool missing both webhookUrl and handler', () => {
      expect(() =>
        client.agent({
          systemPrompt: 'Hi',
          tools: [{ name: 'test', description: 'x', parameters: {} }],
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
    it('throws if called in cloud mode', async () => {
      const cloud = new Patter({ apiKey: 'key' });
      await expect(
        cloud.serve({
          agent: { systemPrompt: 'Hi' },
        }),
      ).rejects.toThrow('serve() is only available in local mode');
    });

    it('throws if agent is missing', async () => {
      const client = new Patter({
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
      });
      await expect(
        client.serve({ agent: null as never }),
      ).rejects.toThrow('agent is required');
    });

    it('throws if systemPrompt missing (non-pipeline)', async () => {
      const client = new Patter({
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
      });
      await expect(
        client.serve({
          agent: { systemPrompt: '', provider: 'openai_realtime' },
        }),
      ).rejects.toThrow('agent.systemPrompt is required');
    });

    it('throws for invalid port', async () => {
      const client = new Patter({
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
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
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
      });
      await client.serve({ agent: { systemPrompt: 'Hello' } });
      // No throw means success — EmbeddedServer is mocked
    });
  });

  // --- test() ---

  describe('test()', () => {
    it('throws if called in cloud mode', async () => {
      const cloud = new Patter({ apiKey: 'key' });
      await expect(
        cloud.test({ agent: { systemPrompt: 'Hi' } }),
      ).rejects.toThrow('test() is only available in local mode');
    });

    it('runs a test session in local mode', async () => {
      const client = new Patter({
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
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
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
      });
      await expect(
        client.call({ to: '', agent: { systemPrompt: 'Hi' } }),
      ).rejects.toThrow("'to' phone number is required");
    });

    it('throws if "to" is not E.164', async () => {
      const client = new Patter({
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
      });
      await expect(
        client.call({ to: '5551234567', agent: { systemPrompt: 'Hi' } }),
      ).rejects.toThrow("'to' must be in E.164 format");
    });

    it('makes a Twilio outbound call via fetch', async () => {
      const client = new Patter({
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
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
    });

    it('throws ProvisionError on Twilio call failure', async () => {
      const client = new Patter({
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        twilioSid: 'AC123',
        twilioToken: 'tok',
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
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_123',
        telnyxConnectionId: 'conn-1',
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
        mode: 'local',
        phoneNumber: '+15551234567',
        webhookUrl: 'https://example.com/wh',
        telephonyProvider: 'telnyx',
        telnyxKey: 'KEY_123',
        telnyxConnectionId: 'conn-1',
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

  // --- call() cloud mode ---

  describe('call() in cloud mode', () => {
    it('throws PatterConnectionError if not connected and no onMessage', async () => {
      const client = new Patter({ apiKey: 'key' });
      await expect(
        client.call({ to: '+15559999999' }),
      ).rejects.toThrow(PatterConnectionError);
    });
  });
});

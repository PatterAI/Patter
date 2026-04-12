import { describe, it, expect } from "vitest";
import { Patter } from "../src/client";
import { PatterConnectionError } from "../src/errors";

describe("Patter", () => {
  it("stores apiKey", () => {
    const phone = new Patter({ apiKey: "pt_test123" });
    expect(phone.apiKey).toBe("pt_test123");
  });

  it("uses default URLs", () => {
    const phone = new Patter({ apiKey: "pt_test" });
    expect(phone["backendUrl"]).toBe("wss://api.getpatter.com");
    expect(phone["restUrl"]).toBe("https://api.getpatter.com");
  });

  it("accepts custom URLs", () => {
    const phone = new Patter({
      apiKey: "pt_test",
      backendUrl: "wss://custom.com",
      restUrl: "https://custom.com",
    });
    expect(phone["backendUrl"]).toBe("wss://custom.com");
  });

  it("has static provider helpers", () => {
    expect(Patter.deepgram({ apiKey: "dg" }).provider).toBe("deepgram");
    expect(Patter.whisper({ apiKey: "sk" }).provider).toBe("whisper");
    expect(Patter.elevenlabs({ apiKey: "el" }).provider).toBe("elevenlabs");
    expect(Patter.openaiTts({ apiKey: "sk" }).provider).toBe("openai");
  });

  it("call throws when not connected and no handler", async () => {
    const phone = new Patter({ apiKey: "pt_test" });
    await expect(phone.call({ to: "+39123" })).rejects.toThrow(
      PatterConnectionError
    );
  });

  it("has createAgent method", () => {
    const phone = new Patter({ apiKey: "pt_test" });
    expect(typeof phone.createAgent).toBe("function");
  });

  it("has buyNumber method", () => {
    const phone = new Patter({ apiKey: "pt_test" });
    expect(typeof phone.buyNumber).toBe("function");
  });

  it("has assignAgent method", () => {
    const phone = new Patter({ apiKey: "pt_test" });
    expect(typeof phone.assignAgent).toBe("function");
  });

  it("has listCalls method", () => {
    const phone = new Patter({ apiKey: "pt_test" });
    expect(typeof phone.listCalls).toBe("function");
  });
});

describe("Local mode", () => {
  it("detects local mode", () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x.ngrok.dev' });
    expect((phone as unknown as { mode: string }).mode).toBe('local');
  });

  it("agent() returns config", () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x.ngrok.dev' });
    const agent = phone.agent({ systemPrompt: 'Test', voice: 'nova' });
    expect(agent.systemPrompt).toBe('Test');
    expect(agent.voice).toBe('nova');
  });

  it("agent() returns immutable copy", () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x.ngrok.dev' });
    const opts = { systemPrompt: 'Original' };
    const agent = phone.agent(opts);
    expect(agent).not.toBe(opts);
    expect(agent.systemPrompt).toBe('Original');
  });

  it("serve() throws in cloud mode", async () => {
    const phone = new Patter({ apiKey: 'pt_test' });
    const agent = { systemPrompt: 'Test' };
    await expect(phone.serve({ agent })).rejects.toThrow('serve() is only available in local mode');
  });

  it("has agent method", () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x.ngrok.dev' });
    expect(typeof phone.agent).toBe('function');
  });

  it("has serve method", () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x.ngrok.dev' });
    expect(typeof phone.serve).toBe('function');
  });

  it("agent() accepts stt and tts config", () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x.ngrok.dev' });
    const agent = phone.agent({
      provider: 'pipeline',
      systemPrompt: 'test',
      stt: Patter.deepgram({ apiKey: 'dg_test' }),
      tts: Patter.elevenlabs({ apiKey: 'el_test', voice: 'aria' }),
    });
    expect(agent.stt?.provider).toBe('deepgram');
    expect(agent.tts?.provider).toBe('elevenlabs');
    expect(agent.tts?.voice).toBe('aria');
  });

  it("agent() accepts whisper stt and openai tts config", () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x.ngrok.dev' });
    const agent = phone.agent({
      provider: 'pipeline',
      systemPrompt: 'test',
      stt: Patter.whisper({ apiKey: 'sk_test' }),
      tts: Patter.openaiTts({ apiKey: 'sk_test', voice: 'nova' }),
    });
    expect(agent.stt?.provider).toBe('whisper');
    expect(agent.tts?.provider).toBe('openai');
    expect(agent.tts?.voice).toBe('nova');
  });

  it("agent() stt and tts are undefined by default", () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x.ngrok.dev' });
    const agent = phone.agent({ systemPrompt: 'test' });
    expect(agent.stt).toBeUndefined();
    expect(agent.tts).toBeUndefined();
  });
});

describe('Parameter Validation', () => {
  it('agent() rejects invalid provider', () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never);
    expect(() => phone.agent({ systemPrompt: 'test', provider: 'invalid' as never })).toThrow('provider must be one of');
  });

  it('agent() rejects tools without name', () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never);
    expect(() => phone.agent({ systemPrompt: 'test', tools: [{ description: 'x', parameters: {}, webhookUrl: 'x' } as never] })).toThrow("tools[0] missing required 'name'");
  });

  it('agent() rejects tools without webhookUrl or handler', () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never);
    expect(() => phone.agent({ systemPrompt: 'test', tools: [{ name: 'myTool', description: 'x', parameters: {} } as never] })).toThrow("tools[0] requires either 'webhookUrl' or 'handler'");
  });

  it('constructor rejects local mode without phoneNumber', () => {
    expect(() => new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', webhookUrl: 'x' } as never)).toThrow('phoneNumber');
  });

  it('constructor accepts local mode without webhookUrl (deferred to serve)', () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1' } as never);
    expect(phone).toBeDefined();
  });

  it('constructor rejects local mode without twilioSid or telnyxKey', () => {
    expect(() => new Patter({ mode: 'local', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never)).toThrow('twilioSid or telnyxKey');
  });

  it('constructor rejects twilioSid without twilioToken', () => {
    expect(() => new Patter({ mode: 'local', twilioSid: 'AC', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never)).toThrow('twilioToken');
  });

  it('call rejects non-E164 number', async () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never);
    await expect(phone.call({ to: 'not-a-number', agent: { systemPrompt: 'test' } } as never)).rejects.toThrow('E.164');
  });

  it('call rejects missing to field', async () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never);
    await expect(phone.call({ agent: { systemPrompt: 'test' } } as never)).rejects.toThrow("'to' phone number is required");
  });

  it('serve() rejects invalid port', async () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never);
    await expect(phone.serve({ agent: { systemPrompt: 'test' }, port: 99999 })).rejects.toThrow('port must be between 1 and 65535');
  });
});

describe('Patter.tool()', () => {
  it('creates tool with handler', () => {
    const handler = async () => 'result';
    const t = Patter.tool({ name: 'myTool', description: 'Does stuff', handler });
    expect(t.name).toBe('myTool');
    expect(t.description).toBe('Does stuff');
    expect(t.handler).toBe(handler);
    expect(t.webhookUrl).toBeUndefined();
    expect(t.parameters).toEqual({ type: 'object', properties: {} });
  });

  it('creates tool with webhookUrl', () => {
    const t = Patter.tool({ name: 'myTool', webhookUrl: 'https://example.com/hook' });
    expect(t.name).toBe('myTool');
    expect(t.webhookUrl).toBe('https://example.com/hook');
    expect(t.handler).toBeUndefined();
    expect(t.description).toBe('');
  });

  it('creates tool with custom parameters', () => {
    const params = { type: 'object', properties: { query: { type: 'string' } } };
    const t = Patter.tool({ name: 'search', parameters: params, webhookUrl: 'https://x.com' });
    expect(t.parameters).toEqual(params);
  });

  it('throws without handler or webhookUrl', () => {
    expect(() => Patter.tool({ name: 'broken' })).toThrow('tool() requires either handler or webhookUrl');
  });

  it('agent() accepts tools created with Patter.tool()', () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never);
    const agent = phone.agent({
      systemPrompt: 'test',
      tools: [
        Patter.tool({ name: 'lookup', description: 'Look up data', handler: async () => '{}' }),
      ],
    });
    expect(agent.tools).toHaveLength(1);
    expect(agent.tools?.[0].name).toBe('lookup');
  });
});

describe('Guardrails', () => {
  it('Patter.guardrail creates guardrail config', () => {
    const g = Patter.guardrail({ name: 'test', blockedTerms: ['bad'] });
    expect(g.name).toBe('test');
    expect(g.blockedTerms).toContain('bad');
    expect(g.replacement).toBe("I'm sorry, I can't respond to that.");
  });

  it('Patter.guardrail accepts custom replacement', () => {
    const g = Patter.guardrail({ name: 'test', blockedTerms: ['bad'], replacement: 'Custom message.' });
    expect(g.replacement).toBe('Custom message.');
  });

  it('Patter.guardrail accepts check function', () => {
    const g = Patter.guardrail({ name: 'test', check: (text) => text.includes('forbidden') });
    expect(g.check).toBeDefined();
    expect(g.check?.('this is forbidden')).toBe(true);
    expect(g.check?.('this is fine')).toBe(false);
  });

  it('agent() accepts guardrails', () => {
    const phone = new Patter({ mode: 'local', twilioSid: 'AC', twilioToken: 'x', openaiKey: 'sk', phoneNumber: '+1', webhookUrl: 'x' } as never);
    const agent = phone.agent({
      systemPrompt: 'test',
      guardrails: [Patter.guardrail({ name: 'block', blockedTerms: ['bad'] })],
    });
    expect(agent.guardrails).toHaveLength(1);
    expect(agent.guardrails?.[0].name).toBe('block');
  });

  it('guardrail blockedTerms matching is case-insensitive', () => {
    const g = Patter.guardrail({ name: 'test', blockedTerms: ['Diagnosis'] });
    const lowerMatch = g.blockedTerms?.some((term) => 'get a diagnosis today'.toLowerCase().includes(term.toLowerCase()));
    expect(lowerMatch).toBe(true);
  });
});

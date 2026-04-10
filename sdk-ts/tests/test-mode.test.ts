import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { AgentOptions, PipelineMessageHandler } from '../src/types';

// Track inputs queue for mock readline
let inputQueue: string[] = [];
let mockRlClose: ReturnType<typeof vi.fn>;

vi.mock('readline', () => {
  return {
    createInterface: () => {
      mockRlClose = vi.fn();
      return {
        question: (_prompt: string, cb: (answer: string) => void) => {
          const answer = inputQueue.length > 0 ? inputQueue.shift()! : '/quit';
          Promise.resolve().then(() => cb(answer));
        },
        close: mockRlClose,
      };
    },
  };
});

// Suppress console output during tests
beforeEach(() => {
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  inputQueue = [];
  vi.restoreAllMocks();
});

// Import after mock is set up
const { TestSession } = await import('../src/test-mode');

describe('TestSession', () => {
  const baseAgent: AgentOptions = {
    systemPrompt: 'You are a test assistant.',
    model: 'gpt-4o-mini',
    voice: 'alloy',
  };

  it('calls onMessage handler with text and returns response', async () => {
    inputQueue = ['Hello agent'];
    const onMessage: PipelineMessageHandler = vi.fn().mockResolvedValue('Hello back!');

    const session = new TestSession();
    await session.run({
      agent: baseAgent,
      onMessage,
    });

    expect(onMessage).toHaveBeenCalledOnce();
    const callArg = (onMessage as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(callArg.text).toBe('Hello agent');
    expect(callArg.call_id).toBeDefined();
    expect(callArg.caller).toBe('+15550000001');
    expect(callArg.history).toBeDefined();
  });

  it('fires onCallStart and onCallEnd lifecycle hooks', async () => {
    inputQueue = ['/quit'];
    const onCallStart = vi.fn().mockResolvedValue(undefined);
    const onCallEnd = vi.fn().mockResolvedValue(undefined);

    const session = new TestSession();
    await session.run({
      agent: baseAgent,
      onCallStart,
      onCallEnd,
    });

    expect(onCallStart).toHaveBeenCalledOnce();
    const startData = onCallStart.mock.calls[0][0];
    expect(startData.call_id).toBeDefined();
    expect(startData.caller).toBe('+15550000001');
    expect(startData.callee).toBe('+15550000002');
    expect(startData.direction).toBe('test');

    expect(onCallEnd).toHaveBeenCalledOnce();
    const endData = onCallEnd.mock.calls[0][0];
    expect(endData.call_id).toBeDefined();
    expect(endData.transcript).toBeDefined();
  });

  it('sends firstMessage if configured', async () => {
    inputQueue = ['/quit'];
    const agentWithFirst: AgentOptions = {
      ...baseAgent,
      firstMessage: 'Welcome! How can I help you?',
    };

    const onCallEnd = vi.fn().mockResolvedValue(undefined);

    const session = new TestSession();
    await session.run({
      agent: agentWithFirst,
      onCallEnd,
    });

    // firstMessage should appear in the transcript
    const endData = onCallEnd.mock.calls[0][0];
    const transcript = endData.transcript as Array<{ role: string; text: string }>;
    expect(transcript.length).toBeGreaterThanOrEqual(1);
    expect(transcript[0].role).toBe('assistant');
    expect(transcript[0].text).toBe('Welcome! How can I help you?');

    // Also verify console.log was called with the first message
    expect(console.log).toHaveBeenCalledWith(
      expect.stringContaining('Welcome! How can I help you?'),
    );
  });

  it('handles /history command by logging conversation', async () => {
    inputQueue = ['Hello', '/history', '/quit'];
    const onMessage: PipelineMessageHandler = vi.fn().mockResolvedValue('Response.');

    const session = new TestSession();
    await session.run({
      agent: baseAgent,
      onMessage,
    });

    // /history prints conversation entries via console.log
    const logCalls = (console.log as ReturnType<typeof vi.fn>).mock.calls.map(
      (args) => args[0],
    );
    const historyLines = logCalls.filter(
      (line: unknown) =>
        typeof line === 'string' &&
        (line.includes('User:') || line.includes('Assistant:')),
    );
    // After "Hello" -> "Response.", history should have at least user + assistant
    expect(historyLines.length).toBeGreaterThanOrEqual(2);
  });

  it('handles /hangup command to end session', async () => {
    inputQueue = ['/hangup'];
    const onCallEnd = vi.fn().mockResolvedValue(undefined);

    const session = new TestSession();
    await session.run({
      agent: baseAgent,
      onCallEnd,
    });

    expect(onCallEnd).toHaveBeenCalledOnce();
    expect(console.log).toHaveBeenCalledWith(expect.stringContaining('You hung up'));
  });

  it('handles /transfer command to end session', async () => {
    inputQueue = ['/transfer +15559999999'];

    const session = new TestSession();
    await session.run({
      agent: baseAgent,
    });

    expect(console.log).toHaveBeenCalledWith(
      expect.stringContaining('Transfer -> +15559999999'),
    );
  });

  it('skips empty input lines', async () => {
    inputQueue = ['', '', 'Hello', '/quit'];
    const onMessage: PipelineMessageHandler = vi.fn().mockResolvedValue('Hi!');

    const session = new TestSession();
    await session.run({
      agent: baseAgent,
      onMessage,
    });

    // onMessage should only be called once (for "Hello"), empty lines are skipped
    expect(onMessage).toHaveBeenCalledOnce();
  });
});

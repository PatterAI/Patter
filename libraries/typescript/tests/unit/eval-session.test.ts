/**
 * [unit] Real-pipeline eval harness (``src/evals/session.ts``).
 *
 * Mirrors Python ``tests/unit/test_eval_session.py``. These tests drive an
 * ACTUAL pipeline ``StreamHandler`` — the real transcript receive path,
 * ``handleBargeIn`` / ``commitTranscript`` / ``dispatchTurn``, the real
 * ``LLMLoop`` with the real ``DefaultToolExecutor``, guardrails, sentence
 * chunking, and metrics — with only the paid/external boundary faked
 * (telephony bridge, STT/TTS adapters, and a scripted LLM provider).
 * No network anywhere.
 */
import { describe, it, expect } from 'vitest';
import {
  EvalSession,
  ScriptedLLMProvider,
  textTurn,
  toolCallTurn,
} from '../../src/evals';
import { expect as expectTurn } from '../../src/evals';
import { ErrorCode, PatterConfigError, PatterError } from '../../src/errors';
import { tool, guardrail } from '../../src/public-api';
import type { AgentOptions } from '../../src/types';

function makeAgent(overrides: Partial<AgentOptions> = {}): AgentOptions {
  return {
    systemPrompt: 'You are a concise test agent.',
    provider: 'pipeline',
    firstMessage: '',
    ...overrides,
  };
}

async function expectPatterError(
  promise: Promise<unknown>,
  code: (typeof ErrorCode)[keyof typeof ErrorCode],
): Promise<PatterError> {
  try {
    await promise;
  } catch (err) {
    expect(err).toBeInstanceOf(PatterError);
    expect((err as PatterError).code).toBe(code);
    return err as PatterError;
  }
  throw new Error(`expected PatterError(${code}) but nothing was thrown`);
}

// ---------------------------------------------------------------------------
// (a) Tool-call case — the REAL DefaultToolExecutor runs a local handler
// ---------------------------------------------------------------------------

describe('[unit] EvalSession — real pipeline harness', () => {
  it('tool call runs the real tool executor and is assertable', async () => {
    const handlerCalls: Array<
      [Record<string, unknown>, Record<string, unknown>]
    > = [];

    const agent = makeAgent({
      tools: [
        tool({
          name: 'get_weather',
          description: 'Get the weather for a city',
          parameters: {
            type: 'object',
            properties: { city: { type: 'string' } },
            required: ['city'],
          },
          handler: async (args, callContext) => {
            handlerCalls.push([args, callContext]);
            return JSON.stringify({ forecast: 'sunny', city: args.city });
          },
        }),
      ],
    });
    const provider = new ScriptedLLMProvider([
      toolCallTurn('get_weather', { city: 'Paris' }),
      textTurn('It is sunny in Paris today.'),
    ]);

    const s = await EvalSession.create({ agent, llmProvider: provider });
    let result;
    try {
      result = await s.userSays("what's the weather in paris?");
    } finally {
      await s.close();
    }

    // Chainable assertion helpers.
    expectTurn(result)
      .toolCalled('get_weather', { city: 'Paris' })
      .agentTextContains('sunny in Paris');

    // The REAL DefaultToolExecutor invoked the local handler with the real
    // call context (call_id / caller / callee threaded by the handler).
    expect(handlerCalls).toHaveLength(1);
    const [args, callContext] = handlerCalls[0];
    expect(args).toEqual({ city: 'Paris' });
    expect(callContext.call_id).toBe(s.callId);
    expect(callContext.caller).toBe(s.caller);

    // The recorded ToolCallRecord carries the executor's result string.
    expect(result.toolCalls).toHaveLength(1);
    const record = result.toolCalls[0];
    expect(record.name).toBe('get_weather');
    expect(JSON.parse(record.result ?? '')).toEqual({ forecast: 'sunny', city: 'Paris' });

    // The tool result was fed back to the model: the provider's second call
    // contains the assistant tool_calls message + the role='tool' result.
    expect(provider.calls).toHaveLength(2);
    const second = provider.calls[1].messages;
    const toolMsgs = second.filter((m) => m.role === 'tool');
    expect(toolMsgs).toHaveLength(1);
    expect(String(toolMsgs[0].content)).toContain('sunny');

    // The handler's tool-event path also surfaced role='tool' entries into
    // the conversation history (dashboard timeline parity).
    const toolHistory = result.historySnapshot.filter((e) => e.role === 'tool');
    expect(toolHistory.some((e) => e.text.includes('get_weather'))).toBe(true);

    // And the model saw the tool schema (not the handler) via the LLM loop.
    const toolsSent = provider.calls[0].tools;
    expect(toolsSent).not.toBeNull();
    const fn = (toolsSent![0] as { function: Record<string, unknown> }).function;
    expect(fn.name).toBe('get_weather');
    expect('handler' in fn).toBe(false);
  });

  // -------------------------------------------------------------------------
  // (b) Multi-turn — history accumulates through the real handler exactly once
  // -------------------------------------------------------------------------

  it('multi-turn history accumulates without duplicates', async () => {
    const provider = new ScriptedLLMProvider([
      textTurn('First reply.'),
      textTurn('Second reply.'),
    ]);
    const agent = makeAgent();

    const s = await EvalSession.create({ agent, llmProvider: provider });
    let r1, r2;
    try {
      r1 = await s.userSays('tell me a joke');
      r2 = await s.userSays('explain that joke');
    } finally {
      await s.close();
    }

    expect(r1.agentText).toBe('First reply.');
    expect(r2.agentText).toBe('Second reply.');

    // History snapshot after turn 2: both exchanges, in order, exactly once.
    const rolesTexts = r2.historySnapshot.map((e) => [e.role, e.text]);
    expect(rolesTexts).toEqual([
      ['user', 'tell me a joke'],
      ['assistant', 'First reply.'],
      ['user', 'explain that joke'],
      ['assistant', 'Second reply.'],
    ]);

    // What the provider actually received on turn 2 — the history snapshot
    // handed to LLMLoop.run EXCLUDES the current user turn, and
    // buildMessages appends userText exactly once, so every turn (including
    // the current one) reaches the provider exactly once and in order. This
    // is the cross-SDK contract; a regression re-introducing the old
    // trailing duplicate of the current user message must fail here.
    const msgs = provider.calls[1].messages;
    expect(msgs[0].role).toBe('system');
    expect(msgs.slice(1).map((m) => [m.role, m.content])).toEqual([
      ['user', 'tell me a joke'],
      ['assistant', 'First reply.'],
      ['user', 'explain that joke'],
    ]);
    // No entry is ever duplicated.
    expect(msgs.filter((m) => m.content === 'tell me a joke')).toHaveLength(1);
    expect(msgs.filter((m) => m.content === 'First reply.')).toHaveLength(1);
    expect(msgs.filter((m) => m.content === 'explain that joke')).toHaveLength(1);
  });

  it('firstMessage flows through the real start path', async () => {
    const provider = new ScriptedLLMProvider([textTurn('Sure thing.')]);
    const agent = makeAgent({ firstMessage: 'Hello, thanks for calling!' });

    const s = await EvalSession.create({ agent, llmProvider: provider });
    let result;
    try {
      // The REAL start path spoke the greeting through the fake TTS,
      // billed it, and pushed it into history.
      expect(s.tts.spoken).toEqual(['Hello, thanks for calling!']);
      expect(s.history[0].role).toBe('assistant');
      expect(s.history[0].text).toBe('Hello, thanks for calling!');
      expect(s.audioSender.sentAudio.length).toBeGreaterThanOrEqual(1);

      result = await s.userSays('hi, I need help');
    } finally {
      await s.close();
    }

    // The greeting is part of the LLM context on the first user turn.
    const msgs = provider.calls[0].messages;
    expect([msgs[1].role, msgs[1].content]).toEqual([
      'assistant',
      'Hello, thanks for calling!',
    ]);
    expect(result.historySnapshot[0].text).toBe('Hello, thanks for calling!');
  });

  // -------------------------------------------------------------------------
  // (c) Guardrail replacement is observable in agentText
  // -------------------------------------------------------------------------

  it('guardrail replacement is observable in agentText', async () => {
    const provider = new ScriptedLLMProvider([
      textTurn('The secret password is swordfish.'),
    ]);
    const agent = makeAgent({
      guardrails: [
        guardrail({
          name: 'no-secrets',
          blockedTerms: ['swordfish'],
          replacement: 'I cannot share that information.',
        }),
      ],
    });

    const s = await EvalSession.create({ agent, llmProvider: provider });
    let result, spoken;
    try {
      result = await s.userSays('what is the secret password?');
      spoken = [...s.tts.spoken];
    } finally {
      await s.close();
    }

    // agentText reflects what the caller HEARD: the guardrail replacement.
    expectTurn(result).agentTextContains('cannot share that information');
    expect(result.agentText).not.toContain('swordfish');
    // The raw sentence never reached TTS.
    expect(spoken).toEqual(['I cannot share that information.']);
    // Pipeline-streaming history keeps the RAW LLM text (existing handler
    // behaviour, mirrored from the live path) — documented on TurnResult.
    const assistantEntries = result.historySnapshot.filter(
      (e) => e.role === 'assistant',
    );
    expect(assistantEntries[assistantEntries.length - 1].text).toBe(
      'The secret password is swordfish.',
    );
  });

  // -------------------------------------------------------------------------
  // (d) close() runs the handler's real teardown
  // -------------------------------------------------------------------------

  it('close() tears the fakes down through the real handler teardown', async () => {
    const provider = new ScriptedLLMProvider([
      textTurn('Reply one.'),
      textTurn('Reply two.'),
    ]);
    const agent = makeAgent({ firstMessage: 'Hi!' });
    const s = await EvalSession.create({ agent, llmProvider: provider });
    try {
      await s.userSays('first question');
      await s.userSays('second question');
    } finally {
      await s.close();
    }

    // The fakes were closed through the handler's real handleStop().
    expect(s.stt.closed).toBe(true);
    expect(s.tts.closed).toBe(true);
    // close() is idempotent.
    await s.close();
    // A closed session refuses further turns, loudly.
    await expectPatterError(s.userSays('hello again'), ErrorCode.CONFIG);
  });

  it('teardown runs when the body raises (try/finally pattern)', async () => {
    const provider = new ScriptedLLMProvider([textTurn('ok')]);
    const agent = makeAgent();

    const s = await EvalSession.create({ agent, llmProvider: provider });
    let raised = false;
    try {
      throw new Error('boom');
    } catch (err) {
      raised = true;
      expect((err as Error).message).toBe('boom');
    } finally {
      await s.close();
    }
    expect(raised).toBe(true);
    expect(s.stt.closed).toBe(true);
  });

  // -------------------------------------------------------------------------
  // Real commit-path filters apply (dedup / hallucination), loudly
  // -------------------------------------------------------------------------

  it('a duplicate transcript is dropped by the real commit filter', async () => {
    const provider = new ScriptedLLMProvider([textTurn('Once.'), textTurn('Twice.')]);
    const agent = makeAgent();

    const s = await EvalSession.create({ agent, llmProvider: provider });
    try {
      await s.userSays('repeat after me');
      // The REAL ``commitTranscript`` dedup throttle drops an identical
      // final within 2 s — exactly as on a live call. The harness surfaces
      // that as a loud, typed error instead of a silent no-op turn.
      await expectPatterError(s.userSays('repeat after me'), ErrorCode.INPUT_VALIDATION);
    } finally {
      await s.close();
    }
  });

  it('an STT hallucination is dropped by the real commit filter', async () => {
    const provider = new ScriptedLLMProvider([textTurn('unused')]);
    const agent = makeAgent();

    const s = await EvalSession.create({ agent, llmProvider: provider });
    try {
      await expectPatterError(
        s.userSays('thank you for watching'),
        ErrorCode.INPUT_VALIDATION,
      );
    } finally {
      await s.close();
    }
    expect(provider.calls).toEqual([]); // never reached the LLM
  });

  // -------------------------------------------------------------------------
  // Metrics / transcript-event observability
  // -------------------------------------------------------------------------

  it('metrics turn and transcript events are captured', async () => {
    const provider = new ScriptedLLMProvider([textTurn('All sorted.')]);
    const agent = makeAgent();

    const s = await EvalSession.create({ agent, llmProvider: provider });
    let result;
    try {
      result = await s.userSays('sort it out');
    } finally {
      await s.close();
    }

    const turn = result.metricsTurn;
    expect(turn).not.toBeNull();
    expect(turn!.user_text).toBe('sort it out');
    expect(turn!.agent_text).toBe('All sorted.');
    expect(turn!.latency).toBeDefined();
    expect(result.interrupted).toBe(false);

    // onTranscript fired for both roles through the real handler.
    const roles = s.transcriptEvents.map((e) => e.role);
    expect(roles).toContain('user');
    expect(roles).toContain('assistant');
    const userEvents = s.transcriptEvents.filter((e) => e.role === 'user');
    expect(userEvents[0].text).toBe('sort it out');
    expect(userEvents[0].call_id).toBe(s.callId);
  });

  it('hooks run on the real path (afterTranscribe rewrites before the LLM)', async () => {
    // afterTranscribe rewrites the user text before the LLM sees it —
    // proof the real PipelineHookExecutor path is exercised.
    const provider = new ScriptedLLMProvider([textTurn('Noted.')]);
    const agent = makeAgent({
      hooks: {
        afterTranscribe: (text) => text.replace('4111-1111', '[card]'),
      },
    });

    const s = await EvalSession.create({ agent, llmProvider: provider });
    let result;
    try {
      result = await s.userSays('my card is 4111-1111');
    } finally {
      await s.close();
    }

    const msgs = provider.calls[0].messages;
    const userContents = msgs
      .filter((m) => m.role === 'user')
      .map((m) => String(m.content));
    expect(userContents.length).toBeGreaterThan(0);
    expect(userContents.every((c) => c.includes('[card]'))).toBe(true);
    expect(userContents.every((c) => !c.includes('4111-1111'))).toBe(true);
    // History carries the redacted text too (what the LLM actually saw).
    expect(result.historySnapshot[0].text).toBe('my card is [card]');
  });

  // -------------------------------------------------------------------------
  // Configuration errors are loud and typed
  // -------------------------------------------------------------------------

  it('a session without an LLM raises a config error', async () => {
    const agent = makeAgent();
    await expect(EvalSession.create({ agent })).rejects.toBeInstanceOf(PatterConfigError);
  });

  it('userSays before start raises', async () => {
    const agent = makeAgent();
    const session = new EvalSession({ agent, llmProvider: new ScriptedLLMProvider() });
    await expectPatterError(session.userSays('hello'), ErrorCode.CONFIG);
  });
});

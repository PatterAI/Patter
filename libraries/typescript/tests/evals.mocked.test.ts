/**
 * Mocked-boundary tests for the OpenAI judge backend.
 *
 * The ONLY thing mocked here is the OpenAI HTTP endpoint (global `fetch`) — a
 * paid external API we must not hit in CI. Everything inward (request shaping,
 * response parsing, LLMJudge clamp/threshold logic) is the real code.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LLMJudge, OpenAIJudgeBackend } from '../src/index';
import type { EvalCase } from '../src/index';

function mockFetchOnceJson(content: string): ReturnType<typeof vi.fn> {
  const fn = vi.fn(async () => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => ({ choices: [{ message: { content } }] }),
  }));
  vi.stubGlobal('fetch', fn);
  return fn;
}

describe('[mocked] OpenAIJudgeBackend — OpenAI HTTP boundary', () => {
  beforeEach(() => {
    delete process.env.OPENAI_API_KEY;
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('builds the correct chat-completions request and returns the content', async () => {
    const fetchMock = mockFetchOnceJson(
      '{"score": 0.8, "passed": true, "reasoning": "ok"}',
    );
    const backend = new OpenAIJudgeBackend('gpt-4o-mini', 'sk-test-key');

    const raw = await backend.judge('EXPECTED BEHAVIOR: x');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('https://api.openai.com/v1/chat/completions');
    expect(init.method).toBe('POST');
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer sk-test-key');
    expect(headers['Content-Type']).toBe('application/json');
    const body = JSON.parse(init.body as string) as {
      model: string;
      temperature: number;
      response_format: { type: string };
      messages: Array<{ role: string; content: string }>;
    };
    expect(body.model).toBe('gpt-4o-mini');
    expect(body.temperature).toBe(0);
    expect(body.response_format).toEqual({ type: 'json_object' });
    expect(body.messages[0].role).toBe('system');
    expect(body.messages[1]).toEqual({
      role: 'user',
      content: 'EXPECTED BEHAVIOR: x',
    });
    expect(raw).toBe('{"score": 0.8, "passed": true, "reasoning": "ok"}');
  });

  it('reads the api key from OPENAI_API_KEY when none is passed', async () => {
    const fetchMock = mockFetchOnceJson('{"score": 1, "passed": true, "reasoning": ""}');
    process.env.OPENAI_API_KEY = 'sk-env-key';
    const backend = new OpenAIJudgeBackend('gpt-4o-mini');

    await backend.judge('prompt');

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer sk-env-key');
  });

  it('throws a clear error when no api key is available', async () => {
    mockFetchOnceJson('{}');
    const backend = new OpenAIJudgeBackend('gpt-4o-mini');
    await expect(backend.judge('prompt')).rejects.toThrow(/OpenAI API key/);
  });

  it('throws on a non-2xx response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 429, statusText: 'Too Many Requests' })),
    );
    const backend = new OpenAIJudgeBackend('gpt-4o-mini', 'sk-test-key');
    await expect(backend.judge('prompt')).rejects.toThrow(/429/);
  });

  it('LLMJudge end-to-end through the default backend parses a mocked response', async () => {
    mockFetchOnceJson('{"score": 0.95, "passed": true, "reasoning": "excellent"}');
    const judge = new LLMJudge({ apiKey: 'sk-test-key' });
    const evalCase: EvalCase = {
      name: 'c',
      turns: [{ user: 'hi' }],
      expectedBehavior: 'be nice',
      rubric: 'pass if nice',
    };
    const result = await judge.judgeCase(evalCase, [{ role: 'user', text: 'hi' }]);
    expect(result.score).toBeCloseTo(0.95);
    expect(result.passed).toBe(true);
    expect(result.reasoning).toBe('excellent');
  });
});

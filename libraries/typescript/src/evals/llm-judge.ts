/**
 * LLM-as-judge scoring for eval cases. Parity with `getpatter.evals.llm_judge`.
 */

import { getLogger } from '../logger';
import type { EvalCase, JudgeResult, TranscriptEntry } from './case';

/**
 * Backend seam — any object that turns a judge prompt into the model's raw
 * JSON text. Mirrors Python's injectable `backend` so tests can drive every
 * inward code path (prompt building, fence stripping, parsing, clamping,
 * threshold logic) with no network and no module mocks.
 */
export interface JudgeBackend {
  judge(prompt: string): Promise<string>;
}

export const JUDGE_SYSTEM =
  'You are a strict but fair evaluator of voice-AI agents. ' +
  'You will be given: (1) the expected behavior for the agent, (2) a rubric, ' +
  '(3) a transcript of the conversation. ' +
  'Return a JSON object with exactly three keys:\n' +
  '  - "score": float between 0.0 and 1.0\n' +
  '  - "passed": boolean (true when score >= threshold)\n' +
  '  - "reasoning": short string explaining the score\n' +
  'Do not return any text outside the JSON object.';

/**
 * Default backend — OpenAI Chat Completions via `fetch`, mirroring the exact
 * request pattern the pipeline LLM loop uses (`src/llm-loop.ts`). The TS SDK
 * has no `openai` dependency; Python uses the `openai` client — an allowed
 * language-appropriate divergence with identical behaviour.
 */
export class OpenAIJudgeBackend implements JudgeBackend {
  constructor(
    private readonly model: string,
    private readonly apiKey?: string,
  ) {}

  async judge(prompt: string): Promise<string> {
    const key = this.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        'LLMJudge requires an OpenAI API key. Pass apiKey or set OPENAI_API_KEY.',
      );
    }
    const res = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${key}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: this.model,
        messages: [
          { role: 'system', content: JUDGE_SYSTEM },
          { role: 'user', content: prompt },
        ],
        response_format: { type: 'json_object' },
        temperature: 0,
      }),
    });
    if (!res.ok) {
      throw new Error(`OpenAI judge request failed: ${res.status} ${res.statusText}`);
    }
    const data = (await res.json()) as {
      choices?: Array<{ message?: { content?: string } }>;
    };
    return data.choices?.[0]?.message?.content ?? '{}';
  }
}

export interface LLMJudgeOptions {
  readonly model?: string;
  readonly apiKey?: string;
  readonly passThreshold?: number;
  readonly backend?: JudgeBackend;
}

/**
 * Scores conversation transcripts against a rubric via an OpenAI model.
 *
 * The judge is intentionally provider-specific (OpenAI chat completions)
 * because reliability of structured JSON output matters more than provider
 * flexibility for evals. Callers who need a different backend can implement a
 * {@link JudgeBackend} and inject it via the `backend` option.
 */
export class LLMJudge {
  readonly model: string;
  readonly apiKey?: string;
  readonly passThreshold: number;
  private readonly backend: JudgeBackend;

  constructor(opts: LLMJudgeOptions = {}) {
    this.model = opts.model ?? 'gpt-4o-mini';
    this.apiKey = opts.apiKey;
    this.passThreshold = opts.passThreshold ?? 0.7;
    this.backend = opts.backend ?? new OpenAIJudgeBackend(this.model, this.apiKey);
  }

  /** Return a {@link JudgeResult} for the given transcript. */
  async judgeCase(
    evalCase: EvalCase,
    transcript: readonly TranscriptEntry[],
  ): Promise<JudgeResult> {
    const prompt = this.buildPrompt(evalCase, transcript);
    const raw = await this.backend.judge(prompt);
    return this.parse(raw);
  }

  private buildPrompt(
    evalCase: EvalCase,
    transcript: readonly TranscriptEntry[],
  ): string {
    const lines = [
      `EXPECTED BEHAVIOR: ${evalCase.expectedBehavior}`,
      `RUBRIC: ${evalCase.rubric}`,
      `PASS THRESHOLD: ${this.passThreshold}`,
      'TRANSCRIPT:',
    ];
    for (const turn of transcript) {
      lines.push(`  ${turn.role ?? '?'}: ${turn.text ?? ''}`);
    }
    return lines.join('\n');
  }

  private parse(raw: string): JudgeResult {
    let text = raw.trim();
    // Strip a leading fence if the model added one despite json_object mode.
    if (text.startsWith('```')) {
      text = text.replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '');
    }
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(text) as Record<string, unknown>;
    } catch {
      getLogger().warn(
        `LLMJudge: invalid JSON, defaulting to fail: ${JSON.stringify(raw)}`,
      );
      return {
        score: 0,
        passed: false,
        reasoning: `Judge returned invalid JSON: ${raw.slice(0, 200)}`,
      };
    }
    let score = Number((data as { score?: unknown }).score ?? 0);
    if (!Number.isFinite(score)) score = 0;
    score = Math.max(0, Math.min(1, score));
    // Verdict is computed LOCALLY from the score: trusting the judge's
    // self-reported `passed` let a hallucinated `passed: true` with
    // `score: 0.2` record a pass.
    const passed = score >= this.passThreshold;
    const reasoning = String((data as { reasoning?: unknown }).reasoning ?? '');
    return { score, passed, reasoning };
  }
}

/**
 * LLM-as-judge scoring for eval cases.
 *
 * Mirrors the Python `getpatter.evals.llm_judge` module. The judge is
 * intentionally provider-specific (OpenAI chat completions) because
 * reliability of structured JSON output matters more than provider
 * flexibility for evals. Callers who need a different backend can implement
 * a {@link JudgeBackend} and inject it via the ``backend`` option.
 */

import { getLogger } from '../logger';
import type { EvalCase, JudgeResult, TranscriptEntry } from './case';

const JUDGE_SYSTEM =
  'You are a strict but fair evaluator of voice-AI agents. ' +
  'You will be given: (1) the expected behavior for the agent, (2) a rubric, ' +
  '(3) a transcript of the conversation. ' +
  'Return a JSON object with exactly three keys:\n' +
  '  - "score": float between 0.0 and 1.0\n' +
  '  - "passed": boolean (true when score >= threshold)\n' +
  '  - "reasoning": short string explaining the score\n' +
  'Do not return any text outside the JSON object.';

/** Pluggable judge backend — anything exposing ``judge(prompt)``. */
export interface JudgeBackend {
  judge(prompt: string): Promise<string>;
}

/** Options for {@link LLMJudge}. Defaults match the Python SDK byte-for-byte. */
export interface LLMJudgeOptions {
  /** Model the judge should use. Default: ``gpt-4o-mini``. */
  readonly model?: string;
  /** OpenAI API key. Falls back to ``OPENAI_API_KEY`` when unset. */
  readonly apiKey?: string;
  /** Score threshold for a pass. Default: ``0.7``. */
  readonly passThreshold?: number;
  /** Test/alternative backend — any object exposing ``judge(prompt)``. */
  readonly backend?: JudgeBackend;
}

/** Scores conversation transcripts against a rubric via an OpenAI model. */
export class LLMJudge {
  readonly model: string;
  readonly passThreshold: number;
  private readonly apiKey?: string;
  private readonly backend?: JudgeBackend;

  constructor(options: LLMJudgeOptions = {}) {
    this.model = options.model ?? 'gpt-4o-mini';
    this.apiKey = options.apiKey;
    this.passThreshold = options.passThreshold ?? 0.7;
    this.backend = options.backend;
  }

  /** Return a {@link JudgeResult} for the given transcript. */
  async judgeCase(
    evalCase: EvalCase,
    transcript: ReadonlyArray<TranscriptEntry>,
  ): Promise<JudgeResult> {
    const prompt = this.buildPrompt(evalCase, transcript);
    const raw = this.backend
      ? await this.backend.judge(prompt)
      : await this.callOpenAI(prompt);
    return this.parse(raw);
  }

  private buildPrompt(
    evalCase: EvalCase,
    transcript: ReadonlyArray<TranscriptEntry>,
  ): string {
    const lines = [
      `EXPECTED BEHAVIOR: ${evalCase.expectedBehavior}`,
      `RUBRIC: ${evalCase.rubric}`,
      `PASS THRESHOLD: ${this.passThreshold}`,
      'TRANSCRIPT:',
    ];
    for (const turn of transcript) {
      lines.push(`  ${turn.role || '?'}: ${turn.text ?? ''}`);
    }
    return lines.join('\n');
  }

  /** Call OpenAI chat completions directly over fetch (no SDK dependency). */
  private async callOpenAI(prompt: string): Promise<string> {
    const apiKey = this.apiKey || process.env.OPENAI_API_KEY;
    if (!apiKey) {
      throw new Error(
        'LLMJudge requires an OpenAI API key. ' +
          'Set OPENAI_API_KEY or pass apiKey to the LLMJudge constructor.',
      );
    }
    const response = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model: this.model,
        messages: [
          { role: 'system', content: JUDGE_SYSTEM },
          { role: 'user', content: prompt },
        ],
        response_format: { type: 'json_object' },
        temperature: 0.0,
      }),
    });
    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`LLMJudge OpenAI call failed: ${response.status} ${errText.slice(0, 200)}`);
    }
    const data = (await response.json()) as {
      choices?: Array<{ message?: { content?: string | null } }>;
    };
    const content = data.choices?.[0]?.message?.content;
    if (!content) {
      // A 200 without choices (error envelope, empty completion) would
      // otherwise silently parse as a zero-score fail — surface it instead.
      throw new Error(
        `LLMJudge response had no choices/content: ${JSON.stringify(data).slice(0, 200)}`,
      );
    }
    return content;
  }

  /** Parse the judge's JSON — tolerant of extra whitespace / code fences. */
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
      getLogger().warn(`LLMJudge: invalid JSON, defaulting to fail: ${JSON.stringify(raw)}`);
      return {
        score: 0.0,
        passed: false,
        reasoning: `Judge returned invalid JSON: ${raw.slice(0, 200)}`,
      };
    }
    const scoreRaw = Number(data.score ?? 0.0);
    let score = Number.isFinite(scoreRaw) ? scoreRaw : 0.0;
    score = Math.max(0.0, Math.min(1.0, score));
    // Verdict is computed LOCALLY from the score: trusting the judge's
    // self-reported ``passed`` let a hallucinated ``passed: true`` with
    // ``score: 0.2`` record a pass.
    const passed = score >= this.passThreshold;
    const reasoning = String(data.reasoning ?? '');
    return { score, passed, reasoning };
  }
}

/**
 * Deterministic scripted LLM provider for CI evals.
 *
 * The fourth fake-able boundary of {@link EvalSession}: instead of a paid
 * model API, a {@link ScriptedLLMProvider} replays pre-scripted streaming
 * chunks through the REAL {@link LLMLoop} — tool dispatch, history
 * threading, usage accounting, and abort handling all run for real.
 */

import type { LLMChunk, LLMProvider, LLMStreamOptions } from '../llm-loop';

/** Build one scripted assistant turn that streams ``text`` then usage. */
export function textTurn(
  text: string,
  options: { readonly inputTokens?: number; readonly outputTokens?: number } = {},
): LLMChunk[] {
  return [
    { type: 'text', content: text },
    {
      type: 'usage',
      inputTokens: options.inputTokens ?? 8,
      outputTokens: options.outputTokens ?? 8,
    },
  ];
}

/**
 * Build one scripted turn that emits a single complete tool call.
 *
 * The {@link LLMLoop} executes the tool via the real ``DefaultToolExecutor``
 * and re-submits — so a tool scenario needs a follow-up scripted turn
 * (usually {@link textTurn}) for the post-tool-result response.
 */
export function toolCallTurn(
  name: string,
  args?: Record<string, unknown>,
  options: { readonly callId?: string } = {},
): LLMChunk[] {
  return [
    {
      type: 'tool_call',
      index: 0,
      id: options.callId ?? 'call_1',
      name,
      arguments: JSON.stringify(args ?? {}),
    },
    { type: 'usage', inputTokens: 8, outputTokens: 4 },
  ];
}

/** One recorded {@link ScriptedLLMProvider.stream} request. */
export interface ScriptedLLMCall {
  readonly messages: Array<Record<string, unknown>>;
  readonly tools: Array<Record<string, unknown>> | null;
  readonly callId: string | null;
}

/**
 * Deterministic {@link LLMProvider}.
 *
 * Pops one scripted chunk-list per ``stream()`` call (i.e. per LLM-loop
 * iteration — a tool-call turn consumes one script for the call and one for
 * the post-result response). Records every request's ``messages`` and
 * ``tools`` in {@link ScriptedLLMProvider.calls} so tests can assert exactly
 * what the real pipeline sent to the model. Honours the per-turn abort
 * signal between chunks like a well-behaved provider.
 */
export class ScriptedLLMProvider implements LLMProvider {
  /** Stable pricing/dashboard key (no real pricing entry — cost is 0). */
  static readonly providerKey = 'scripted';

  readonly calls: ScriptedLLMCall[] = [];
  private readonly scripts: LLMChunk[][];

  constructor(turns?: ReadonlyArray<ReadonlyArray<LLMChunk>>) {
    this.scripts = (turns ?? []).map((chunks) => chunks.map((c) => ({ ...c })));
  }

  /** Append another scripted turn (chunk list) to the script queue. */
  addTurn(chunks: ReadonlyArray<LLMChunk>): void {
    this.scripts.push(chunks.map((c) => ({ ...c })));
  }

  async *stream(
    messages: Array<Record<string, unknown>>,
    tools?: Array<Record<string, unknown>> | null,
    opts?: LLMStreamOptions,
  ): AsyncGenerator<LLMChunk, void, unknown> {
    this.calls.push({
      messages: messages.map((m) => ({ ...m })),
      tools: tools ? tools.map((t) => ({ ...t })) : null,
      callId: opts?.callId ?? null,
    });
    const script = this.scripts.shift();
    if (script === undefined) {
      yield { type: 'done' };
      return;
    }
    for (const chunk of script) {
      if (opts?.signal?.aborted) return;
      yield { ...chunk };
    }
  }
}

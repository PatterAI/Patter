/**
 * Public API primitives — `Tool` and `Guardrail` classes, plus the
 * `tool()` / `guardrail()` factory functions.
 *
 * These mirror the Python SDK's `patter.Tool` / `patter.Guardrail`. The
 * classes are structurally compatible with the existing `Guardrail`
 * interface and `ToolDefinition` shape used internally, so code that
 * consumed either form keeps working.
 */

import type { ToolDefinition } from "./types";

export interface GuardrailOptions {
  /** Name for logging when triggered. */
  name: string;
  /** List of terms that trigger the guardrail (case-insensitive). */
  blockedTerms?: string[];
  /** Custom check function — return true to block the response. */
  check?: (text: string) => boolean;
  /** Replacement text spoken when guardrail triggers. */
  replacement?: string;
}

const DEFAULT_GUARDRAIL_REPLACEMENT = "I'm sorry, I can't respond to that.";

/**
 * Guardrail definition. Structurally matches the internal `Guardrail`
 * interface so existing code consuming plain objects keeps working.
 *
 * @example
 * ```ts
 * import { Guardrail } from "getpatter";
 * const rail = new Guardrail({ name: "profanity", blockedTerms: ["badword"] });
 * ```
 */
export class Guardrail {
  readonly name: string;
  readonly blockedTerms?: string[];
  readonly check?: (text: string) => boolean;
  readonly replacement: string;

  constructor(opts: GuardrailOptions) {
    if (!opts.name) {
      throw new Error("Guardrail requires a non-empty name.");
    }
    this.name = opts.name;
    if (opts.blockedTerms) this.blockedTerms = opts.blockedTerms;
    if (opts.check) this.check = opts.check;
    this.replacement = opts.replacement ?? DEFAULT_GUARDRAIL_REPLACEMENT;
  }
}

/** Factory helper mirroring Python's `guardrail(...)` function. */
export function guardrail(opts: GuardrailOptions): Guardrail {
  return new Guardrail(opts);
}

export type ToolHandler = (
  args: Record<string, unknown>,
  context: Record<string, unknown>,
) => Promise<string>;

export interface ToolOptions {
  /** Tool name (visible to the LLM). */
  name: string;
  /** What the tool does (visible to the LLM). */
  description?: string;
  /** JSON Schema for tool arguments. */
  parameters?: Record<string, unknown>;
  /** Async function called in-process when the LLM invokes the tool. */
  handler?: ToolHandler;
  /** URL to POST to when the LLM invokes the tool. */
  webhookUrl?: string;
}

/**
 * Tool definition. Structurally matches `ToolDefinition` so it drops
 * directly into `agent({ tools: [...] })`.
 *
 * Exactly one of `handler` or `webhookUrl` must be provided.
 *
 * @example
 * ```ts
 * import { Tool } from "getpatter";
 * const t = new Tool({
 *   name: "check_menu",
 *   description: "Check available menu items",
 *   handler: async () => JSON.stringify({ items: ["margherita"] }),
 * });
 * ```
 */
export class Tool implements ToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly parameters: Record<string, unknown>;
  readonly handler?: ToolHandler;
  readonly webhookUrl?: string;

  constructor(opts: ToolOptions) {
    if (!opts.name) {
      throw new Error("Tool requires a non-empty name.");
    }
    const hasHandler = typeof opts.handler === "function";
    const hasWebhook = typeof opts.webhookUrl === "string" && opts.webhookUrl.length > 0;
    if (!hasHandler && !hasWebhook) {
      throw new Error("Tool requires either handler or webhookUrl.");
    }
    if (hasHandler && hasWebhook) {
      throw new Error("Tool accepts handler OR webhookUrl, not both.");
    }
    this.name = opts.name;
    this.description = opts.description ?? "";
    this.parameters = opts.parameters ?? { type: "object", properties: {} };
    if (hasHandler) this.handler = opts.handler;
    if (hasWebhook) this.webhookUrl = opts.webhookUrl;
  }
}

/** Factory helper mirroring Python's `tool(...)` function. */
export function tool(opts: ToolOptions): Tool {
  return new Tool(opts);
}

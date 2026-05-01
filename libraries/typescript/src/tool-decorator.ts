/**
 * Factory function that builds a {@link ToolDefinition} from a concise
 * parameter spec, auto-generating the full JSON Schema `parameters` object.
 *
 * @example
 * ```ts
 * import { defineTool } from 'getpatter';
 *
 * const getWeather = defineTool({
 *   name: 'get_weather',
 *   description: 'Get the current weather for a location.',
 *   parameters: {
 *     location: { type: 'string', description: 'City name or zip code' },
 *     unit: { type: 'string', description: 'Temperature unit', default: 'celsius' },
 *   },
 *   handler: async (args) => {
 *     return `Sunny, 22°${(args.unit as string)[0].toUpperCase()}`;
 *   },
 * });
 * ```
 */

import type { ToolDefinition } from "./types";

// ── Public types ────────────────────────────────────────────────────────

/** Shorthand property spec accepted by {@link defineTool}. */
export interface ParamSpec {
  readonly type: string;
  readonly description?: string;
  /** When present the parameter is *not* required. */
  readonly default?: unknown;
}

/** Input accepted by {@link defineTool}. */
export interface DefineToolInput {
  readonly name: string;
  readonly description?: string;
  readonly parameters: Readonly<Record<string, ParamSpec>>;
  readonly handler: (
    args: Record<string, unknown>,
    context: Record<string, unknown>,
  ) => Promise<string>;
}

// ── Implementation ──────────────────────────────────────────────────────

/**
 * Build a full {@link ToolDefinition} from a concise parameter spec.
 *
 * Parameters that include a `default` value are treated as optional; all
 * others are added to the JSON Schema `required` array.
 */
export function defineTool(input: DefineToolInput): ToolDefinition {
  const properties: Record<string, Record<string, unknown>> = {};
  const required: string[] = [];

  for (const [paramName, spec] of Object.entries(input.parameters)) {
    const prop: Record<string, unknown> = { type: spec.type };
    if (spec.description !== undefined) {
      prop.description = spec.description;
    }

    properties[paramName] = prop;

    if (spec.default === undefined) {
      required.push(paramName);
    }
  }

  const parameters: Record<string, unknown> = {
    type: "object",
    properties,
  };
  if (required.length > 0) {
    parameters.required = required;
  }

  return {
    name: input.name,
    description: input.description ?? "",
    parameters,
    handler: input.handler,
  };
}

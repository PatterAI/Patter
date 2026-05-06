/**
 * Tool JSON-schema validation for Patter agents.
 *
 * Two layers:
 *  - **Always-on structural sanity**: every tool's ``parameters`` must
 *    look like a valid OpenAI function-tool schema (``type: "object"``,
 *    ``properties`` is an object, ``required`` is an array if present).
 *    Catches typos at build time instead of letting them blow up
 *    mid-call.
 *  - **Strict mode**: when a tool sets ``strict: true``, the schema must
 *    additionally satisfy OpenAI's strict-mode requirements
 *    (``additionalProperties: false`` on every nested object, every
 *    property in ``required``, no truly optional fields). Strict mode
 *    is opt-in — backward-compatible.
 *
 * Both layers run inside ``Patter.agent({...})`` (TS) /
 * ``Patter.agent(...)`` (Py) so user mistakes are surfaced immediately,
 * not on the first inbound call.
 */

import type { ToolDefinition } from '../types';

/** Thrown by ``validateToolSchema`` for a malformed tool schema. */
export class ToolSchemaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ToolSchemaError';
  }
}

interface JsonSchemaObject {
  type?: unknown;
  properties?: unknown;
  required?: unknown;
  additionalProperties?: unknown;
  items?: unknown;
}

/**
 * Validate a tool's ``parameters`` schema. Throws ``ToolSchemaError``
 * with a clear message on the first violation; otherwise returns
 * normally. Idempotent and pure — safe to call from constructors.
 */
export function validateToolSchema(tool: ToolDefinition): void {
  const params = tool.parameters as JsonSchemaObject | null | undefined;
  const tag = `tool '${tool.name}'`;

  if (!params || typeof params !== 'object' || Array.isArray(params)) {
    throw new ToolSchemaError(
      `${tag}: \`parameters\` must be a JSON Schema object (got ${typeof params}).`,
    );
  }
  if (params.type !== 'object') {
    throw new ToolSchemaError(
      `${tag}: \`parameters.type\` must be "object" (got ${JSON.stringify(params.type)}). ` +
        `OpenAI function tools require an object root.`,
    );
  }
  if (
    params.properties !== undefined &&
    (typeof params.properties !== 'object' || params.properties === null || Array.isArray(params.properties))
  ) {
    throw new ToolSchemaError(
      `${tag}: \`parameters.properties\` must be an object map of field → JSON Schema.`,
    );
  }
  if (params.required !== undefined && !Array.isArray(params.required)) {
    throw new ToolSchemaError(
      `${tag}: \`parameters.required\` must be an array of field names.`,
    );
  }
  if (Array.isArray(params.required) && params.properties) {
    const props = params.properties as Record<string, unknown>;
    for (const fieldName of params.required as unknown[]) {
      if (typeof fieldName !== 'string') {
        throw new ToolSchemaError(
          `${tag}: \`parameters.required\` entries must be strings (got ${typeof fieldName}).`,
        );
      }
      if (!(fieldName in props)) {
        throw new ToolSchemaError(
          `${tag}: \`parameters.required\` lists "${fieldName}" but it is not declared in \`parameters.properties\`.`,
        );
      }
    }
  }

  if (tool.strict === true) {
    validateStrictModeSchema(tool.name, params);
  }
}

/**
 * Verify a schema satisfies OpenAI strict mode's structural rules:
 * recursive ``additionalProperties: false`` and ``required`` covering
 * every property at each object level. Called only when the tool opts
 * into ``strict: true`` so we don't penalise lenient callers.
 */
function validateStrictModeSchema(toolName: string, schema: JsonSchemaObject, pathParts: string[] = []): void {
  const tag = `tool '${toolName}'`;
  const here = pathParts.length === 0 ? 'parameters' : `parameters.${pathParts.join('.')}`;

  if (schema.type === 'object') {
    if (schema.additionalProperties !== false) {
      throw new ToolSchemaError(
        `${tag}: strict mode requires \`${here}.additionalProperties: false\` on every object — got ${JSON.stringify(schema.additionalProperties)}.`,
      );
    }
    const props = (schema.properties ?? {}) as Record<string, unknown>;
    const required = Array.isArray(schema.required) ? schema.required : [];
    for (const propName of Object.keys(props)) {
      if (!required.includes(propName)) {
        throw new ToolSchemaError(
          `${tag}: strict mode requires every property to be listed in \`required\` — "${here}.${propName}" is missing. ` +
            `Use a nullable type (e.g. ["string", "null"]) instead of an optional field.`,
        );
      }
    }
    for (const [propName, propSchema] of Object.entries(props)) {
      if (propSchema && typeof propSchema === 'object') {
        validateStrictModeSchema(toolName, propSchema as JsonSchemaObject, [...pathParts, 'properties', propName]);
      }
    }
  } else if (schema.type === 'array' && schema.items && typeof schema.items === 'object') {
    validateStrictModeSchema(toolName, schema.items as JsonSchemaObject, [...pathParts, 'items']);
  }
}

/** Validate a list of tools; rethrows the first ``ToolSchemaError``. */
export function validateAllToolSchemas(tools: readonly ToolDefinition[] | undefined): void {
  if (!tools) return;
  for (const tool of tools) {
    validateToolSchema(tool);
  }
}

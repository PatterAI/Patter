import { describe, it, expect } from 'vitest';
import {
  validateToolSchema,
  validateAllToolSchemas,
  ToolSchemaError,
} from '../src/tools/schema-validation';
import type { ToolDefinition } from '../src/types';

const handler = async (): Promise<string> => '{}';

function tool(overrides: Partial<ToolDefinition>): ToolDefinition {
  return {
    name: overrides.name ?? 'test_tool',
    description: overrides.description ?? 'desc',
    parameters: overrides.parameters ?? { type: 'object', properties: {} },
    handler: overrides.handler ?? handler,
    ...overrides,
  };
}

describe('[unit] validateToolSchema (always-on structural checks)', () => {
  it('accepts a minimal valid object schema', () => {
    expect(() => validateToolSchema(tool({ parameters: { type: 'object' } }))).not.toThrow();
  });

  it('rejects parameters that is not an object', () => {
    expect(() => validateToolSchema(tool({ parameters: 'oops' as unknown as Record<string, unknown> })))
      .toThrow(ToolSchemaError);
  });

  it('rejects parameters with missing or wrong type', () => {
    expect(() => validateToolSchema(tool({ parameters: { type: 'string' } as unknown as Record<string, unknown> })))
      .toThrow(/must be "object"/);
  });

  it('rejects parameters.properties when not an object', () => {
    expect(() => validateToolSchema(tool({
      parameters: { type: 'object', properties: ['not', 'an', 'object'] } as unknown as Record<string, unknown>,
    }))).toThrow(/must be an object map/);
  });

  it('rejects parameters.required when not an array', () => {
    expect(() => validateToolSchema(tool({
      parameters: { type: 'object', properties: {}, required: 'name' } as unknown as Record<string, unknown>,
    }))).toThrow(/must be an array/);
  });

  it('rejects required entries that are not declared in properties', () => {
    expect(() => validateToolSchema(tool({
      parameters: { type: 'object', properties: { foo: { type: 'string' } }, required: ['foo', 'bar'] } as Record<string, unknown>,
    }))).toThrow(/lists "bar"/);
  });
});

describe('[unit] validateToolSchema (strict mode)', () => {
  it('accepts a strict schema that satisfies all rules', () => {
    expect(() => validateToolSchema(tool({
      strict: true,
      parameters: {
        type: 'object',
        properties: { name: { type: 'string' }, age: { type: 'number' } },
        required: ['name', 'age'],
        additionalProperties: false,
      },
    }))).not.toThrow();
  });

  it('rejects strict schema without additionalProperties: false', () => {
    expect(() => validateToolSchema(tool({
      strict: true,
      // additionalProperties intentionally omitted (defaults to true in JSON Schema).
      parameters: {
        type: 'object',
        properties: { name: { type: 'string' } },
        required: ['name'],
      },
    }))).toThrow(/additionalProperties: false/);
  });

  it('rejects strict schema with optional fields (in properties but not required)', () => {
    expect(() => validateToolSchema(tool({
      strict: true,
      parameters: {
        type: 'object',
        properties: { name: { type: 'string' }, age: { type: 'number' } },
        required: ['name'],
        additionalProperties: false,
      },
    }))).toThrow(/strict mode requires every property/);
  });

  it('recurses into nested object properties', () => {
    expect(() => validateToolSchema(tool({
      strict: true,
      parameters: {
        type: 'object',
        properties: {
          address: {
            type: 'object',
            properties: { city: { type: 'string' } },
            required: ['city'],
            // missing additionalProperties: false on the nested object
          },
        },
        required: ['address'],
        additionalProperties: false,
      },
    }))).toThrow(/parameters\.properties\.address\.additionalProperties: false/);
  });

  it('recurses into array items', () => {
    expect(() => validateToolSchema(tool({
      strict: true,
      parameters: {
        type: 'object',
        properties: {
          tags: {
            type: 'array',
            items: {
              type: 'object',
              properties: { key: { type: 'string' } },
              required: ['key'],
              // missing additionalProperties: false on the items schema
            },
          },
        },
        required: ['tags'],
        additionalProperties: false,
      },
    }))).toThrow(/parameters\.properties\.tags\.items/);
  });

  it('skips strict checks when strict is unset (default)', () => {
    expect(() => validateToolSchema(tool({
      // strict not set
      parameters: { type: 'object', properties: { name: { type: 'string' } } },
    }))).not.toThrow();
  });
});

describe('[unit] validateAllToolSchemas', () => {
  it('handles empty / undefined gracefully', () => {
    expect(() => validateAllToolSchemas(undefined)).not.toThrow();
    expect(() => validateAllToolSchemas([])).not.toThrow();
  });

  it('rethrows the first violation', () => {
    expect(() => validateAllToolSchemas([
      tool({ name: 'ok', parameters: { type: 'object' } }),
      tool({ name: 'bad', parameters: 'oops' as unknown as Record<string, unknown> }),
    ])).toThrow(/tool 'bad'/);
  });
});

import { describe, it, expect } from 'vitest';
import { sanitizeGeminiSchema } from '../src/providers/google-llm.js';

// [unit] sanitizeGeminiSchema — REGRESSION (prod 2026-06-18 demo silence):
// the sanitizer recursed into the `properties` map and treated user property
// NAMES (query/to/subject) as schema keywords, stripping them — so a tool's
// `properties` became {} while `required` still listed the names. Gemini Live
// rejected the setup (WS close 1007: "required[0]: property is not defined")
// -> no setupComplete -> no audio. Property names under `properties` MUST be
// preserved; only disallowed keywords inside subschemas get stripped.

describe('[unit] sanitizeGeminiSchema', () => {
  it('preserves property names under properties (the prod-silence bug)', () => {
    const schema = { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] };
    const out = sanitizeGeminiSchema(schema) as Record<string, any>;
    expect(out.properties).toBeDefined();
    expect(out.properties.query).toEqual({ type: 'string' });
    expect(out.required).toEqual(['query']);
    // Invariant the server enforces: every `required` name exists in properties.
    for (const name of out.required) expect(out.properties[name]).toBeDefined();
  });

  it('strips disallowed keywords ($schema, additionalProperties) but keeps property names', () => {
    const schema = {
      type: 'object',
      $schema: 'http://json-schema.org/draft-07/schema#',
      additionalProperties: false,
      properties: {
        to: { type: 'string', additionalProperties: false },
        subject: { type: 'string' },
        body: { type: 'string' },
      },
      required: ['to', 'subject', 'body'],
    };
    const out = sanitizeGeminiSchema(schema) as Record<string, any>;
    expect(out.$schema).toBeUndefined();
    expect(out.additionalProperties).toBeUndefined();
    expect(out.properties.to).toEqual({ type: 'string' }); // additionalProperties stripped from subschema
    expect(out.properties.subject).toEqual({ type: 'string' });
    expect(out.required).toEqual(['to', 'subject', 'body']);
    for (const name of out.required) expect(out.properties[name]).toBeDefined();
  });

  it('recurses into nested object properties (required names preserved at every level)', () => {
    const schema = {
      type: 'object',
      properties: {
        filters: { type: 'object', properties: { since: { type: 'string' } }, required: ['since'] },
      },
      required: ['filters'],
    };
    const out = sanitizeGeminiSchema(schema) as Record<string, any>;
    expect(out.properties.filters.properties.since).toEqual({ type: 'string' });
    expect(out.properties.filters.required).toEqual(['since']);
    expect(out.properties.filters).toBeDefined();
  });

  it('handles array items subschemas', () => {
    const schema = { type: 'object', properties: { tags: { type: 'array', items: { type: 'string' } } }, required: ['tags'] };
    const out = sanitizeGeminiSchema(schema) as Record<string, any>;
    expect(out.properties.tags.items).toEqual({ type: 'string' });
  });
});

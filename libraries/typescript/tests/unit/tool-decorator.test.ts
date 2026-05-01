import { describe, it, expect } from 'vitest';
import { defineTool } from '../../src/tools/tool-decorator';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const getWeather = defineTool({
  name: 'get_weather',
  description: 'Get the current weather for a location.',
  parameters: {
    location: { type: 'string', description: 'City name or zip code' },
    unit: { type: 'string', description: 'Temperature unit', default: 'celsius' },
  },
  handler: async (args) => {
    const unit = args.unit as string;
    return `Sunny, 22°${unit[0].toUpperCase()}`;
  },
});

const addNumbers = defineTool({
  name: 'add_numbers',
  description: 'Add two numbers.',
  parameters: {
    a: { type: 'integer', description: 'First number' },
    b: { type: 'integer', description: 'Second number' },
  },
  handler: async (args) => {
    return String((args.a as number) + (args.b as number));
  },
});

const allTypes = defineTool({
  name: 'all_types',
  description: 'Accepts all types.',
  parameters: {
    s: { type: 'string', description: 'A string' },
    i: { type: 'integer', description: 'An integer' },
    f: { type: 'number', description: 'A float' },
    b: { type: 'boolean', description: 'A boolean' },
    arr: { type: 'array', description: 'An array' },
    obj: { type: 'object', description: 'An object' },
  },
  handler: async () => 'ok',
});

const allDefaults = defineTool({
  name: 'all_defaults',
  parameters: {
    x: { type: 'integer', description: 'First', default: 0 },
    y: { type: 'integer', description: 'Second', default: 0 },
  },
  handler: async (args) => {
    return String((args.x as number ?? 0) + (args.y as number ?? 0));
  },
});

const noDescription = defineTool({
  name: 'no_desc',
  parameters: {
    value: { type: 'string' },
  },
  handler: async (args) => args.value as string,
});

// ===========================================================================
// Tests
// ===========================================================================

describe('defineTool', () => {
  // -----------------------------------------------------------------------
  // Basic structure
  // -----------------------------------------------------------------------

  describe('basic structure', () => {
    it('returns an object with name, description, parameters, handler', () => {
      expect(getWeather).toHaveProperty('name');
      expect(getWeather).toHaveProperty('description');
      expect(getWeather).toHaveProperty('parameters');
      expect(getWeather).toHaveProperty('handler');
    });

    it('sets the name correctly', () => {
      expect(getWeather.name).toBe('get_weather');
    });

    it('sets the description correctly', () => {
      expect(getWeather.description).toBe('Get the current weather for a location.');
    });

    it('parameters has type "object"', () => {
      expect(getWeather.parameters).toHaveProperty('type', 'object');
    });

    it('parameters has properties object', () => {
      expect(getWeather.parameters).toHaveProperty('properties');
      expect(typeof (getWeather.parameters as Record<string, unknown>).properties).toBe('object');
    });
  });

  // -----------------------------------------------------------------------
  // Required params
  // -----------------------------------------------------------------------

  describe('required parameters', () => {
    it('params without default are in required array', () => {
      const required = (getWeather.parameters as Record<string, unknown>).required as string[];
      expect(required).toContain('location');
    });

    it('params with default are NOT in required array', () => {
      const required = (getWeather.parameters as Record<string, unknown>).required as string[];
      expect(required).not.toContain('unit');
    });

    it('all required when no defaults', () => {
      const required = (addNumbers.parameters as Record<string, unknown>).required as string[];
      expect(required).toEqual(['a', 'b']);
    });

    it('no required key when all params have defaults', () => {
      expect(allDefaults.parameters).not.toHaveProperty('required');
    });
  });

  // -----------------------------------------------------------------------
  // Type mapping
  // -----------------------------------------------------------------------

  describe('type mapping', () => {
    it('string type is preserved', () => {
      const props = (allTypes.parameters as Record<string, unknown>).properties as Record<string, Record<string, unknown>>;
      expect(props.s.type).toBe('string');
    });

    it('integer type is preserved', () => {
      const props = (allTypes.parameters as Record<string, unknown>).properties as Record<string, Record<string, unknown>>;
      expect(props.i.type).toBe('integer');
    });

    it('number type is preserved', () => {
      const props = (allTypes.parameters as Record<string, unknown>).properties as Record<string, Record<string, unknown>>;
      expect(props.f.type).toBe('number');
    });

    it('boolean type is preserved', () => {
      const props = (allTypes.parameters as Record<string, unknown>).properties as Record<string, Record<string, unknown>>;
      expect(props.b.type).toBe('boolean');
    });

    it('array type is preserved', () => {
      const props = (allTypes.parameters as Record<string, unknown>).properties as Record<string, Record<string, unknown>>;
      expect(props.arr.type).toBe('array');
    });

    it('object type is preserved', () => {
      const props = (allTypes.parameters as Record<string, unknown>).properties as Record<string, Record<string, unknown>>;
      expect(props.obj.type).toBe('object');
    });
  });

  // -----------------------------------------------------------------------
  // Descriptions
  // -----------------------------------------------------------------------

  describe('parameter descriptions', () => {
    it('includes description when provided', () => {
      const props = (getWeather.parameters as Record<string, unknown>).properties as Record<string, Record<string, unknown>>;
      expect(props.location.description).toBe('City name or zip code');
      expect(props.unit.description).toBe('Temperature unit');
    });

    it('omits description key when not provided', () => {
      const props = (noDescription.parameters as Record<string, unknown>).properties as Record<string, Record<string, unknown>>;
      expect(props.value).not.toHaveProperty('description');
    });
  });

  // -----------------------------------------------------------------------
  // No description
  // -----------------------------------------------------------------------

  describe('no description', () => {
    it('uses empty string when description is omitted', () => {
      expect(allDefaults.description).toBe('');
    });
  });

  // -----------------------------------------------------------------------
  // Handler
  // -----------------------------------------------------------------------

  describe('handler', () => {
    it('handler is a function', () => {
      expect(typeof getWeather.handler).toBe('function');
    });

    it('handler returns correct result', async () => {
      const result = await getWeather.handler!({ location: 'NYC', unit: 'celsius' }, {});
      expect(result).toBe('Sunny, 22°C');
    });

    it('handler works with different arguments', async () => {
      const result = await getWeather.handler!({ location: 'London', unit: 'fahrenheit' }, {});
      expect(result).toBe('Sunny, 22°F');
    });

    it('add_numbers handler computes correctly', async () => {
      const result = await addNumbers.handler!({ a: 3, b: 4 }, {});
      expect(result).toBe('7');
    });
  });

  // -----------------------------------------------------------------------
  // Valid ToolDefinition
  // -----------------------------------------------------------------------

  describe('ToolDefinition conformance', () => {
    it('has all required ToolDefinition keys', () => {
      const keys = Object.keys(getWeather);
      expect(keys).toContain('name');
      expect(keys).toContain('description');
      expect(keys).toContain('parameters');
      expect(keys).toContain('handler');
    });

    it('name is a string', () => {
      expect(typeof getWeather.name).toBe('string');
    });

    it('description is a string', () => {
      expect(typeof getWeather.description).toBe('string');
    });

    it('parameters is a Record<string, unknown>', () => {
      expect(typeof getWeather.parameters).toBe('object');
      expect(getWeather.parameters).not.toBeNull();
    });
  });
});

import { describe, it, expect } from 'vitest';
import { DefaultToolExecutor } from '../src/llm-loop';
import type { ToolDefinition } from '../src/types';

describe('[unit] DefaultToolExecutor — async generator handlers (#5 streaming)', () => {
  it('forwards { progress } yields to onProgress and returns the final return value', async () => {
    const progressUpdates: string[] = [];
    const onProgress = (text: string): void => { progressUpdates.push(text); };

    const tool: ToolDefinition = {
      name: 'streaming_search',
      description: '',
      parameters: { type: 'object', properties: {} },
      handler: async function* () {
        yield { progress: 'Searching the database...' };
        yield { progress: 'Found 12 matches.' };
        return JSON.stringify({ count: 12, items: ['a', 'b', 'c'] });
      },
    };

    const executor = new DefaultToolExecutor();
    const result = await executor.execute(tool, {}, {}, onProgress);

    expect(progressUpdates).toEqual([
      'Searching the database...',
      'Found 12 matches.',
    ]);
    expect(result).toBe('{"count":12,"items":["a","b","c"]}');
  });

  it('falls back to the last { result } yield when the generator does not return', async () => {
    const tool: ToolDefinition = {
      name: 'no_return',
      description: '',
      parameters: { type: 'object', properties: {} },
      handler: async function* () {
        yield { progress: 'Working...' };
        yield { result: '{"final":"value"}' };
        // implicit undefined return
      },
    };

    const executor = new DefaultToolExecutor();
    const result = await executor.execute(tool, {}, {});
    expect(result).toBe('{"final":"value"}');
  });

  it('still works when no onProgress is provided (yields are silently consumed)', async () => {
    const tool: ToolDefinition = {
      name: 'no_progress_sink',
      description: '',
      parameters: { type: 'object', properties: {} },
      handler: async function* () {
        yield { progress: 'this disappears' };
        return 'ok';
      },
    };

    const executor = new DefaultToolExecutor();
    const result = await executor.execute(tool, {}, {});
    expect(result).toBe('ok');
  });

  it('keeps backward compatibility: plain async functions still return their string', async () => {
    const tool: ToolDefinition = {
      name: 'classic',
      description: '',
      parameters: { type: 'object', properties: {} },
      handler: async () => '"plain result"',
    };

    const executor = new DefaultToolExecutor();
    const result = await executor.execute(tool, {}, {});
    expect(result).toBe('"plain result"');
  });

  it('errors thrown inside a generator are caught and retried like plain async functions', async () => {
    let attempts = 0;
    const tool: ToolDefinition = {
      name: 'flaky_generator',
      description: '',
      parameters: { type: 'object', properties: {} },
      handler: async function* () {
        attempts++;
        if (attempts < 3) throw new Error('transient');
        yield { progress: 'ok now' };
        return '"recovered"';
      },
    };

    const executor = new DefaultToolExecutor({ retryDelayMs: 1 });
    const result = await executor.execute(tool, {}, {});
    expect(result).toBe('"recovered"');
    expect(attempts).toBe(3);
  });
});

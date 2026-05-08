/**
 * Mocked tests for `MCPManager` in `src/tools/mcp-client.ts`.
 *
 * Mocks the `@modelcontextprotocol/sdk` outer boundary only — every
 * config-validation, tool-wrapping, dispatch, collision-detection, and
 * lifecycle path inside `MCPManager` runs the real code under test.
 * Mirrors the Python `tests/test_mcp_client.py` coverage.
 *
 * File suffix `.mocked.test.ts` per `.claude/rules/authentic-tests.md`
 * because we mock the external SDK import.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ToolDefinition } from '../src/types';

// ---------------------------------------------------------------------------
// Mock @modelcontextprotocol/sdk subpath imports.
//
// `vi.mock` is hoisted, so the factory cannot close over module-scope vars.
// We stash state on the mock constructors themselves so tests can read what
// the SDK code did via `MockClientCtor.instances` etc.
// ---------------------------------------------------------------------------

interface MockClient {
  info: { name: string; version: string };
  connectCalledWith: unknown;
  listToolsResult: unknown;
  callToolCalls: Array<{ name: string; arguments: Record<string, unknown> }>;
  callToolResult: unknown;
  callToolThrows: Error | null;
  closed: boolean;
  connectThrows: Error | null;
  connect: (transport: unknown) => Promise<void>;
  listTools: () => Promise<unknown>;
  callTool: (req: { name: string; arguments: Record<string, unknown> }) => Promise<unknown>;
  close: () => Promise<void>;
}

interface MockTransport {
  url: URL;
  opts: { requestInit?: { headers?: Record<string, string> } } | undefined;
  closed: boolean;
  close: () => Promise<void>;
}

interface MockClientCtorType {
  new (info: { name: string; version: string }): MockClient;
  instances: MockClient[];
  // Per-instance overrides queued FIFO. Each constructor pops one.
  nextListToolsResult: unknown[];
  nextCallToolResult: unknown[];
  nextCallToolThrows: Array<Error | null>;
  nextConnectThrows: Array<Error | null>;
}

interface MockTransportCtorType {
  new (
    url: URL,
    opts?: { requestInit?: { headers?: Record<string, string> } },
  ): MockTransport;
  instances: MockTransport[];
}

vi.mock('@modelcontextprotocol/sdk/client/index.js', () => {
  class MockClientImpl implements MockClient {
    info: { name: string; version: string };
    connectCalledWith: unknown = null;
    listToolsResult: unknown = { tools: [] };
    callToolCalls: Array<{ name: string; arguments: Record<string, unknown> }> = [];
    callToolResult: unknown = { content: [], isError: false };
    callToolThrows: Error | null = null;
    closed = false;
    connectThrows: Error | null = null;

    constructor(info: { name: string; version: string }) {
      this.info = info;
      const ctor = MockClientImpl as unknown as MockClientCtorType;
      ctor.instances.push(this);
      if (ctor.nextListToolsResult.length > 0) {
        this.listToolsResult = ctor.nextListToolsResult.shift();
      }
      if (ctor.nextCallToolResult.length > 0) {
        this.callToolResult = ctor.nextCallToolResult.shift();
      }
      if (ctor.nextCallToolThrows.length > 0) {
        this.callToolThrows = ctor.nextCallToolThrows.shift() ?? null;
      }
      if (ctor.nextConnectThrows.length > 0) {
        this.connectThrows = ctor.nextConnectThrows.shift() ?? null;
      }
    }

    async connect(transport: unknown): Promise<void> {
      this.connectCalledWith = transport;
      if (this.connectThrows) throw this.connectThrows;
    }

    async listTools(): Promise<unknown> {
      return this.listToolsResult;
    }

    async callTool(req: { name: string; arguments: Record<string, unknown> }): Promise<unknown> {
      this.callToolCalls.push(req);
      if (this.callToolThrows) throw this.callToolThrows;
      return this.callToolResult;
    }

    async close(): Promise<void> {
      this.closed = true;
    }
  }
  (MockClientImpl as unknown as MockClientCtorType).instances = [];
  (MockClientImpl as unknown as MockClientCtorType).nextListToolsResult = [];
  (MockClientImpl as unknown as MockClientCtorType).nextCallToolResult = [];
  (MockClientImpl as unknown as MockClientCtorType).nextCallToolThrows = [];
  (MockClientImpl as unknown as MockClientCtorType).nextConnectThrows = [];
  return { Client: MockClientImpl };
});

vi.mock('@modelcontextprotocol/sdk/client/streamableHttp.js', () => {
  class MockTransportImpl implements MockTransport {
    url: URL;
    opts: { requestInit?: { headers?: Record<string, string> } } | undefined;
    closed = false;

    constructor(url: URL, opts?: { requestInit?: { headers?: Record<string, string> } }) {
      this.url = url;
      this.opts = opts;
      (MockTransportImpl as unknown as MockTransportCtorType).instances.push(this);
    }

    async close(): Promise<void> {
      this.closed = true;
    }
  }
  (MockTransportImpl as unknown as MockTransportCtorType).instances = [];
  return { StreamableHTTPClientTransport: MockTransportImpl };
});

// Pull the mocked module references after registration so we can inspect them.
const { Client: MockClientCtor } = (await import(
  '@modelcontextprotocol/sdk/client/index.js'
)) as { Client: MockClientCtorType };
const { StreamableHTTPClientTransport: MockTransportCtor } = (await import(
  '@modelcontextprotocol/sdk/client/streamableHttp.js'
)) as { StreamableHTTPClientTransport: MockTransportCtorType };

// Import the unit under test AFTER mocks are registered.
import { MCPManager } from '../src/tools/mcp-client';

beforeEach(() => {
  MockClientCtor.instances.length = 0;
  MockClientCtor.nextListToolsResult.length = 0;
  MockClientCtor.nextCallToolResult.length = 0;
  MockClientCtor.nextCallToolThrows.length = 0;
  MockClientCtor.nextConnectThrows.length = 0;
  MockTransportCtor.instances.length = 0;
});

// ---------------------------------------------------------------------------
// 1. Config validation
// ---------------------------------------------------------------------------

describe('[mocked] MCPManager — config validation', () => {
  it('accepts a string shorthand (resolves to { url })', async () => {
    MockClientCtor.nextListToolsResult.push({ tools: [] });
    const mgr = new MCPManager(['https://mcp.example.com/sse']);
    await mgr.connect();
    await mgr.close();
    expect(MockTransportCtor.instances).toHaveLength(1);
    expect(MockTransportCtor.instances[0].url.toString()).toBe(
      'https://mcp.example.com/sse',
    );
  });

  it('preserves a full options object (url + headers + name)', async () => {
    MockClientCtor.nextListToolsResult.push({ tools: [] });
    const mgr = new MCPManager([
      {
        url: 'https://mcp.paypal.com/sse',
        headers: { Authorization: 'Bearer xyz' },
        name: 'paypal',
      },
    ]);
    await mgr.connect();
    await mgr.close();
    expect(MockTransportCtor.instances[0].url.toString()).toBe(
      'https://mcp.paypal.com/sse',
    );
    expect(MockTransportCtor.instances[0].opts?.requestInit?.headers).toEqual({
      Authorization: 'Bearer xyz',
    });
  });

  it("throws with the index when 'url' is missing", () => {
    expect(
      () =>
        new MCPManager([
          'https://ok.example.com/sse',
          // missing url -- index 1
          { headers: { x: 'y' } } as unknown as { url: string },
        ]),
    ).toThrow(/mcpServers\[1\].*url/);
  });
});

// ---------------------------------------------------------------------------
// 2. hasServers getter
// ---------------------------------------------------------------------------

describe('[mocked] MCPManager — hasServers getter', () => {
  it('is false when undefined', () => {
    expect(new MCPManager(undefined).hasServers).toBe(false);
  });

  it('is false when empty array', () => {
    expect(new MCPManager([]).hasServers).toBe(false);
  });

  it('is true when configs are present', () => {
    expect(new MCPManager(['https://mcp.example.com/sse']).hasServers).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 3. tools/list discovery
// ---------------------------------------------------------------------------

describe('[mocked] MCPManager — tools/list discovery', () => {
  it('wraps discovered MCP tools as ToolDefinitions with synthetic handlers', async () => {
    MockClientCtor.nextListToolsResult.push({
      tools: [
        {
          name: 'search_email',
          description: 'Search Gmail',
          inputSchema: { type: 'object', properties: { q: { type: 'string' } } },
        },
        { name: 'send_email' }, // missing description + inputSchema
      ],
    });
    const mgr = new MCPManager(['https://mcp.googleworkspace.com/sse']);
    const tools = await mgr.connect();
    await mgr.close();

    expect(tools).toHaveLength(2);
    expect(tools[0].name).toBe('search_email');
    expect(tools[0].description).toBe('Search Gmail');
    expect(tools[0].parameters).toEqual({
      type: 'object',
      properties: { q: { type: 'string' } },
    });
    expect(typeof tools[0].handler).toBe('function');
    expect(tools[1].name).toBe('send_email');
    // Defaults: empty description and an open object schema.
    expect(tools[1].description).toBe('');
    expect(tools[1].parameters).toEqual({ type: 'object', properties: {} });
  });

  it('skips tools with no name', async () => {
    MockClientCtor.nextListToolsResult.push({
      tools: [{ name: '' }, { name: 'real_tool' }],
    });
    const mgr = new MCPManager(['https://mcp.example.com/sse']);
    const tools = await mgr.connect();
    await mgr.close();
    expect(tools.map((t) => t.name)).toEqual(['real_tool']);
  });
});

// ---------------------------------------------------------------------------
// 4. tools/call dispatch
// ---------------------------------------------------------------------------

describe('[mocked] MCPManager — tools/call dispatch', () => {
  it('synthetic handler routes to client.callTool with forwarded args and returns text', async () => {
    MockClientCtor.nextListToolsResult.push({
      tools: [{ name: 'echo_tool', description: '', inputSchema: {} }],
    });
    MockClientCtor.nextCallToolResult.push({
      content: [{ type: 'text', text: 'hello back' }],
      isError: false,
    });

    const mgr = new MCPManager(['https://mcp.example.com/sse']);
    const tools = await mgr.connect();
    const handler = tools[0].handler as (
      args: Record<string, unknown>,
      ctx: Record<string, unknown>,
    ) => Promise<string>;
    const result = await handler({ msg: 'hi' }, {});
    await mgr.close();

    expect(result).toBe('hello back');
    expect(MockClientCtor.instances).toHaveLength(1);
    expect(MockClientCtor.instances[0].callToolCalls).toEqual([
      { name: 'echo_tool', arguments: { msg: 'hi' } },
    ]);
  });

  it('serialises isError responses as the structured fallback envelope', async () => {
    MockClientCtor.nextListToolsResult.push({
      tools: [{ name: 'flaky_tool' }],
    });
    MockClientCtor.nextCallToolResult.push({
      content: [{ type: 'text', text: 'upstream 500' }],
      isError: true,
    });

    const mgr = new MCPManager(['https://mcp.example.com/sse']);
    const tools = await mgr.connect();
    const handler = tools[0].handler as (
      args: Record<string, unknown>,
      ctx: Record<string, unknown>,
    ) => Promise<string>;
    const result = await handler({}, {});
    await mgr.close();

    const parsed = JSON.parse(result);
    expect(parsed.fallback).toBe(true);
    expect(parsed.error).toContain('upstream 500');
  });

  it('joins multi-block content with newlines (text + non-text blocks)', async () => {
    MockClientCtor.nextListToolsResult.push({ tools: [{ name: 'multi_tool' }] });
    MockClientCtor.nextCallToolResult.push({
      content: [
        { type: 'text', text: 'line one' },
        { type: 'image', url: 'https://x/y.png' }, // non-text → JSON-serialised
      ],
      isError: false,
    });

    const mgr = new MCPManager(['https://mcp.example.com/sse']);
    const tools = await mgr.connect();
    const handler = tools[0].handler as (
      args: Record<string, unknown>,
      ctx: Record<string, unknown>,
    ) => Promise<string>;
    const result = await handler({}, {});
    await mgr.close();

    const lines = result.split('\n');
    expect(lines[0]).toBe('line one');
    expect(JSON.parse(lines[1])).toEqual({
      type: 'image',
      url: 'https://x/y.png',
    });
  });
});

// ---------------------------------------------------------------------------
// 5. Tool-name collision
// ---------------------------------------------------------------------------

describe('[mocked] MCPManager — tool-name collision', () => {
  function makeTool(name: string): ToolDefinition {
    return {
      name,
      description: '',
      parameters: {},
      handler: async () => '',
    };
  }

  it('throws with the offending tool name when user + MCP collide', () => {
    expect(() =>
      MCPManager.assertNoConflicts(
        [makeTool('send_email'), makeTool('list_inbox')],
        [makeTool('search_email'), makeTool('send_email')],
      ),
    ).toThrow(/send_email/);
  });

  it('is a no-op when user tools are absent', () => {
    expect(() =>
      MCPManager.assertNoConflicts(undefined, [makeTool('any')]),
    ).not.toThrow();
    expect(() => MCPManager.assertNoConflicts([], [makeTool('any')])).not.toThrow();
  });

  it('is a no-op when MCP tools are empty', () => {
    expect(() => MCPManager.assertNoConflicts([makeTool('x')], [])).not.toThrow();
  });

  it('passes when names are disjoint', () => {
    expect(() =>
      MCPManager.assertNoConflicts([makeTool('local_a')], [makeTool('remote_b')]),
    ).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// 6. Auth headers propagation
// ---------------------------------------------------------------------------

describe('[mocked] MCPManager — auth headers propagation', () => {
  it('forwards Authorization header to the transport constructor', async () => {
    MockClientCtor.nextListToolsResult.push({ tools: [] });
    const mgr = new MCPManager([
      {
        url: 'https://mcp.paypal.com/sse',
        headers: { Authorization: 'Bearer secret_xyz' },
      },
    ]);
    await mgr.connect();
    await mgr.close();

    expect(MockTransportCtor.instances).toHaveLength(1);
    expect(MockTransportCtor.instances[0].opts?.requestInit?.headers).toEqual({
      Authorization: 'Bearer secret_xyz',
    });
  });

  it('passes empty headers when string shorthand is used', async () => {
    MockClientCtor.nextListToolsResult.push({ tools: [] });
    const mgr = new MCPManager(['https://mcp.example.com/sse']);
    await mgr.connect();
    await mgr.close();
    expect(MockTransportCtor.instances[0].opts?.requestInit?.headers).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// 7. Per-call lifecycle
// ---------------------------------------------------------------------------

describe('[mocked] MCPManager — lifecycle', () => {
  it('connect() opens the client and close() closes it', async () => {
    MockClientCtor.nextListToolsResult.push({ tools: [] });
    const mgr = new MCPManager(['https://mcp.example.com/sse']);
    await mgr.connect();
    expect(MockClientCtor.instances).toHaveLength(1);
    expect(MockClientCtor.instances[0].connectCalledWith).not.toBeNull();
    expect(MockClientCtor.instances[0].closed).toBe(false);

    await mgr.close();
    expect(MockClientCtor.instances[0].closed).toBe(true);
  });

  it('close() called twice is safe', async () => {
    MockClientCtor.nextListToolsResult.push({ tools: [] });
    const mgr = new MCPManager(['https://mcp.example.com/sse']);
    await mgr.connect();
    await mgr.close();
    await expect(mgr.close()).resolves.toBeUndefined();
  });

  it('connect() returns [] without touching the SDK when no servers configured', async () => {
    const mgr = new MCPManager(undefined);
    const result = await mgr.connect();
    expect(result).toEqual([]);
    expect(MockClientCtor.instances).toHaveLength(0);
    expect(MockTransportCtor.instances).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 8. Optional dependency absent
//
// Notes: vitest cannot un-mock an already-mocked module mid-file in a way
// that triggers the dynamic import to fail, so we use `vi.doMock` + a
// dynamic re-import of `mcp-client` in an isolated module context.
// `vi.resetModules()` ensures the new mock takes effect.
// ---------------------------------------------------------------------------

describe('[mocked] MCPManager — optional dep absent', () => {
  it('throws a clear ImportError-style message when @modelcontextprotocol/sdk is missing and servers are configured', async () => {
    vi.resetModules();
    vi.doMock('@modelcontextprotocol/sdk/client/index.js', () => {
      throw new Error(
        "Cannot find package '@modelcontextprotocol/sdk' imported from test",
      );
    });
    vi.doMock('@modelcontextprotocol/sdk/client/streamableHttp.js', () => {
      throw new Error(
        "Cannot find package '@modelcontextprotocol/sdk' imported from test",
      );
    });

    try {
      const { MCPManager: FreshMCPManager } = await import('../src/tools/mcp-client');
      const mgr = new FreshMCPManager(['https://mcp.example.com/sse']);
      await expect(mgr.connect()).rejects.toThrow(
        /mcpServers configured.*@modelcontextprotocol\/sdk.*npm install/s,
      );
    } finally {
      vi.doUnmock('@modelcontextprotocol/sdk/client/index.js');
      vi.doUnmock('@modelcontextprotocol/sdk/client/streamableHttp.js');
      vi.resetModules();
    }
  });

  it('does NOT attempt to import the SDK when mcpServers is omitted (zero-cost)', async () => {
    vi.resetModules();
    let importAttempted = false;
    vi.doMock('@modelcontextprotocol/sdk/client/index.js', () => {
      importAttempted = true;
      throw new Error('should not be imported');
    });
    vi.doMock('@modelcontextprotocol/sdk/client/streamableHttp.js', () => {
      importAttempted = true;
      throw new Error('should not be imported');
    });

    try {
      const { MCPManager: FreshMCPManager } = await import('../src/tools/mcp-client');
      const mgr = new FreshMCPManager(undefined);
      const result = await mgr.connect();
      expect(result).toEqual([]);
      expect(importAttempted).toBe(false);
    } finally {
      vi.doUnmock('@modelcontextprotocol/sdk/client/index.js');
      vi.doUnmock('@modelcontextprotocol/sdk/client/streamableHttp.js');
      vi.resetModules();
    }
  });
});

/**
 * Model Context Protocol (MCP) client integration for Patter.
 *
 * Lets users plug a Patter agent into MCP servers (Google Workspace,
 * PayPal, Postgres, GitHub, …) without writing a wrapper handler per
 * service:
 *
 *   ```ts
 *   phone.agent({
 *     mcpServers: [
 *       'https://mcp.googleworkspace.com/sse',
 *       { url: 'https://mcp.paypal.com/sse', headers: { Authorization: '...' } },
 *     ],
 *   });
 *   ```
 *
 * At call start, the SDK queries each server's ``tools/list``,
 * registers the discovered tools as Patter ``ToolDefinition``s with a
 * synthetic ``handler`` that dispatches to ``tools/call``, and merges
 * them into ``agent.tools`` before sending the function-tool list to
 * the underlying model (Realtime ``session.update`` or pipeline LLM).
 *
 * Lazy import: ``@modelcontextprotocol/sdk`` is an optional dependency.
 * Users who do not configure ``mcpServers`` never pay the install cost
 * and the SDK ships without a hard dependency.
 *
 * Limitations of the MVP (will iterate):
 *  - Per-call connection (handshake on every call: ~50-200 ms × N
 *    servers). Caching ``listTools`` results process-wide is a follow-up.
 *  - Streamable HTTP transport only — ``stdio`` and the legacy ``SSE``
 *    fallback are not exposed yet.
 *  - No tool-name conflict resolution: if an MCP tool collides with a
 *    user-defined ``agent.tools`` entry, MCP is rejected at startup.
 */

import type { ToolDefinition } from '../types';
import { getLogger } from '../logger';
import { validateWebhookUrl } from '../server';
import { VERSION } from '../version';

/** Public MCP server config. ``string`` is shorthand for ``{ url }``. */
export type MCPServerConfig =
  | string
  | {
      readonly url: string;
      /** Headers attached to every transport request — typically auth. */
      readonly headers?: Record<string, string>;
      /** Optional logical name for telemetry / log lines. */
      readonly name?: string;
    };

interface ResolvedConfig {
  readonly url: string;
  readonly headers: Record<string, string>;
  readonly name: string;
}

function resolveConfig(input: MCPServerConfig, index: number): ResolvedConfig {
  if (typeof input === 'string') {
    return { url: input, headers: {}, name: `mcp[${index}]` };
  }
  if (!input.url) {
    throw new Error(`mcpServers[${index}]: missing required 'url' field`);
  }
  return {
    url: input.url,
    headers: input.headers ?? {},
    name: input.name ?? `mcp[${index}]`,
  };
}

/** Loaded ``@modelcontextprotocol/sdk`` types — opaque to keep the
 *  optional dependency truly optional. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type MCPClientLike = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type MCPTransportLike = any;

interface ConnectedServer {
  readonly config: ResolvedConfig;
  readonly client: MCPClientLike;
  readonly transport: MCPTransportLike;
}

/**
 * Manages a set of MCP server connections for a single Patter call.
 * Lifecycle: ``connect()`` once, ``getDiscoveredTools()`` returns the
 * merged tool list with synthetic handlers, ``close()`` on call end.
 */
export class MCPManager {
  private readonly configs: ReadonlyArray<ResolvedConfig>;
  private connected: ConnectedServer[] = [];

  constructor(servers: ReadonlyArray<MCPServerConfig> | undefined) {
    this.configs = (servers ?? []).map((s, i) => resolveConfig(s, i));
  }

  get hasServers(): boolean {
    return this.configs.length > 0;
  }

  /** Connect to every configured server and discover their tools.
   *  Returns the discovered tools wrapped as Patter ``ToolDefinition``s. */
  async connect(): Promise<ToolDefinition[]> {
    if (this.configs.length === 0) return [];

    let mcpModule: { Client: new (info: { name: string; version: string }) => MCPClientLike };
    let transportModule: {
      StreamableHTTPClientTransport: new (
        url: URL,
        opts?: { requestInit?: { headers?: Record<string, string> } },
      ) => MCPTransportLike;
    };
    try {
      // Dynamic import keeps @modelcontextprotocol/sdk optional. Subpath
      // imports per the SDK's package.json exports map.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      mcpModule = (await import('@modelcontextprotocol/sdk/client/index.js' as any)) as typeof mcpModule;
      transportModule = (await import(
        '@modelcontextprotocol/sdk/client/streamableHttp.js' as never
      )) as typeof transportModule;
    } catch (e) {
      throw new Error(
        'mcpServers configured but `@modelcontextprotocol/sdk` is not installed. ' +
          'Run `npm install @modelcontextprotocol/sdk` to enable MCP support. ' +
          `(import error: ${String(e)})`,
      );
    }

    const aggregatedTools: ToolDefinition[] = [];
    for (const cfg of this.configs) {
      try {
        validateWebhookUrl(cfg.url);
      } catch (e) {
        getLogger().error(`MCP server '${cfg.name}' (${cfg.url}) rejected by SSRF guard: ${String(e)}`);
        continue;
      }
      const transport = new transportModule.StreamableHTTPClientTransport(new URL(cfg.url), {
        requestInit: { headers: cfg.headers },
      });
      const client = new mcpModule.Client({ name: 'patter', version: VERSION });
      try {
        await client.connect(transport);
      } catch (e) {
        getLogger().error(`MCP server '${cfg.name}' (${cfg.url}) connect failed: ${String(e)}`);
        // Best effort cleanup of the half-connected transport.
        try { await transport.close?.(); } catch { /* ignore */ }
        continue;
      }
      this.connected.push({ config: cfg, client, transport });

      let listed: { tools?: Array<{ name: string; description?: string; inputSchema?: Record<string, unknown> }> };
      try {
        listed = await client.listTools();
      } catch (e) {
        getLogger().error(`MCP server '${cfg.name}' tools/list failed: ${String(e)}`);
        continue;
      }
      const tools = Array.isArray(listed?.tools) ? listed.tools : [];
      for (const t of tools) {
        if (!t?.name) continue;
        aggregatedTools.push({
          name: t.name,
          description: t.description ?? '',
          parameters: (t.inputSchema as Record<string, unknown>) ?? { type: 'object', properties: {} },
          handler: async (args: Record<string, unknown>): Promise<string> => {
            const callResult: { content?: Array<{ type: string; text?: string }>; isError?: boolean } = await client.callTool({
              name: t.name,
              arguments: args,
            });
            const text = (callResult.content ?? [])
              .map((c) => (c.type === 'text' ? c.text ?? '' : JSON.stringify(c)))
              .join('\n');
            // MCP errors arrive as ``isError: true`` with a content
            // payload; surface them as the structured fallback shape so
            // the model can recover gracefully (parity with our local
            // tool error envelope).
            if (callResult.isError) {
              return JSON.stringify({ error: text || 'MCP tool error', fallback: true });
            }
            return text || '{}';
          },
        });
      }
      getLogger().info(`MCP server '${cfg.name}' registered ${tools.length} tool(s)`);
    }
    return aggregatedTools;
  }

  /** Validate no tool name collides between MCP-discovered and
   *  user-supplied tools. Throws on conflict so the user fixes it. */
  static assertNoConflicts(
    userTools: ReadonlyArray<ToolDefinition> | undefined,
    mcpTools: ReadonlyArray<ToolDefinition>,
  ): void {
    if (!userTools || userTools.length === 0 || mcpTools.length === 0) return;
    const userNames = new Set(userTools.map((t) => t.name));
    for (const mcp of mcpTools) {
      if (userNames.has(mcp.name)) {
        throw new Error(
          `MCP tool '${mcp.name}' collides with a user-supplied tool of the same name. ` +
            'Rename one of them or remove the duplicate from agent.tools.',
        );
      }
    }
  }

  /** Close every open MCP connection. Idempotent; logs but does not
   *  throw on individual failures (we don't want a flaky shutdown to
   *  derail the call-end teardown). */
  async close(): Promise<void> {
    const conns = this.connected;
    this.connected = [];
    for (const conn of conns) {
      try {
        await conn.client.close?.();
      } catch (e) {
        getLogger().debug(`MCP server '${conn.config.name}' close error (ignored): ${String(e)}`);
      }
    }
  }
}

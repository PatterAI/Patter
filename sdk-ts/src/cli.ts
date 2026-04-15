#!/usr/bin/env node

/**
 * Patter CLI — standalone dashboard and utilities.
 *
 * Usage:
 *   npx getpatter dashboard [--port 8000]
 */

import { createServer } from 'node:http';
import express from 'express';
import { MetricsStore } from './dashboard/store';
import { mountDashboard, mountApi } from './dashboard/routes';
import { getLogger } from './logger';

const BANNER = `
██████╗  █████╗ ████████╗████████╗███████╗██████╗
██╔══██╗██╔══██╗╚══██╔══╝╚══██╔══╝██╔════╝██╔══██╗
██████╔╝███████║   ██║      ██║   █████╗  ██████╔╝
██╔═══╝ ██╔══██║   ██║      ██║   ██╔══╝  ██╔══██╗
██║     ██║  ██║   ██║      ██║   ███████╗██║  ██║
╚═╝     ╚═╝  ╚═╝   ╚═╝      ╚═╝   ╚══════╝╚═╝  ╚═╝

Connect AI agents to phone numbers in 4 lines of code
`;

function parseArgs(argv: string[]): { port: number } {
  const args = argv.slice(2);
  let port = 8000;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === 'dashboard') continue;
    if (args[i] === '--port' && args[i + 1]) {
      port = parseInt(args[i + 1], 10);
      i++;
    } else if (args[i] === '--help' || args[i] === '-h') {
      console.log('Usage: getpatter dashboard [--port 8000]');
      process.exit(0);
    }
  }

  return { port };
}

async function main(): Promise<void> {
  const command = process.argv[2];
  if (command !== 'dashboard') {
    console.log('Usage: getpatter dashboard [--port 8000]');
    process.exit(command ? 1 : 0);
  }

  const { port } = parseArgs(process.argv);

  console.log(BANNER);

  const store = new MetricsStore();

  console.log(`  Dashboard:  http://localhost:${port}/`);
  console.log(`  API:        http://localhost:${port}/api/v1/calls`);
  console.log();
  console.log('  Waiting for calls…  Press Ctrl+C to stop.\n');

  const app = express();
  app.use(express.json());

  mountDashboard(app, store);
  mountApi(app, store);

  app.get('/health', (_req, res) => {
    res.json({ status: 'ok', mode: 'dashboard' });
  });

  // Ingest endpoint — SDK POSTs completed call data here for live updates
  app.post('/api/dashboard/ingest', (req, res) => {
    const data = req.body as Record<string, unknown>;
    const callId = (data.call_id as string) || '';
    if (!callId) {
      res.json({ ok: false, error: 'missing call_id' });
      return;
    }
    store.recordCallStart(data);
    if (data.ended_at) {
      store.recordCallEnd(data, (data.metrics as Record<string, unknown>) ?? null);
    }
    res.json({ ok: true, call_id: callId });
  });

  const server = createServer(app);

  // Track open connections so we can destroy them on shutdown
  const connections = new Set<import('node:net').Socket>();
  server.on('connection', (conn) => {
    connections.add(conn);
    conn.on('close', () => connections.delete(conn));
  });

  server.listen(port, '127.0.0.1', () => {
    getLogger().info(`Dashboard server listening on port ${port}`);
  });

  const shutdown = () => {
    console.log('\nShutting down dashboard...');
    // Destroy all open connections (including SSE keep-alive)
    for (const conn of connections) conn.destroy();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1000);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  console.error('Failed to start dashboard:', err);
  process.exit(1);
});

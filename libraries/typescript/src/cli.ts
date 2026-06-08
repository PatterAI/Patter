#!/usr/bin/env node

/**
 * Patter CLI — standalone dashboard and utilities.
 *
 * Usage:
 *   npx getpatter dashboard [--port 8000]
 *   npx getpatter eval                        (stub — evals are Python-only today)
 *   npx getpatter telemetry [status|disable|enable]
 */

import { createServer } from 'node:http';
import express from 'express';
import { MetricsStore } from './dashboard/store';
import { mountDashboard, mountApi } from './dashboard/routes';
import { getLogger } from './logger';
import { showBanner } from './banner';
import { VERSION } from './version';
import { TelemetryClient, DEFAULT_ENDPOINT } from './telemetry/client';
import { isEnabled } from './telemetry/consent';
import { isOptedOut, setOptOut } from './telemetry/install-id';

/**
 * Record which CLI command was invoked (the name only — never args/flags), then
 * flush. `process.exit()` skips the client's `beforeExit` flush hook, so we flush
 * explicitly here, bounded so the CLI stays snappy even when the collector is
 * unreachable. Best-effort and fail-safe — never blocks or breaks the CLI.
 */
async function emitCliCommand(command: string): Promise<void> {
  try {
    const client = new TelemetryClient({ sdkVersion: VERSION });
    client.record('cli_command', { cli_command: command });
    const timeout = new Promise<void>((resolve) => {
      const t = setTimeout(resolve, 600);
      (t as { unref?: () => void }).unref?.();
    });
    await Promise.race([client.close(), timeout]);
  } catch {
    /* best-effort — never break the CLI */
  }
}

/**
 * Implement `getpatter telemetry status|disable|enable` (parity with
 * `next telemetry`). Persists a machine-level opt-out marker read by consent.
 */
function runTelemetryCommand(action: string | undefined): number {
  const act = action ?? 'status';
  if (act === 'disable') {
    try {
      setOptOut(true);
    } catch (err) {
      console.log(`Could not write the opt-out marker: ${String(err)}`);
      return 1;
    }
    console.log('Anonymous telemetry disabled. No usage data will be sent.');
    return 0;
  }
  if (act === 'enable') {
    try {
      setOptOut(false);
    } catch (err) {
      console.log(`Could not remove the opt-out marker: ${String(err)}`);
      return 1;
    }
    console.log('Anonymous telemetry re-enabled (opt-out model, on by default).');
    return 0;
  }
  if (act !== 'status') {
    console.log('Usage: getpatter telemetry [status|disable|enable]');
    return 1;
  }
  const endpoint = process.env.PATTER_TELEMETRY_ENDPOINT || DEFAULT_ENDPOINT;
  console.log(`Anonymous usage telemetry: ${isEnabled() ? 'ENABLED' : 'DISABLED'}`);
  if (isOptedOut()) {
    console.log('  Opted out via: getpatter telemetry disable (persisted marker)');
  }
  console.log(`  Endpoint: ${endpoint}`);
  console.log('  Inspect what would be sent (prints, sends nothing): PATTER_TELEMETRY_DEBUG=1');
  console.log(
    '  Disable: getpatter telemetry disable  |  DO_NOT_TRACK=1  |  PATTER_TELEMETRY_DISABLED=1',
  );
  console.log('  Details: https://docs.getpatter.com/telemetry');
  return 0;
}

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

function printEvalStub(): void {
  console.log(
    'Evaluations are not yet available in the TypeScript SDK.\n' +
      'Use the Python SDK instead:\n\n' +
      '  pip install getpatter\n' +
      '  patter eval --help\n\n' +
      'See https://github.com/PatterAI/Patter for docs.',
  );
}

async function main(): Promise<void> {
  const command = process.argv[2];

  // Telemetry control command — never emits telemetry itself (disabling must not
  // phone home on the very invocation that opts the user out).
  if (command === 'telemetry') {
    process.exit(runTelemetryCommand(process.argv[3]));
  }

  if (command === 'eval') {
    await emitCliCommand('eval');
    printEvalStub();
    process.exit(0);
  }
  if (command !== 'dashboard') {
    await emitCliCommand(command ? 'other' : 'none');
    console.log('Usage: getpatter dashboard [--port 8000]');
    console.log('       getpatter eval          (stub — use Python SDK for evals)');
    console.log('       getpatter telemetry [status|disable|enable]');
    process.exit(command ? 1 : 0);
  }

  await emitCliCommand('dashboard');

  const { port } = parseArgs(process.argv);

  showBanner();

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

  // Ingest endpoint — SDK POSTs call lifecycle events here so a
  // standalone dashboard surfaces them live. Three event kinds:
  //   * status="initiated" — outbound dial handed off to carrier,
  //     callee hasn't picked up yet. Surfaces the row immediately so
  //     the user sees the attempt during ringing.
  //   * default (no status) — call_start, media stream began.
  //   * ended_at present — call_end, final metrics + transcript.
  app.post('/api/dashboard/ingest', (req, res) => {
    const data = req.body as Record<string, unknown>;
    const callId = (data.call_id as string) || '';
    if (!callId) {
      res.json({ ok: false, error: 'missing call_id' });
      return;
    }
    const status = data.status as string | undefined;
    if (status === 'initiated') {
      store.recordCallInitiated(data);
      res.json({ ok: true, call_id: callId, event: 'initiated' });
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

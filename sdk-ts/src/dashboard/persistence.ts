/**
 * Dashboard notification for live call updates.
 *
 * When the SDK completes a call, it fires a POST to the standalone dashboard
 * (if running) so calls appear in real time.  Data lives only in memory —
 * nothing is written to disk.
 */

export function notifyDashboard(
  callData: Record<string, unknown>,
  port = 8000,
): void {
  try {
    const body = JSON.stringify(callData);
    const req = (require('node:http') as typeof import('node:http')).request(
      {
        hostname: '127.0.0.1',
        port,
        path: '/api/dashboard/ingest',
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
        },
        timeout: 1000,
      },
      () => { /* ignore response */ },
    );
    req.on('error', () => { /* dashboard not running, ignore */ });
    req.write(body);
    req.end();
  } catch {
    // silently ignore
  }
}

/**
 * Dashboard notification for live call updates.
 *
 * When the SDK completes a call, it fires a POST to the standalone dashboard
 * (if running) so calls appear in real time.  Data lives only in memory —
 * nothing is written to disk.
 *
 * TODO(parity): Python's `notify_dashboard` is now an async fire-and-forget
 * coroutine (see sdk-py/getpatter/dashboard/persistence.py). This TS version
 * uses `http.request` which is already non-blocking, but for parity consider
 * exposing this as `async function notifyDashboard(...): Promise<void>` so
 * call sites can `await` or `void` it explicitly, matching the Python API.
 */

import http from 'node:http';

/** Fire-and-forget POST a completed call payload into a locally-running dashboard, if any. */
export function notifyDashboard(
  callData: Record<string, unknown>,
  port = 8000,
): void {
  try {
    const body = JSON.stringify(callData);
    const req = http.request(
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

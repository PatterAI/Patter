/**
 * Built-in tunnel support via cloudflared.
 *
 * Spawns a Cloudflare Quick Tunnel that exposes a local port to the internet.
 * Zero account required — uses Cloudflare's free trycloudflare.com service.
 *
 * Install: npm install cloudflared
 */

import { getLogger } from './logger';

const log = getLogger();

export interface TunnelHandle {
  /** Public hostname (no protocol), e.g. "random-name.trycloudflare.com" */
  hostname: string;
  /** Stop the tunnel process */
  stop: () => void;
}

/**
 * Start a cloudflared quick tunnel pointing to the given local port.
 *
 * @param port - Local port to tunnel to
 * @param timeoutMs - How long to wait for the tunnel URL (default 30s)
 * @returns A handle with the public hostname and a stop function
 */
export async function startTunnel(port: number, timeoutMs = 30_000): Promise<TunnelHandle> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let tunnelMod: any;
  try {
    tunnelMod = await import('cloudflared' as string);
  } catch {
    throw new Error(
      'Built-in tunnel requires the "cloudflared" package. Install it with:\n\n' +
      '  npm install cloudflared\n\n' +
      'Or provide your own webhookUrl instead of using tunnel: true.'
    );
  }

  log.info('Starting tunnel to localhost:%d ...', port);

  const result = tunnelMod.tunnel({
    '--url': `http://localhost:${port}`,
  }) as { url: Promise<string>; connections: Promise<unknown>; stop: () => void };

  // Wait for the tunnel URL with a timeout
  const tunnelUrl = await Promise.race([
    result.url,
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error(
        `Tunnel failed to start within ${timeoutMs / 1000}s. ` +
        'Check your internet connection or provide webhookUrl manually.'
      )), timeoutMs)
    ),
  ]);

  // Extract hostname from URL (strip https://)
  const hostname = tunnelUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');

  log.info('Tunnel ready: https://%s', hostname);

  // Wait for at least one connection to be established
  result.connections.then(() => {
    log.info('Tunnel connections established');
  }).catch(() => {
    // Connection info may not always resolve — non-critical
  });

  return {
    hostname,
    stop: () => {
      log.info('Stopping tunnel...');
      result.stop();
    },
  };
}

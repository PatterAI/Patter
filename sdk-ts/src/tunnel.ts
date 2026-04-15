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

  // cloudflared@0.7+ exposes Tunnel.quick() for quick tunnels.
  // cloudflared@0.5 used tunnel({ '--url': ... }) returning { url: Promise }.
  // We support both APIs for backward compatibility.

  const TunnelClass = tunnelMod.Tunnel;
  const hasQuick = TunnelClass && typeof TunnelClass.quick === 'function';

  let instance: { on: (event: string, cb: (data: string) => void) => void; stop: () => void };

  if (hasQuick) {
    // New API (cloudflared 0.7+): Tunnel.quick(url) returns EventEmitter
    instance = TunnelClass.quick(`http://localhost:${port}`);
  } else {
    // Old API (cloudflared 0.5): tunnel({ '--url': ... }) returns { url: Promise, stop }
    const result = tunnelMod.tunnel({ '--url': `http://localhost:${port}` });
    if (result.url && typeof result.url.then === 'function') {
      // Old API with Promise-based URL
      const tunnelUrl: string = await Promise.race([
        result.url as Promise<string>,
        new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error(
            `Tunnel failed to start within ${timeoutMs / 1000}s. ` +
            'Check your internet connection or provide webhookUrl manually.'
          )), timeoutMs)
        ),
      ]);

      const hostname = tunnelUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');
      log.info('Tunnel ready: https://%s', hostname);
      return { hostname, stop: () => { log.info('Stopping tunnel...'); result.stop(); } };
    }
    // If result has .on (EventEmitter pattern), fall through to event-based handling
    instance = result;
  }

  // Event-based URL resolution (cloudflared 0.7+)
  const tunnelUrl: string = await Promise.race([
    new Promise<string>((resolve) => {
      instance.on('url', (url: string) => resolve(url));
    }),
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error(
        `Tunnel failed to start within ${timeoutMs / 1000}s. ` +
        'Check your internet connection or provide webhookUrl manually.'
      )), timeoutMs)
    ),
  ]);

  const hostname = tunnelUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');
  log.info('Tunnel ready: https://%s', hostname);

  return {
    hostname,
    stop: () => {
      log.info('Stopping tunnel...');
      instance.stop();
    },
  };
}

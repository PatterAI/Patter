/** Tunnel marker classes for Patter. Dispatched by the client to decide how to expose local servers. */

/**
 * Cloudflare Quick Tunnel marker — ask Patter to start a cloudflared tunnel.
 *
 * @example
 * ```ts
 * import { CloudflareTunnel } from "getpatter/tunnels";
 * const tunnel = new CloudflareTunnel();
 * ```
 */
export class CloudflareTunnel {
  readonly kind = "cloudflare" as const;
}

/**
 * Static hostname marker — use a pre-existing public hostname (no tunnel).
 *
 * @example
 * ```ts
 * import { Static } from "getpatter/tunnels";
 * const tunnel = new Static({ hostname: "agent.example.com" });
 * ```
 */
export class Static {
  readonly kind = "static" as const;
  readonly hostname: string;

  constructor(opts: { hostname: string }) {
    if (!opts.hostname) {
      throw new Error("Static tunnel requires a non-empty hostname.");
    }
    this.hostname = opts.hostname;
  }
}

/**
 * Ngrok tunnel marker — parity with the Python ``getpatter.tunnels.Ngrok``.
 *
 * Patter does not bundle the ngrok binary or auto-provision tunnels. This
 * marker exists so applications can pass an existing ngrok hostname through
 * the same code path as ``Static`` / ``CloudflareTunnel``. Constructing one
 * without a hostname is allowed (mirrors the Python type), but ``start()``
 * will throw — the user is expected to either pass a hostname or run the
 * tunnel themselves and feed the resulting URL via ``Static``.
 *
 * @example
 * ```ts
 * import { Ngrok } from "getpatter/tunnels";
 * const tunnel = new Ngrok({ hostname: "abc.ngrok.io" });
 * ```
 */
export class Ngrok {
  readonly kind = "ngrok" as const;
  readonly hostname: string;

  constructor(opts: { hostname?: string } = {}) {
    this.hostname = opts.hostname ?? "";
  }

  /**
   * Returns the configured hostname or throws if the marker was constructed
   * without one. Patter does not start ngrok itself — the user is expected
   * to either supply a hostname or run ngrok out-of-band.
   */
  start(): string {
    if (!this.hostname) {
      throw new Error(
        'Ngrok requires a hostname; pass new Ngrok({ hostname: "abc.ngrok.io" })',
      );
    }
    return this.hostname;
  }
}

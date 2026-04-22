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

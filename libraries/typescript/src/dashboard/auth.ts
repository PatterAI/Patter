/**
 * Dashboard authentication middleware for Express.
 *
 * When a token is configured, requests must include either:
 * - Authorization: Bearer <token> header
 * - ?token=<token> query parameter
 */

import type { Request, Response, NextFunction } from 'express';
import crypto from 'node:crypto';

function timingSafeCompare(a: string, b: string): boolean {
  const aBuf = Buffer.from(a);
  const bBuf = Buffer.from(b);
  if (aBuf.length !== bBuf.length) {
    // Still run a timingSafeEqual to keep the code path timing uniform.
    crypto.timingSafeEqual(aBuf, aBuf);
    return false;
  }
  return crypto.timingSafeEqual(aBuf, bBuf);
}

export function makeAuthMiddleware(token: string = '') {
  return (req: Request, res: Response, next: NextFunction): void => {
    if (!token) {
      next();
      return;
    }

    // Check Authorization header (timing-safe)
    const auth = req.headers.authorization || '';
    const expected = `Bearer ${token}`;
    if (timingSafeCompare(auth, expected)) {
      next();
      return;
    }

    // Check query param (timing-safe)
    const queryToken = String(req.query.token ?? '');
    if (timingSafeCompare(queryToken, token)) {
      next();
      return;
    }

    res.status(401).json({ error: 'Unauthorized' });
  };
}

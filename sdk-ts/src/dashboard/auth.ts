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
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
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
    if (auth.length === expected.length && timingSafeCompare(auth, expected)) {
      next();
      return;
    }

    // Check query param (timing-safe)
    const queryToken = String(req.query.token ?? '');
    if (queryToken.length === token.length && timingSafeCompare(queryToken, token)) {
      next();
      return;
    }

    res.status(401).json({ error: 'Unauthorized' });
  };
}

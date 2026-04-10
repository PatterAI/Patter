/**
 * Dashboard authentication middleware for Express.
 *
 * When a token is configured, requests must include either:
 * - Authorization: Bearer <token> header
 * - ?token=<token> query parameter
 */

import type { Request, Response, NextFunction } from 'express';

export function makeAuthMiddleware(token: string = '') {
  return (req: Request, res: Response, next: NextFunction): void => {
    if (!token) {
      next();
      return;
    }

    // Check Authorization header
    const auth = req.headers.authorization || '';
    if (auth === `Bearer ${token}`) {
      next();
      return;
    }

    // Check query param (for browser access)
    if (req.query.token === token) {
      next();
      return;
    }

    res.status(401).json({ error: 'Unauthorized' });
  };
}

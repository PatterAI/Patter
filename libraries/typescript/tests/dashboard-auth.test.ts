import { describe, it, expect, vi } from 'vitest';
import { makeAuthMiddleware } from '../src/dashboard/auth';

function mockReq(opts: { authorization?: string; token?: string } = {}) {
  return {
    headers: { authorization: opts.authorization || '' },
    query: { token: opts.token },
  } as unknown as import('express').Request;
}

function mockRes() {
  const res = {
    statusCode: 200,
    body: null as unknown,
    status(code: number) {
      res.statusCode = code;
      return res;
    },
    json(body: unknown) {
      res.body = body;
    },
  };
  return res as unknown as import('express').Response;
}

describe('makeAuthMiddleware', () => {
  it('allows all requests when no token configured', () => {
    const middleware = makeAuthMiddleware('');
    const next = vi.fn();
    middleware(mockReq(), mockRes(), next);
    expect(next).toHaveBeenCalled();
  });

  it('allows valid bearer token', () => {
    const middleware = makeAuthMiddleware('secret123');
    const next = vi.fn();
    middleware(mockReq({ authorization: 'Bearer secret123' }), mockRes(), next);
    expect(next).toHaveBeenCalled();
  });

  it('allows valid query token', () => {
    const middleware = makeAuthMiddleware('secret123');
    const next = vi.fn();
    middleware(mockReq({ token: 'secret123' }), mockRes(), next);
    expect(next).toHaveBeenCalled();
  });

  it('rejects invalid token', () => {
    const middleware = makeAuthMiddleware('secret123');
    const next = vi.fn();
    const res = mockRes();
    middleware(mockReq({ authorization: 'Bearer wrong' }), res, next);
    expect(next).not.toHaveBeenCalled();
    expect(res.statusCode).toBe(401);
  });

  it('rejects missing token when required', () => {
    const middleware = makeAuthMiddleware('secret123');
    const next = vi.fn();
    const res = mockRes();
    middleware(mockReq(), res, next);
    expect(next).not.toHaveBeenCalled();
    expect(res.statusCode).toBe(401);
  });
});

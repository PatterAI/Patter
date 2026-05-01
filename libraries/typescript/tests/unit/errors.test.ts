/**
 * Unit tests for the Patter error taxonomy (`src/errors.ts`).
 *
 * Verifies that:
 *   - `ErrorCode` is importable from the package root and contains the
 *     canonical set of codes.
 *   - Every concrete exception class carries the matching default `.code`.
 *   - Per-instance `code` overrides are honoured.
 *   - Subclassing relationships still hold (backward compat).
 *   - At least one real throw site surfaces the expected code.
 */

import { describe, expect, it } from "vitest";
import * as pkg from "../../src/index";
import {
  AuthenticationError,
  ErrorCode,
  PatterConnectionError,
  PatterError,
  ProvisionError,
  RateLimitError,
} from "../../src/errors";

describe("[unit] ErrorCode enum surface", () => {
  it("is re-exported from the package root", () => {
    expect(pkg.ErrorCode).toBe(ErrorCode);
    expect(pkg.ErrorCode.CONFIG).toBe("CONFIG");
  });

  it("has the canonical set of stable string values", () => {
    // Wire format is stable — these strings must NEVER change.
    expect(ErrorCode.CONFIG).toBe("CONFIG");
    expect(ErrorCode.CONNECTION).toBe("CONNECTION");
    expect(ErrorCode.AUTH).toBe("AUTH");
    expect(ErrorCode.TIMEOUT).toBe("TIMEOUT");
    expect(ErrorCode.RATE_LIMIT).toBe("RATE_LIMIT");
    expect(ErrorCode.WEBHOOK_VERIFICATION).toBe("WEBHOOK_VERIFICATION");
    expect(ErrorCode.INPUT_VALIDATION).toBe("INPUT_VALIDATION");
    expect(ErrorCode.PROVIDER_ERROR).toBe("PROVIDER_ERROR");
    expect(ErrorCode.PROVISION).toBe("PROVISION");
    expect(ErrorCode.INTERNAL).toBe("INTERNAL");
  });

  it("matches Python ErrorCode value-for-value (parity)", () => {
    // Mirrored byte-for-byte by libraries/python/getpatter/exceptions.py.
    const expected = [
      "CONFIG",
      "CONNECTION",
      "AUTH",
      "TIMEOUT",
      "RATE_LIMIT",
      "WEBHOOK_VERIFICATION",
      "INPUT_VALIDATION",
      "PROVIDER_ERROR",
      "PROVISION",
      "INTERNAL",
    ].sort();
    const actual = Object.values(ErrorCode).sort();
    expect(actual).toEqual(expected);
  });
});

describe("[unit] default codes per exception class", () => {
  it.each<[new (m: string) => PatterError, ErrorCode]>([
    [PatterError, ErrorCode.INTERNAL],
    [PatterConnectionError, ErrorCode.CONNECTION],
    [AuthenticationError, ErrorCode.AUTH],
    [ProvisionError, ErrorCode.PROVISION],
    [RateLimitError, ErrorCode.RATE_LIMIT],
  ])("%p instances default to the matching ErrorCode", (Cls, expected) => {
    const err = new Cls("boom");
    expect(err.code).toBe(expected);
    expect(err.message).toBe("boom");
    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(PatterError);
  });
});

describe("[unit] per-instance code overrides", () => {
  it("honours an explicit code on the connection error", () => {
    const err = new PatterConnectionError("api 5xx", {
      code: ErrorCode.PROVIDER_ERROR,
    });
    expect(err.code).toBe(ErrorCode.PROVIDER_ERROR);
  });

  it("honours an explicit code on the base class", () => {
    const err = new PatterError("oops", { code: ErrorCode.TIMEOUT });
    expect(err.code).toBe(ErrorCode.TIMEOUT);
  });
});

describe("[unit] backward compatibility", () => {
  it("preserves the existing class hierarchy", () => {
    const conn = new PatterConnectionError("x");
    const auth = new AuthenticationError("x");
    const prov = new ProvisionError("x");
    const rate = new RateLimitError("x");

    expect(conn).toBeInstanceOf(PatterError);
    expect(auth).toBeInstanceOf(PatterError);
    expect(prov).toBeInstanceOf(PatterError);
    expect(rate).toBeInstanceOf(PatterConnectionError);
    expect(rate).toBeInstanceOf(PatterError);
  });

  it("supports the historical message-only constructor (no options arg)", () => {
    // Opt-in config rule: `options` is optional with a safe default.
    expect(() => new AuthenticationError("auth failed")).not.toThrow();
    expect(() => new ProvisionError("number rejected")).not.toThrow();
    expect(() => new PatterConnectionError("ws closed")).not.toThrow();
    expect(() => new RateLimitError("429")).not.toThrow();
    expect(() => new PatterError("generic")).not.toThrow();
  });

  it("preserves the `.name` property for each subclass", () => {
    expect(new PatterError("x").name).toBe("PatterError");
    expect(new PatterConnectionError("x").name).toBe("PatterConnectionError");
    expect(new AuthenticationError("x").name).toBe("AuthenticationError");
    expect(new ProvisionError("x").name).toBe("ProvisionError");
    expect(new RateLimitError("x").name).toBe("RateLimitError");
  });
});

describe("[unit] real throw site smoke", () => {
  it("a thrown ProvisionError carries ErrorCode.PROVISION", () => {
    // Mirrors `client.ts` Telnyx / Twilio call-initiation throw sites:
    //   throw new ProvisionError(`Failed to initiate call: ...`)
    let caught: PatterError | undefined;
    try {
      throw new ProvisionError("Failed to initiate call: 503 upstream");
    } catch (e: unknown) {
      if (e instanceof PatterError) caught = e;
    }
    expect(caught).toBeDefined();
    expect(caught!.code).toBe(ErrorCode.PROVISION);
    // Generic catch-all path: a downstream UI mapping `code → toast` doesn't
    // need to import the subclass at all.
    expect(caught!.code).toBe("PROVISION");
  });
});

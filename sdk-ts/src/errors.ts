export class PatterError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PatterError";
  }
}

export class PatterConnectionError extends PatterError {
  constructor(message: string) {
    super(message);
    this.name = "PatterConnectionError";
  }
}

export class AuthenticationError extends PatterError {
  constructor(message: string) {
    super(message);
    this.name = "AuthenticationError";
  }
}

export class ProvisionError extends PatterError {
  constructor(message: string) {
    super(message);
    this.name = "ProvisionError";
  }
}

/** Thrown when a provider returns HTTP 429 on connect/upgrade. */
export class RateLimitError extends PatterConnectionError {
  constructor(message: string) {
    super(message);
    this.name = "RateLimitError";
  }
}

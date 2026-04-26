class PatterError(Exception):
    pass


class PatterConnectionError(PatterError):
    pass


class AuthenticationError(PatterError):
    pass


class ProvisionError(PatterError):
    pass


class RateLimitError(PatterConnectionError):
    """Raised when a provider returns HTTP 429 on connect/upgrade."""

    pass

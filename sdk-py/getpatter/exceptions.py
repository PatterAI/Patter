class PatterError(Exception):
    """Base class for all errors raised by the Patter SDK."""


class PatterConnectionError(PatterError):
    """Raised when the SDK cannot establish or maintain a network connection to a Patter backend or upstream provider."""


class AuthenticationError(PatterError):
    """Raised when API key or credential validation fails (HTTP 401/403 or invalid signature)."""


class ProvisionError(PatterError):
    """Raised when phone number provisioning, webhook configuration, or carrier setup fails."""


class RateLimitError(PatterConnectionError):
    """Raised when a provider returns HTTP 429 on connect/upgrade."""

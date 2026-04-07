class PatterError(Exception):
    pass


class PatterConnectionError(PatterError):
    pass


class AuthenticationError(PatterError):
    pass


class ProvisionError(PatterError):
    pass

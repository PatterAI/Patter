"""Dashboard authentication middleware."""


def make_auth_dependency(token: str = ""):
    """Create a FastAPI dependency for token-based authentication.

    Returns a callable that can be used with ``Depends()``.
    When *token* is empty, all requests are allowed.
    """
    from fastapi import HTTPException, Request

    async def verify_token(request: Request) -> None:
        if not token:
            return

        # Check Authorization header
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {token}":
            return

        # Check query param (for browser access)
        if request.query_params.get("token") == token:
            return

        raise HTTPException(status_code=401, detail="Unauthorized")

    return verify_token

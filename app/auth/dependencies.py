"""
FastAPI dependencies for authentication.

Usage
-----
Protected routes::

    @router.get("/foo")
    async def foo(user: CurrentUser = Depends(require_auth)):
        return {"sub": user.sub}

Public routes (catalog, health)::

    @router.get("/bar", dependencies=[Depends(public)])
    async def bar():
        return {"ok": True}

In local mode every request is treated as the synthetic ``LOCAL_USER`` — no
token is required and none is validated. In cloud mode a Cognito ID token in
the ``Authorization: Bearer`` header is mandatory.
"""

import getpass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.auth.cognito import CognitoVerifier
from app.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)

# Module-level cache: one CognitoVerifier per (jwks_url, issuer) pair.
_verifier_cache: dict[tuple[str, str], CognitoVerifier] = {}


class CurrentUser(BaseModel):
    """The authenticated user extracted from a verified Cognito ID token."""

    sub: str
    email: str | None = None
    username: str | None = None
    groups: list[str] = []
    token: str


def _local_user() -> CurrentUser:
    """Return a CurrentUser whose sub is the OS-level username of the running process."""
    return CurrentUser(sub=getpass.getuser(), token="")


def get_verifier(settings: Settings = Depends(get_settings)) -> CognitoVerifier:
    """
    Return (or lazily create) the ``CognitoVerifier`` for the current settings.

    Tests override this dependency to inject a pre-configured verifier that
    uses test JWKS instead of making network calls.
    """
    key = (settings.jwks_url, settings.cognito_issuer, settings.cognito_client_id)
    if key not in _verifier_cache:
        _verifier_cache[key] = CognitoVerifier(
            jwks_url=settings.jwks_url,
            issuer=settings.cognito_issuer,
            client_id=settings.cognito_client_id,
        )
    return _verifier_cache[key]


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
    verifier: CognitoVerifier = Depends(get_verifier),
) -> CurrentUser:
    """
    Dependency that enforces authentication.

    - **Local mode**: bypasses all token checks and returns the synthetic
      ``LOCAL_USER``.
    - **Cloud mode**: requires a valid Cognito ID token in the
      ``Authorization: Bearer`` header.

    Raises ``HTTP 401`` for missing or invalid tokens in cloud mode.
    """
    if settings.execution_mode == "local":
        return _local_user()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Provide a Cognito ID token as 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = await verifier.verify(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return CurrentUser(
        sub=claims["sub"],
        email=claims.get("email"),
        username=claims.get("cognito:username"),
        groups=claims.get("cognito:groups", []),
        token=credentials.credentials,
    )


async def public() -> None:
    """
    Marker dependency for explicitly public routes.

    Attach with ``dependencies=[Depends(public)]`` to make the intent
    visible in code review and the OpenAPI schema.
    """

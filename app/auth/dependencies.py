"""
FastAPI dependencies for authentication.

Auth model: BFF (backend-for-frontend) — tokens are stored in httpOnly cookies
and never exposed to browser JavaScript. The ``session`` cookie holds the Cognito
ID token set by ``GET /auth/callback`` after a successful OAuth2 authorization-code
+ PKCE exchange.

Usage
-----
Protected routes::

    @router.get("/foo")
    async def foo(user: CurrentUser = Depends(require_auth)):
        return {"sub": user.sub}

Public routes (catalog, health, docs)::

    @router.get("/bar", dependencies=[Depends(public)])
    async def bar():
        return {"ok": True}

In local mode every request is treated as the OS user — no cookie is required and
none is validated. In cloud mode a valid ``session`` cookie is mandatory.
"""

import getpass

import jwt
from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.auth.cognito import CognitoVerifier
from app.config import Settings, get_settings

# Module-level cache: one CognitoVerifier per (jwks_url, issuer, client_id) triple.
_verifier_cache: dict[tuple[str, str, str], CognitoVerifier] = {}


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
    request: Request,
    settings: Settings = Depends(get_settings),
    verifier: CognitoVerifier = Depends(get_verifier),
) -> CurrentUser:
    """
    Dependency that enforces authentication.

    - **Local mode**: bypasses all token checks and returns the OS user.
    - **Cloud mode**: requires a valid Cognito ID token in the ``session``
      httpOnly cookie (set by ``GET /auth/callback``).

    Raises ``HTTP 401`` for a missing or invalid session cookie in cloud mode.
    """
    if settings.execution_mode == "local":
        return _local_user()

    token = request.cookies.get("session")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Visit /auth/login to begin the sign-in flow.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = await verifier.verify(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid session: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return CurrentUser(
        sub=claims["sub"],
        email=claims.get("email"),
        username=claims.get("cognito:username"),
        groups=claims.get("cognito:groups", []),
        token=token,
    )


async def public() -> None:
    """
    Marker dependency for explicitly public routes.

    Attach with ``dependencies=[Depends(public)]`` to make the intent
    visible in code review and the OpenAPI schema.
    """

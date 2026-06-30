"""
BFF OAuth2 endpoints — authorization code + PKCE flow with Cognito.

All tokens are stored in httpOnly cookies and never exposed to browser JavaScript.

Flow
----
1. Browser navigates to ``GET /auth/login``.
   Server generates CSRF state + PKCE pair, stashes them in short-lived cookies,
   and redirects the browser to the Cognito Hosted UI.

2. After the user authenticates, Cognito redirects to ``GET /auth/callback?code=...&state=...``.
   Server validates state, exchanges the code for tokens, verifies the ID token,
   sets the ``session`` cookie, and redirects the browser to the frontend.

3. The frontend polls ``GET /auth/me`` (sends the ``session`` cookie automatically)
   to determine auth state and retrieve user claims.

4. ``GET /auth/logout`` clears the session cookie and redirects to the Cognito
   logout URL, which then redirects back to the configured frontend URL.
"""

from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.auth.dependencies import CurrentUser, get_verifier, require_auth
from app.auth.cognito import CognitoVerifier
from app.auth.pkce import generate_pkce_pair, generate_state
from app.config import Settings, get_settings
from app.models.errors import ErrorDetail

router = APIRouter(prefix="/auth", tags=["Auth"])

_STATE_COOKIE = "auth_state"
_VERIFIER_COOKIE = "pkce_verifier"
_SESSION_COOKIE = "session"
_REFRESH_COOKIE = "refresh_token"

_STATE_TTL = 600    # 10 minutes — long enough for a slow login flow
_SESSION_TTL = 3600  # 1 hour — matches Cognito ID token default lifetime


class UserInfo(BaseModel):
    """Safe user claims returned by GET /auth/me. The raw token is never included."""

    sub: str
    email: str | None = None
    username: str | None = None
    groups: list[str] = []


def _ssl_verify(settings: Settings) -> bool | str:
    """Return the httpx `verify` value: cert path when a valid CA bundle is configured, True otherwise."""
    cb = settings.ca_bundle
    if cb and cb.exists() and cb.stat().st_size > 0:
        return str(cb)
    return True


def _cookie_kwargs(settings: Settings, max_age: int) -> dict:
    """Shared httpOnly cookie attributes."""
    return dict(
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain,
        path="/",
        max_age=max_age,
    )


def _delete_cookie_kwargs(settings: Settings) -> dict:
    """Attributes for a deletion-intent (max_age=0) cookie."""
    return dict(
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain,
        path="/",
        max_age=0,
    )


@router.get(
    "/login",
    summary="Begin OAuth2 sign-in",
    description=(
        "Generates a CSRF ``state`` and PKCE ``code_verifier``/``code_challenge``, "
        "stores them in short-lived httpOnly cookies, then redirects the browser to "
        "the Cognito Hosted UI authorization endpoint. "
        "Navigate the browser (not a fetch/XHR) to this URL to start sign-in."
    ),
    response_class=RedirectResponse,
    responses={
        302: {"description": "Redirect to Cognito authorize endpoint."},
        503: {"model": ErrorDetail, "description": "Cognito domain not configured."},
    },
)
async def login(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if not settings.cognito_domain:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OAuth is not configured on this server (NICHART_COGNITO_DOMAIN is unset).",
        )

    state = generate_state()
    verifier, challenge = generate_pkce_pair()

    params = urlencode({
        "response_type": "code",
        "client_id": settings.cognito_client_id,
        "redirect_uri": settings.auth_callback_url,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "openid email profile",
    })
    authorize_url = f"{settings.cognito_domain}/oauth2/authorize?{params}"

    response = RedirectResponse(url=authorize_url, status_code=302)
    response.set_cookie(_STATE_COOKIE, state, **_cookie_kwargs(settings, _STATE_TTL))
    response.set_cookie(_VERIFIER_COOKIE, verifier, **_cookie_kwargs(settings, _STATE_TTL))
    return response


@router.get(
    "/callback",
    summary="OAuth2 callback — exchange code for session",
    description=(
        "Cognito redirects here after the user authenticates. "
        "Validates the CSRF ``state`` cookie, exchanges the authorization code for "
        "tokens (using the PKCE verifier + client secret), verifies the ID token, "
        "sets the ``session`` cookie, and redirects to the frontend. "
        "This endpoint is called by the browser following a redirect — not by JS fetch."
    ),
    response_class=RedirectResponse,
    responses={
        302: {"description": "Redirect to frontend after successful authentication."},
        400: {"model": ErrorDetail, "description": "State mismatch or missing parameters."},
        401: {"model": ErrorDetail, "description": "Token validation failed."},
        502: {"model": ErrorDetail, "description": "Cognito token exchange failed."},
    },
)
async def callback(
    request: Request,
    settings: Settings = Depends(get_settings),
    verifier: CognitoVerifier = Depends(get_verifier),
) -> RedirectResponse:
    code = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        raise HTTPException(400, f"Authorization error from Cognito: {error}")

    if not code or not returned_state:
        raise HTTPException(400, "Missing 'code' or 'state' in callback parameters.")

    # CSRF validation
    expected_state = request.cookies.get(_STATE_COOKIE)
    if not expected_state or returned_state != expected_state:
        raise HTTPException(400, "State mismatch — possible CSRF attack. Please sign in again.")

    code_verifier = request.cookies.get(_VERIFIER_COOKIE)
    if not code_verifier:
        raise HTTPException(400, "PKCE verifier cookie missing. Please sign in again.")

    # Exchange authorization code for tokens
    try:
        async with httpx.AsyncClient(verify=_ssl_verify(settings)) as http:
            token_resp = await http.post(
                f"{settings.cognito_domain}/oauth2/token",
                content=urlencode({
                    "grant_type": "authorization_code",
                    "client_id": settings.cognito_client_id,
                    "client_secret": settings.cognito_client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": settings.auth_callback_url,
                }).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Could not reach Cognito token endpoint: {exc}")

    if token_resp.status_code != 200:
        raise HTTPException(
            502,
            f"Cognito token exchange failed ({token_resp.status_code}): {token_resp.text}",
        )

    tokens = token_resp.json()
    id_token = tokens.get("id_token")
    refresh_token = tokens.get("refresh_token")

    if not id_token:
        raise HTTPException(502, "Cognito did not return an id_token.")

    # Validate the ID token before trusting it
    try:
        await verifier.verify(id_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Received an already-expired ID token from Cognito.")
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"ID token validation failed: {exc}")

    response = RedirectResponse(url=settings.frontend_url, status_code=302)

    # Set session cookie (id_token)
    response.set_cookie(_SESSION_COOKIE, id_token, **_cookie_kwargs(settings, _SESSION_TTL))

    # Optionally persist the refresh token for future /auth/refresh calls
    if refresh_token:
        response.set_cookie(
            _REFRESH_COOKIE,
            refresh_token,
            **_cookie_kwargs(settings, 30 * 24 * 3600),  # 30 days — matches Cognito refresh token default
        )

    # Clear the short-lived PKCE / state cookies
    response.set_cookie(_STATE_COOKIE, "", **_delete_cookie_kwargs(settings))
    response.set_cookie(_VERIFIER_COOKIE, "", **_delete_cookie_kwargs(settings))

    return response


@router.post(
    "/refresh",
    summary="Refresh the session",
    description=(
        "Uses the ``refresh_token`` httpOnly cookie to obtain a new ID token from Cognito "
        "and updates the ``session`` cookie. Returns updated user claims on success. "
        "Returns 401 when there is no refresh token or it has expired — "
        "the client should redirect to ``GET /auth/login``."
    ),
    response_model=UserInfo,
    responses={
        401: {"model": ErrorDetail, "description": "No refresh token or token expired."},
        502: {"model": ErrorDetail, "description": "Cognito token refresh failed."},
    },
)
async def refresh(
    request: Request,
    settings: Settings = Depends(get_settings),
    verifier: CognitoVerifier = Depends(get_verifier),
) -> JSONResponse:
    refresh_token = request.cookies.get(_REFRESH_COOKIE)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token — please sign in again.")

    try:
        async with httpx.AsyncClient(verify=_ssl_verify(settings)) as http:
            token_resp = await http.post(
                f"{settings.cognito_domain}/oauth2/token",
                content=urlencode({
                    "grant_type": "refresh_token",
                    "client_id": settings.cognito_client_id,
                    "client_secret": settings.cognito_client_secret,
                    "refresh_token": refresh_token,
                }).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Could not reach Cognito token endpoint: {exc}")

    if token_resp.status_code == 400:
        raise HTTPException(401, "Refresh token expired or invalid — please sign in again.")
    if token_resp.status_code != 200:
        raise HTTPException(502, f"Cognito token refresh failed ({token_resp.status_code}): {token_resp.text}")

    tokens = token_resp.json()
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(502, "Cognito did not return an id_token.")

    try:
        claims = await verifier.verify(id_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Received an already-expired ID token from Cognito.")
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"ID token validation failed: {exc}")

    user_info = UserInfo(
        sub=claims.get("sub", ""),
        email=claims.get("email"),
        username=claims.get("cognito:username"),
        groups=claims.get("cognito:groups", []),
    )
    response = JSONResponse(content=user_info.model_dump())
    response.set_cookie(_SESSION_COOKIE, id_token, **_cookie_kwargs(settings, _SESSION_TTL))
    return response


@router.get(
    "/me",
    summary="Return current user claims",
    description=(
        "Reads the ``session`` cookie and returns safe user claims if the session "
        "is valid. The frontend should poll this endpoint to determine auth state "
        "on page load. Returns 401 when there is no session or it has expired — "
        "the client should redirect to ``GET /auth/login``. "
        "In local mode, always returns the OS user without a cookie."
    ),
    response_model=UserInfo,
    responses={
        401: {"model": ErrorDetail, "description": "No valid session."},
    },
)
async def me(user: CurrentUser = Depends(require_auth)) -> UserInfo:
    return UserInfo(
        sub=user.sub,
        email=user.email,
        username=user.username,
        groups=user.groups,
    )


@router.get(
    "/logout",
    summary="Sign out",
    description=(
        "Clears the ``session`` and ``refresh_token`` cookies, then redirects to the "
        "Cognito logout URL (which itself redirects to the configured frontend URL). "
        "Navigate the browser to this URL — do not call it via fetch/XHR."
    ),
    response_class=RedirectResponse,
    responses={302: {"description": "Redirect to Cognito logout endpoint."}},
)
async def logout(
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if settings.cognito_domain:
        logout_params = urlencode({
            "client_id": settings.cognito_client_id,
            "logout_uri": settings.frontend_url,
        })
        destination = f"{settings.cognito_domain}/logout?{logout_params}"
    else:
        destination = settings.frontend_url

    response = RedirectResponse(url=destination, status_code=302)
    response.set_cookie(_SESSION_COOKIE, "", **_delete_cookie_kwargs(settings))
    response.set_cookie(_REFRESH_COOKIE, "", **_delete_cookie_kwargs(settings))
    return response

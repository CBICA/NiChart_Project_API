"""
Tests for the authentication layer.

Auth model: BFF — tokens live in httpOnly ``session`` cookies, never in JS.

Covers:
- Local mode: any protected route is accessible without a cookie.
- Cloud mode: protected routes require a valid session cookie.
- Cloud mode: specific token failure modes each return 401.
- BFF endpoints: /auth/login, /auth/callback, /auth/me, /auth/logout.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Local mode ────────────────────────────────────────────────────────────────

def test_local_mode_no_cookie_required(local_client):
    """In local mode, protected routes must not require a session cookie."""
    resp = local_client.get("/projects")
    assert resp.status_code != 401


def test_local_mode_garbage_cookie_ignored(local_client):
    """In local mode, a garbage session cookie must not cause an error."""
    resp = local_client.get("/projects", cookies={"session": "not-a-real-token"})
    assert resp.status_code != 401


# ── Cloud mode — valid session cookie ────────────────────────────────────────

def test_cloud_mode_valid_session_cookie(cloud_client, make_id_token):
    token = make_id_token(sub="user-abc", email="user@example.com")
    resp = cloud_client.get("/projects", cookies={"session": token})
    assert resp.status_code != 401


def test_cloud_mode_valid_token_with_groups(cloud_client, make_id_token):
    token = make_id_token(sub="admin", groups=["admins", "users"])
    resp = cloud_client.get("/projects", cookies={"session": token})
    assert resp.status_code != 401


# ── Cloud mode — token failures ───────────────────────────────────────────────

def test_cloud_mode_missing_cookie(cloud_client):
    resp = cloud_client.get("/projects")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


def test_cloud_mode_malformed_token(cloud_client):
    resp = cloud_client.get("/projects", cookies={"session": "not.a.jwt"})
    assert resp.status_code == 401


def test_cloud_mode_expired_token(cloud_client, make_id_token):
    token = make_id_token(expired=True)
    resp = cloud_client.get("/projects", cookies={"session": token})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_cloud_mode_wrong_issuer(cloud_client, make_id_token):
    token = make_id_token(issuer="https://evil.example.com/wrong-pool")
    resp = cloud_client.get("/projects", cookies={"session": token})
    assert resp.status_code == 401


def test_cloud_mode_access_token_rejected(cloud_client, make_id_token):
    """Access tokens (token_use='access') must be rejected; only ID tokens are valid."""
    token = make_id_token(token_use="access")
    resp = cloud_client.get("/projects", cookies={"session": token})
    assert resp.status_code == 401
    assert "token_use" in resp.json()["detail"]


# ── Public routes in cloud mode ───────────────────────────────────────────────

def test_catalog_public_no_cookie(cloud_client):
    resp = cloud_client.get("/catalog/pipelines")
    assert resp.status_code != 401


def test_health_public_no_cookie(cloud_client):
    resp = cloud_client.get("/health")
    assert resp.status_code == 200


# ── GET /auth/me ──────────────────────────────────────────────────────────────

def test_auth_me_local_mode(local_client):
    """In local mode /auth/me returns the OS user without a cookie."""
    resp = local_client.get("/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert "sub" in data
    assert "token" not in data  # raw token must never be returned


def test_auth_me_valid_cookie(cloud_client, make_id_token):
    token = make_id_token(sub="user-xyz", email="u@example.com")
    resp = cloud_client.get("/auth/me", cookies={"session": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sub"] == "user-xyz"
    assert data["email"] == "u@example.com"
    assert "token" not in data


def test_auth_me_no_cookie_returns_401(cloud_client):
    resp = cloud_client.get("/auth/me")
    assert resp.status_code == 401


def test_auth_me_expired_cookie_returns_401(cloud_client, make_id_token):
    token = make_id_token(expired=True)
    resp = cloud_client.get("/auth/me", cookies={"session": token})
    assert resp.status_code == 401


# ── GET /auth/login ───────────────────────────────────────────────────────────

def test_auth_login_redirects_to_cognito(cloud_client):
    """Login must redirect to the Cognito authorize endpoint."""
    resp = cloud_client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "oauth2/authorize" in location
    assert "code_challenge" in location
    assert "state" in location


def test_auth_login_sets_state_and_verifier_cookies(cloud_client):
    resp = cloud_client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    cookies = resp.cookies
    assert "auth_state" in cookies
    assert "pkce_verifier" in cookies


def test_auth_login_unconfigured_returns_503(local_client):
    """Without NICHART_COGNITO_DOMAIN, /auth/login returns 503."""
    resp = local_client.get("/auth/login")
    assert resp.status_code == 503


# ── GET /auth/callback ────────────────────────────────────────────────────────

def test_auth_callback_rejects_state_mismatch(cloud_client):
    """Callback must reject a state that doesn't match the cookie."""
    # Set a real state cookie but send a different state in the query param
    resp = cloud_client.get(
        "/auth/callback?code=somecode&state=tampered_state",
        cookies={"auth_state": "correct_state", "pkce_verifier": "verifier"},
    )
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"].lower()


def test_auth_callback_rejects_missing_code(cloud_client):
    resp = cloud_client.get(
        "/auth/callback?state=somestate",
        cookies={"auth_state": "somestate", "pkce_verifier": "verifier"},
    )
    assert resp.status_code == 400


def test_auth_callback_rejects_cognito_error_param(cloud_client):
    resp = cloud_client.get(
        "/auth/callback?error=access_denied&state=somestate",
        cookies={"auth_state": "somestate"},
    )
    assert resp.status_code == 400
    assert "access_denied" in resp.json()["detail"]


def test_auth_callback_sets_session_cookie_on_success(cloud_client, make_id_token):
    """
    On a valid exchange, callback must set the session cookie and redirect.
    Mocks the Cognito token endpoint call.
    """
    id_token = make_id_token(sub="new-user")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id_token": id_token, "refresh_token": "rt-abc"}

    with patch("app.routers.auth.httpx.AsyncClient") as mock_client_cls:
        mock_instance = AsyncMock()
        mock_instance.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = cloud_client.get(
            "/auth/callback?code=authcode123&state=mystate",
            cookies={"auth_state": "mystate", "pkce_verifier": "myverifier"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert "session" in resp.cookies
    # Short-lived PKCE / state cookies must be cleared
    assert resp.cookies.get("auth_state") in ("", None)
    assert resp.cookies.get("pkce_verifier") in ("", None)


# ── GET /auth/logout ──────────────────────────────────────────────────────────

def test_auth_logout_clears_session_cookie(cloud_client):
    resp = cloud_client.get("/auth/logout", follow_redirects=False)
    assert resp.status_code == 302
    # Cookie deletion is signalled by max_age=0 / empty value
    assert resp.cookies.get("session") in ("", None)


def test_auth_logout_redirects(cloud_client):
    resp = cloud_client.get("/auth/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"]

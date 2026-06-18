"""
Tests for the authentication layer.

Covers:
- Local mode: any protected route is accessible without a token.
- Cloud mode: protected routes require a valid Cognito ID token.
- Cloud mode: specific token failure modes each return 401.
"""

import pytest


# ── Local mode ────────────────────────────────────────────────────────────────

def test_local_mode_no_token_required(local_client):
    """In local mode, protected routes must not require a token."""
    resp = local_client.get("/projects")
    # 501 stub is fine — what matters is it's NOT 401
    assert resp.status_code != 401


def test_local_mode_token_ignored(local_client):
    """In local mode, a garbage token must not cause an error."""
    resp = local_client.get("/projects", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code != 401


# ── Cloud mode — valid token ──────────────────────────────────────────────────

def test_cloud_mode_valid_token(cloud_client, make_id_token):
    token = make_id_token(sub="user-abc", email="user@example.com")
    resp = cloud_client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    # Auth passed — any non-401 response is acceptable
    assert resp.status_code != 401


def test_cloud_mode_valid_token_with_groups(cloud_client, make_id_token):
    token = make_id_token(sub="admin", groups=["admins", "users"])
    resp = cloud_client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code != 401


# ── Cloud mode — token failures ───────────────────────────────────────────────

def test_cloud_mode_missing_token(cloud_client):
    resp = cloud_client.get("/projects")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


def test_cloud_mode_malformed_token(cloud_client):
    resp = cloud_client.get("/projects", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_cloud_mode_expired_token(cloud_client, make_id_token):
    token = make_id_token(expired=True)
    resp = cloud_client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_cloud_mode_wrong_issuer(cloud_client, make_id_token):
    token = make_id_token(issuer="https://evil.example.com/wrong-pool")
    resp = cloud_client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_cloud_mode_access_token_rejected(cloud_client, make_id_token):
    """Access tokens (token_use='access') must be rejected; only ID tokens are valid."""
    token = make_id_token(token_use="access")
    resp = cloud_client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "token_use" in resp.json()["detail"]


# ── Public routes in cloud mode ───────────────────────────────────────────────

def test_catalog_public_no_token(cloud_client):
    """Catalog endpoints must be accessible without a token in cloud mode."""
    resp = cloud_client.get("/catalog/pipelines")
    assert resp.status_code != 401


def test_health_public_no_token(cloud_client):
    resp = cloud_client.get("/health")
    assert resp.status_code == 200
